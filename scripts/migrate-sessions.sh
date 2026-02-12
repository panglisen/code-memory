#!/bin/bash
# migrate-sessions.sh - 将现有 sessions/ 中的原始 transcript 批量转换为 LLM 摘要
# 用法: ~/.claude/scripts/migrate-sessions.sh [--dry-run] [--limit N]
#
# 优先从原始 JSONL transcript 文件提取对话（更可靠），
# 如果原始文件不存在，则从 session .md 中的嵌入 JSONL 提取。
# 转换后单个 session 文件从 ~1MB 降至 ~2KB。

# 不使用 set -e: 迁移脚本需要容错，单个文件失败不应中断整个迁移

SESSIONS_DIR="$HOME/.claude/memory/sessions"
PROJECTS_DIR="$HOME/.claude/projects"
BACKUP_DIR="$HOME/.claude/memory/sessions-backup-$(date +%Y%m%d-%H%M%S)"
JQ_FILTER="$HOME/.claude/scripts/lib/extract-conversation.jq"
SUMMARY_MODEL="haiku"
MAX_CONVERSATION_CHARS=30000
DRY_RUN="false"
LIMIT=0  # 0 = 无限制
PROCESSED=0
SKIPPED=0
FAILED=0

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run) DRY_RUN="true"; shift ;;
        --limit) LIMIT="$2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

echo "=========================================="
echo "  Sessions 迁移工具"
echo "=========================================="
echo ""
echo "模式: $([ "$DRY_RUN" = "true" ] && echo "预览" || echo "执行")"
[ "$LIMIT" -gt 0 ] && echo "限制: 最多处理 $LIMIT 个文件"
echo ""

# 检查依赖
if ! command -v jq &> /dev/null; then
    echo "错误: 需要 jq (brew install jq)"
    exit 1
fi

if ! command -v claude &> /dev/null; then
    echo "错误: 需要 claude CLI"
    exit 1
fi

# 统计当前状态
TOTAL_FILES=$(find "$SESSIONS_DIR" -name "*.md" -type f | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "$SESSIONS_DIR" 2>/dev/null | cut -f1)
echo "当前: $TOTAL_FILES 个文件, 共 $TOTAL_SIZE"
echo ""

# 备份 (每次运行都创建新备份，带时间戳)
if [ "$DRY_RUN" = "false" ]; then
    echo "创建备份: $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    cp "$SESSIONS_DIR"/*.md "$BACKUP_DIR/" 2>/dev/null || true
    echo "备份完成: $(du -sh "$BACKUP_DIR" | cut -f1)"
    echo ""
fi

# ============================================================
# 从 JSONL 文件提取对话文本 (使用共享 jq 过滤器)
# ============================================================
extract_from_jsonl() {
    local jsonl_file="$1"
    if [ -f "$JQ_FILTER" ]; then
        jq -r -f "$JQ_FILTER" "$jsonl_file" 2>/dev/null
    else
        jq -r '
            select(.type == "user" or .type == "assistant") |
            if .type == "user" then
                "用户: " + (if .message.content then
                    if (.message.content | type) == "string" then .message.content
                    elif (.message.content | type) == "array" then
                        [.message.content[] | select(.type == "text") | .text] | join("\n")
                    else "" end else "" end)
            elif .type == "assistant" then
                "助手: " + (if .message.content then
                    if (.message.content | type) == "array" then
                        [.message.content[] | select(.type == "text") | .text] | join("\n")
                    else (.message.content | tostring) end else "" end)
            else empty end |
            select(length > 5)
        ' "$jsonl_file" 2>/dev/null
    fi
}

# ============================================================
# 从 session .md 文件提取对话文本 (fallback)
# [HIGH-3 修复] 使用更精确的 '^{"type":' 模式匹配 JSONL 起始行
# ============================================================
extract_from_session_md() {
    local file="$1"

    # 从 session 文件中找到 JSONL 起始行 (跳过 Markdown header)
    local jsonl_start
    jsonl_start=$(grep -n '^{"type":' "$file" | head -1 | cut -d: -f1)

    if [ -z "$jsonl_start" ]; then
        return 1
    fi

    if [ -f "$JQ_FILTER" ]; then
        tail -n +"$jsonl_start" "$file" | jq -r -f "$JQ_FILTER" 2>/dev/null
    else
        tail -n +"$jsonl_start" "$file" | jq -r '
            select(.type == "user" or .type == "assistant") |
            if .type == "user" then
                "用户: " + (if .message.content then
                    if (.message.content | type) == "string" then .message.content
                    elif (.message.content | type) == "array" then
                        [.message.content[] | select(.type == "text") | .text] | join("\n")
                    else "" end else "" end)
            elif .type == "assistant" then
                "助手: " + (if .message.content then
                    if (.message.content | type) == "array" then
                        [.message.content[] | select(.type == "text") | .text] | join("\n")
                    else (.message.content | tostring) end else "" end)
            else empty end |
            select(length > 5)
        ' 2>/dev/null
    fi
}

