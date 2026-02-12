#!/bin/bash
# session-summary.sh - 会话结束时提取摘要并保存
# 由 Stop 或 SessionEnd Hook 触发
# 策略: 从 JSONL transcript 提取对话文本 -> LLM 生成摘要 -> 保存摘要而非原始内容
# 改进: 借鉴 OpenClaw 的知识提炼思路，将 41MB 死存储转化为可检索知识

MEMORY_DIR="$HOME/.claude/memory"
DAILY_DIR="$MEMORY_DIR/daily"
SESSIONS_DIR="$MEMORY_DIR/sessions"
JQ_FILTER="$HOME/.claude/scripts/lib/extract-conversation.jq"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
DAILY_FILE="$DAILY_DIR/$DATE.md"

# 配置
SUMMARIZE_ENABLED="${CLAUDE_SESSION_SUMMARIZE:-true}"  # 设为 false 可回退到原始保存
MAX_CONVERSATION_CHARS=30000  # 传给 LLM 的最大字符数
SUMMARY_MODEL="haiku"  # 用 Haiku 做摘要，性价比最高
CLAUDE_TIMEOUT=30  # claude CLI 超时秒数 (防止 Hook 挂起)

# 从 stdin 读取 Hook 输入
INPUT=$(cat)

# 从 hook 输入中获取 cwd (比 $PWD 更可靠)
if command -v jq &> /dev/null; then
    HOOK_CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
fi
PROJECT=$(basename "${HOOK_CWD:-$PWD}")

# 尝试多种方式获取 transcript_path
TRANSCRIPT_PATH="${CLAUDE_TRANSCRIPT_PATH:-}"

if [ -z "$TRANSCRIPT_PATH" ]; then
    if command -v jq &> /dev/null; then
        TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)
    else
        TRANSCRIPT_PATH=$(echo "$INPUT" | grep -o '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*: *"\([^"]*\)"/\1/' || echo "")
    fi
fi

# 确保目录存在
mkdir -p "$DAILY_DIR"
mkdir -p "$SESSIONS_DIR"

# 创建每日笔记（如不存在）
if [ ! -f "$DAILY_FILE" ]; then
    cat > "$DAILY_FILE" << EOF
# $DATE

> 自动生成的每日开发笔记

EOF
fi

# ============================================================
# 从 JSONL transcript 中提取纯文本对话内容
# 输入: JSONL 文件路径
# 输出: 格式化的对话文本 (stdout)
# ============================================================
extract_conversation() {
    local transcript="$1"

    if ! command -v jq &> /dev/null; then
        # [CRITICAL-2 修复] jq 不可用时直接 fail，不用损坏的 grep fallback
        echo "[需要 jq] 请运行: brew install jq" >&2
        return 1
    fi

    # 使用共享 jq 过滤器 (消除重复代码)
    if [ -f "$JQ_FILTER" ]; then
        jq -r -f "$JQ_FILTER" "$transcript" 2>/dev/null
    else
        # 内联 fallback (如果共享文件不存在)
        jq -r '
            select(.type == "user" or .type == "assistant") |
            if .type == "user" then
                "用户: " + (
                    if .message.content then
                        if (.message.content | type) == "string" then
                            .message.content
                        elif (.message.content | type) == "array" then
                            [.message.content[] | select(.type == "text") | .text] | join("\n")
                        else "" end
                    else "" end)
            elif .type == "assistant" then
                "助手: " + (
                    if .message.content then
                        if (.message.content | type) == "array" then
                            [.message.content[] | select(.type == "text") | .text] | join("\n")
                        else (.message.content | tostring) end
                    else "" end)
            else empty end |
            select(length > 5)
        ' "$transcript" 2>/dev/null
    fi
}