# ============================================================
# 查找 session ID 对应的原始 JSONL transcript
# ============================================================
find_original_jsonl() {
    local session_id="$1"
    find "$PROJECTS_DIR" -name "${session_id}.jsonl" -type f 2>/dev/null | head -1
}

# 处理每个文件
for session_file in "$SESSIONS_DIR"/*.md; do
    [ -f "$session_file" ] || continue

    filename=$(basename "$session_file")
    session_id="${filename%.md}"
    file_size=$(wc -c < "$session_file" | tr -d ' ')
    file_size_kb=$((file_size / 1024))

    # 跳过已经是摘要格式的
    if head -1 "$session_file" | grep -q "^# 会话摘要:"; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # 跳过太小的文件 (< 1KB，可能没有实质内容)
    if [ "$file_size" -lt 1024 ]; then
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    echo "处理: $filename (${file_size_kb}KB)"

    if [ "$DRY_RUN" = "true" ]; then
        PROCESSED=$((PROCESSED + 1))
        [ "$LIMIT" -gt 0 ] && [ "$PROCESSED" -ge "$LIMIT" ] && break
        continue
    fi

    # 优先从原始 JSONL 提取
    conversation=""
    original_jsonl=$(find_original_jsonl "$session_id")

    if [ -n "$original_jsonl" ] && [ -f "$original_jsonl" ]; then
        echo "  -> 使用原始 JSONL: $(basename "$(dirname "$original_jsonl")")/$(basename "$original_jsonl")"
        conversation=$(extract_from_jsonl "$original_jsonl")
    fi

    # Fallback: 从 session .md 提取
    if [ -z "$conversation" ] || [ ${#conversation} -lt 50 ]; then
        echo "  -> Fallback: 从 session .md 提取"
        conversation=$(extract_from_session_md "$session_file")
    fi

    if [ -z "$conversation" ] || [ ${#conversation} -lt 50 ]; then
        echo "  -> 跳过: 无有效对话内容"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # 提取元数据 (从 header 中)
    project=$(grep "^\- \*\*项目\*\*:" "$session_file" | head -1 | sed 's/.*: //')
    workdir=$(grep "^\- \*\*工作目录\*\*:" "$session_file" | head -1 | sed 's/.*: //')
    timestamp=$(grep "^\- \*\*时间\*\*:" "$session_file" | head -1 | sed 's/.*: //')

    # 生成摘要
    truncated_conv=$(printf '%s' "$conversation" | head -c "$MAX_CONVERSATION_CHARS")
    summary=$(printf '%s' "$truncated_conv" | claude -p \
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
- 项目: ${project:-unknown}" \
        "提取以下会话的关键知识点:" 2>/dev/null) || true

    if [ -n "$summary" ] && [ ${#summary} -gt 20 ]; then
        # 写入摘要格式
        {
            echo "# 会话摘要: $session_id"
            echo ""
            echo "- **项目**: ${project:-未知}"
            echo "- **时间**: ${timestamp:-未知}"
            echo "- **工作目录**: ${workdir:-未知}"
            echo "- **原始大小**: ${file_size_kb}KB"
            if [ -n "$original_jsonl" ]; then
                echo "- **原始文件**: \`$original_jsonl\`"
            fi
            echo ""
            echo "---"
            echo ""
            echo "$summary"
        } > "$session_file"

        new_size=$(wc -c < "$session_file" | tr -d ' ')
        new_size_kb=$((new_size / 1024))
        echo "  -> 完成: ${file_size_kb}KB -> ${new_size_kb}KB (压缩 $((file_size_kb - new_size_kb))KB)"
        PROCESSED=$((PROCESSED + 1))
    else
        echo "  -> 失败: LLM 摘要生成失败"
        FAILED=$((FAILED + 1))
    fi

    [ "$LIMIT" -gt 0 ] && [ "$PROCESSED" -ge "$LIMIT" ] && break

    # 每个文件间隔 1 秒，避免 API 限流
    sleep 1
done

echo ""
echo "=========================================="
echo "  迁移完成"
echo "=========================================="
echo "- 处理: $PROCESSED 个"
echo "- 跳过: $SKIPPED 个"
echo "- 失败: $FAILED 个"

if [ "$DRY_RUN" = "false" ]; then
    NEW_SIZE=$(du -sh "$SESSIONS_DIR" 2>/dev/null | cut -f1)
    echo "- 新大小: $NEW_SIZE (原: $TOTAL_SIZE)"
    [ -d "$BACKUP_DIR" ] && echo "- 备份位置: $BACKUP_DIR"
fi