# ============================================================
# 用 LLM 生成会话摘要 (带超时保护)
# 输入: 对话文本, 项目名
# 输出: Markdown 格式摘要 (stdout)
# ============================================================
generate_summary() {
    local conversation="$1"
    local project="$2"

    # [HIGH-2 修复] 使用 printf 避免转义字符问题
    local truncated_conv
    truncated_conv=$(printf '%s' "$conversation" | head -c "$MAX_CONVERSATION_CHARS")

    # 检查 claude CLI 是否可用
    if ! command -v claude &> /dev/null; then
        echo "**[摘要不可用: claude CLI 未安装]**"
        return 1
    fi

    # [HIGH-1 修复] 带超时的 claude CLI 调用
    local summary
    local err_file
    err_file=$(mktemp "${TMPDIR:-/tmp}/claude-summary-err.XXXXXX")

    # macOS 没有 timeout 命令，用 perl 实现超时
    summary=$(printf '%s' "$truncated_conv" | perl -e '
        use POSIX ":sys_wait_h";
        $SIG{ALRM} = sub { kill("TERM", $pid); exit 124; };
        alarm('"$CLAUDE_TIMEOUT"');
        $pid = open(my $fh, "-|", @ARGV) or exit 1;
        local $/; $out = <$fh>; close $fh;
        print $out;
    ' -- claude -p \
        --model "$SUMMARY_MODEL" \
        --no-session-persistence \
        --system-prompt "你是一个会话摘要提取器。从以下开发会话对话中提取关键知识点。

输出格式要求（严格遵循）:
1. **主题**: 一句话描述会话主要目的
2. **关键知识点**: 3-5 个要点，每个以 - 开头
3. **涉及文件**: 列出被编辑或讨论的关键文件路径
4. **决策记录**: 记录做出的重要技术决策（如果有）
5. **待办事项**: 记录未完成的事项（如果有）

规则:
- 只提取有价值的技术知识，忽略寒暄和重复内容
- 保持简洁，总输出不超过 30 行
- 使用中文
- 项目: ${project}" \
        "提取以下会话的关键知识点:" 2>"$err_file")

    local exit_code=$?
    rm -f "$err_file"

    if [ $exit_code -eq 124 ]; then
        echo "**[摘要超时: claude CLI ${CLAUDE_TIMEOUT}s 未响应]**" >&2
        return 1
    fi

    if [ -n "$summary" ] && [ ${#summary} -gt 20 ]; then
        printf '%s' "$summary"
        return 0
    else
        return 1
    fi
}

# ============================================================
# 主逻辑 (所有处理输出到 stderr，只有最终 JSON 到 stdout)
# ============================================================
{
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
    # 从 transcript 路径提取会话 ID (文件名去掉扩展名)
    SESSION_ID=$(basename "$TRANSCRIPT_PATH" | sed 's/\.[^.]*$//')

    # 使用稳定的文件名: 同一会话始终覆盖同一个文件
    SESSION_FILE="$SESSIONS_DIR/${SESSION_ID}.md"
    IS_NEW_SESSION="false"

    if [ ! -f "$SESSION_FILE" ]; then
        IS_NEW_SESSION="true"
    fi

    TOTAL_LINES=$(wc -l < "$TRANSCRIPT_PATH" | tr -d ' ')
    FILE_SIZE=$(wc -c < "$TRANSCRIPT_PATH" | tr -d ' ')
    FILE_SIZE_KB=$((FILE_SIZE / 1024))

    # 提取对话文本
    CONVERSATION=$(extract_conversation "$TRANSCRIPT_PATH")
    CONV_LENGTH=${#CONVERSATION}

    if [ "$SUMMARIZE_ENABLED" = "true" ] && [ "$CONV_LENGTH" -gt 50 ]; then
        # ===== 新模式: LLM 摘要 =====
        SUMMARY=$(generate_summary "$CONVERSATION" "$PROJECT")

        if [ -n "$SUMMARY" ] && [ ${#SUMMARY} -gt 20 ]; then
            {
                echo "# 会话摘要: $SESSION_ID"
                echo ""
                echo "- **项目**: $PROJECT"
                echo "- **时间**: $DATE $TIME (最后更新)"
                echo "- **工作目录**: ${HOOK_CWD:-$PWD}"
                echo "- **原始大小**: ${FILE_SIZE_KB}KB (${TOTAL_LINES} 行)"
                echo "- **原始文件**: \`$TRANSCRIPT_PATH\`"
                echo ""
                echo "---"
                echo ""
                printf '%s\n' "$SUMMARY"
            } > "$SESSION_FILE"

            echo "[Memory] 会话摘要已保存: $SESSION_FILE ($(wc -c < "$SESSION_FILE" | tr -d ' ') bytes)"
        else
            # 摘要失败，降级保存对话文本
            {
                echo "# 会话记录: $SESSION_ID"
                echo ""
                echo "- **项目**: $PROJECT"
                echo "- **时间**: $DATE $TIME (最后更新)"
                echo "- **工作目录**: ${HOOK_CWD:-$PWD}"
                echo "- **原始大小**: ${FILE_SIZE_KB}KB (${TOTAL_LINES} 行)"
                echo "- **原始文件**: \`$TRANSCRIPT_PATH\`"
                echo ""
                echo "---"
                echo ""
                printf '%s\n' "$CONVERSATION" | head -200
            } > "$SESSION_FILE"

            echo "[Memory] 会话记录已保存 (摘要失败，降级模式): $SESSION_FILE"
        fi
    else
        # ===== 降级模式: 保存对话文本 (非原始 JSONL) =====
        {
            echo "# 会话记录: $SESSION_ID"
            echo ""
            echo "- **项目**: $PROJECT"
            echo "- **时间**: $DATE $TIME (最后更新)"
            echo "- **工作目录**: ${HOOK_CWD:-$PWD}"
            echo "- **原始大小**: ${FILE_SIZE_KB}KB (${TOTAL_LINES} 行)"
            echo "- **原始文件**: \`$TRANSCRIPT_PATH\`"
            echo ""
            echo "---"
            echo ""
            if [ "$CONV_LENGTH" -gt 0 ]; then
                printf '%s\n' "$CONVERSATION" | head -200
            else
                echo "> 无有效对话内容"
            fi
        } > "$SESSION_FILE"

        echo "[Memory] 会话记录已保存: $SESSION_FILE (${FILE_SIZE_KB}KB)"
    fi

    # 只在新会话时在每日笔记中添加索引（避免重复条目）
    if [ "$IS_NEW_SESSION" = "true" ]; then
        {
            echo "## $TIME - 项目: $PROJECT"
            echo "- **会话记录**: [sessions/${SESSION_ID}.md](sessions/${SESSION_ID}.md)"
            echo ""
        } >> "$DAILY_FILE"
    fi
fi
} >&2  # [MEDIUM-7 修复] 所有处理输出到 stderr

# 返回允许停止 (唯一的 stdout 输出)
echo '{"decision": "approve"}'
exit 0
