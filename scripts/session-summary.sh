#!/bin/bash
# session-summary.sh - 会话结束时提取摘要 + 自动知识提取
# 由 Stop 或 SessionEnd Hook 触发
# 策略: 从 JSONL transcript 提取对话文本 -> LLM 生成 JSON (摘要+知识) -> 分发写入
# 改进: 借鉴 OpenClaw 的知识提炼思路，将死存储转化为可检索知识

MEMORY_DIR="$HOME/.claude/memory"
DAILY_DIR="$MEMORY_DIR/daily"
SESSIONS_DIR="$MEMORY_DIR/sessions"
PROJECTS_DIR="$MEMORY_DIR/areas/projects"
MEMORY_FILE="$MEMORY_DIR/MEMORY.md"
JQ_FILTER="$HOME/.claude/scripts/lib/extract-conversation.jq"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
DAILY_FILE="$DAILY_DIR/$DATE.md"

# 配置
SUMMARIZE_ENABLED="${CLAUDE_SESSION_SUMMARIZE:-true}"  # 设为 false 可回退到原始保存
AUTO_EXTRACT="${CLAUDE_AUTO_EXTRACT:-true}"  # 知识提取开关 (独立于摘要)
MIN_CHARS_FOR_EXTRACT="${CLAUDE_SUMMARIZE_MIN_CHARS:-500}"  # 低于此字符数跳过知识提取
MAX_CONVERSATION_CHARS=30000  # 传给 LLM 的最大字符数
SUMMARY_MODEL="haiku"  # 用 Haiku 做摘要，性价比最高
CLAUDE_TIMEOUT=45  # claude CLI 超时秒数 (知识提取需要更多时间)
MAX_AVOIDANCES_PER_SESSION=5  # 每会话避坑经验硬上限
AVOIDANCE_BLOCKLIST="$HOME/.claude/scripts/lib/avoidance-blocklist.txt"  # 已工具化主题屏蔽词

# 项目名规范化映射 (LLM/目录名 → 标准名)
# 工作区名返回空字符串表示跳过 rules 写入
# 使用 case 兼容 macOS bash 3.2 (不支持 declare -A)
normalize_project_name() {
    local raw="$1"
    # 去除首尾空格
    raw=$(printf '%s' "$raw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    # 拒绝逗号分隔的组合名 (如 "project-a, project-b")
    case "$raw" in
        *,*) echo ""; return ;;
    esac

    # 从 config.json 读取项目名映射 (如果存在)
    local config_file="$MEMORY_DIR/config.json"
    if [ -f "$config_file" ] && command -v jq &>/dev/null; then
        local mapped
        mapped=$(jq -r --arg name "$raw" '.project_name_map[$name] // empty' "$config_file" 2>/dev/null)
        if [ -n "$mapped" ]; then
            echo "$mapped"
            return
        fi
    fi

    # 自动检测工作区: cwd/projects/ 下有多个子目录 → 当前目录是工作区不是项目
    local cwd="${HOOK_CWD:-$PWD}"
    local dir_name
    dir_name=$(basename "$cwd")
    if [ "$raw" = "$dir_name" ] && [ -d "$cwd/projects" ]; then
        local subdir_count
        subdir_count=$(find "$cwd/projects" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
        if [ "$subdir_count" -ge 2 ]; then
            echo ""
            return
        fi
    fi
    # 其他项目名原样返回
    echo "$raw"
}

# 避坑经验去重源: 所有可能包含 **避坑条目** 的文件
# 新增规范文件时在此追加，源头抑制和写入去重共用此列表
AVOIDANCE_DEDUP_SOURCES=(
    "$MEMORY_FILE"
    "$MEMORY_DIR/areas/projects"/*/rules.md
    "$MEMORY_DIR/areas/patterns"/*/summary.md
    "$HOME/.claude/rules/security.md"
)

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
# 模式1 (AUTO_EXTRACT=true): 输出 JSON (摘要+知识)
# 模式2 (AUTO_EXTRACT=false): 输出纯 Markdown 摘要
# 输入: 对话文本, 项目名
# 输出: JSON 或 Markdown (stdout)
# ============================================================
generate_summary() {
    local conversation="$1"
    local project="$2"
    local use_json="$3"  # "true" or "false"

    local truncated_conv
    truncated_conv=$(printf '%s' "$conversation" | cut -c1-"$MAX_CONVERSATION_CHARS")

    # 检查 claude CLI 是否可用
    if ! command -v claude &> /dev/null; then
        echo "**[摘要不可用: claude CLI 未安装]**"
        return 1
    fi

    # 构建已有避坑主题列表，注入到 prompt 中抑制重复提取 (扫描所有去重源文件)
    local existing_topics=""
    if [ ${#AVOIDANCE_DEDUP_SOURCES[@]} -gt 0 ]; then
        existing_topics=$(python3 -c "
import re, sys, glob
topics = []
for path in sys.argv[1:]:
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('- '):
                    continue
                # 匹配 **加粗** 格式 或 纯文本列表项的冒号前部分
                m = re.search(r'\*\*(.*?)\*\*', line)
                if m:
                    topics.append(m.group(1)[:50])
                else:
                    # '- 纯文本描述：解释' 或 '- 纯文本描述 (日期)'
                    text = re.sub(r'^- ', '', line)
                    text = re.split(r'[：:]', text)[0].strip()
                    if len(text) > 8:
                        topics.append(text[:50])
    except (FileNotFoundError, IsADirectoryError):
        continue
# 去重并限制数量
seen = set()
unique = []
for t in topics:
    key = t[:20]
    if key not in seen:
        seen.add(key)
        unique.append(t)
    if len(unique) >= 50:
        break
print('\n'.join('- ' + t for t in unique))
" "${AVOIDANCE_DEDUP_SOURCES[@]}" 2>/dev/null) || true
    fi

    local system_prompt
    if [ "$use_json" = "true" ]; then
        local existing_topics_block=""
        if [ -n "$existing_topics" ]; then
            existing_topics_block="

已有避坑经验（不要重复提取以下主题，如果会话中的避坑经验与下列任一条相似，则不要输出到 avoidances）:
${existing_topics}"
        fi
        system_prompt='你是一个会话摘要和知识提取器。从开发会话对话中提取摘要和可复用知识。

输出格式: 严格输出合法 JSON。直接以 { 开头，以 } 结尾。
禁止输出 ```json 或 ``` 标记。禁止输出任何解释文字。

{
  "summary": {
    "topic": "一句话描述会话主要目的",
    "key_points": ["要点1", "要点2", "要点3"],
    "files": ["被编辑或讨论的文件路径"],
    "decisions": ["重要技术决策"],
    "todos": ["未完成事项"]
  },
  "knowledge": {
    "has_knowledge": true,
    "project": "项目名(如能识别)或空字符串",
    "rules": [
      {"category": "分类名", "rule": "规则描述"}
    ],
    "avoidances": [
      "避坑经验描述"
    ]
  }
}

knowledge 提取规则:
- has_knowledge: 仅当会话中出现了**全新的**规范纠正、踩坑修复、最佳实践发现时为 true
- 闲聊、简单问答、无技术纠正的会话 → has_knowledge: false，rules 和 avoidances 为空数组
- rules: 从纠正、规范讨论中提取的可复用规则，category 常见值: 编码规范、禁止项、架构分层、缓存规范、API 规范、数据库规范
- avoidances: 踩过的坑、容易犯错的地方、需要特别注意的陷阱。**严格去重**: 如果经验与已有列表中的任一条相似，不要输出
- avoidances 硬上限: 最多输出 5 条，超过时只保留最有价值的 5 条
- 不要编造不存在的知识，只提取会话中明确出现的内容
- 使用中文
- 项目: '"$project"''"$existing_topics_block"
    else
        system_prompt='你是一个会话摘要提取器。从以下开发会话对话中提取关键知识点。

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
- 项目: '"$project"
    fi

    local result
    local err_file
    err_file=$(mktemp "${TMPDIR:-/tmp}/claude-summary-err.XXXXXX")

    # 构造完整 prompt: 将对话内容包裹在标签中，避免 LLM 把对话当指令执行
    local full_prompt
    full_prompt=$(printf '以下是一段开发会话的对话记录，请从中提取摘要和知识。\n\n<conversation>\n%s\n</conversation>\n\n请严格按照 system prompt 要求的格式输出。' "$truncated_conv")

    # macOS 没有 timeout 命令，用 perl 实现超时
    result=$(printf '%s' "$full_prompt" | perl -e '
        use POSIX ":sys_wait_h";
        $pid = open(my $fh, "-|", @ARGV) or exit 1;
        $SIG{ALRM} = sub { kill("TERM", $pid); sleep(1); kill("KILL", $pid); exit 124; };
        alarm('"$CLAUDE_TIMEOUT"');
        local $/; $out = <$fh>; close $fh;
        print $out;
    ' -- claude -p \
        --model "$SUMMARY_MODEL" \
        --no-session-persistence \
        --system-prompt "$system_prompt" 2>"$err_file")

    local exit_code=$?
    rm -f "$err_file"

    if [ $exit_code -eq 124 ]; then
        echo "**[摘要超时: claude CLI ${CLAUDE_TIMEOUT}s 未响应]**" >&2
        return 1
    fi

    if [ -n "$result" ] && [ ${#result} -gt 20 ]; then
        printf '%s' "$result"
        return 0
    else
        return 1
    fi
}

# ============================================================
# 从 JSON 摘要中格式化 Markdown 会话摘要
# 输入: JSON 字符串
# 输出: Markdown 格式文本 (stdout)
# ============================================================
format_summary_markdown() {
    local json="$1"

    local topic key_points files decisions todos
    topic=$(printf '%s' "$json" | jq -r '.summary.topic // "未知主题"')
    key_points=$(printf '%s' "$json" | jq -r '.summary.key_points // [] | .[] | "- " + .')
    files=$(printf '%s' "$json" | jq -r '.summary.files // [] | .[] | "- `" + . + "`"')
    decisions=$(printf '%s' "$json" | jq -r '.summary.decisions // [] | .[] | "- " + .')
    todos=$(printf '%s' "$json" | jq -r '.summary.todos // [] | .[] | "- [ ] " + .')

    echo "## 主题"
    echo ""
    echo "$topic"
    echo ""

    if [ -n "$key_points" ]; then
        echo "## 关键知识点"
        echo ""
        echo "$key_points"
        echo ""
    fi

    if [ -n "$files" ]; then
        echo "## 涉及文件"
        echo ""
        echo "$files"
        echo ""
    fi

    if [ -n "$decisions" ]; then
        echo "## 决策记录"
        echo ""
        echo "$decisions"
        echo ""
    fi

    if [ -n "$todos" ]; then
        echo "## 待办事项"
        echo ""
        echo "$todos"
        echo ""
    fi
}

# ============================================================
# 将提取的规则写入 rules.md (去重)
# 输入: JSON 字符串, 项目名
# ============================================================
write_rules() {
    local json="$1"
    local project="$2"

    local has_knowledge
    has_knowledge=$(printf '%s' "$json" | jq -r '.knowledge.has_knowledge // false')
    if [ "$has_knowledge" != "true" ]; then
        return 0
    fi

    local knowledge_project
    knowledge_project=$(printf '%s' "$json" | jq -r '.knowledge.project // empty')
    # 优先使用 knowledge 中识别的项目名，fallback 到会话项目名
    local target_project="${knowledge_project:-$project}"

    # 规范化项目名 (防止创建错误目录)
    target_project=$(normalize_project_name "$target_project")
    if [ -z "$target_project" ]; then
        echo "[Knowledge] 项目名为工作区或组合名，跳过 rules 写入" >&2
        return 0
    fi

    local rules_count
    rules_count=$(printf '%s' "$json" | jq -r '.knowledge.rules // [] | length')
    if [ "$rules_count" -eq 0 ]; then
        return 0
    fi

    local rules_dir="$PROJECTS_DIR/$target_project"
    local rules_file="$rules_dir/rules.md"

    # 确保目录存在
    mkdir -p "$rules_dir"

    # 如果 rules.md 不存在，创建带模板头
    if [ ! -f "$rules_file" ]; then
        cat > "$rules_file" << RULESEOF
# $target_project 开发规范

> 由 session-summary 自动提取，可手动编辑补充

RULESEOF
    fi

    # 逐条处理 rules
    local i=0
    while [ "$i" -lt "$rules_count" ]; do
        local category rule
        category=$(printf '%s' "$json" | jq -r ".knowledge.rules[$i].category // \"通用\"")
        rule=$(printf '%s' "$json" | jq -r ".knowledge.rules[$i].rule // empty")

        if [ -z "$rule" ]; then
            i=$((i + 1))
            continue
        fi

        # 净化: 移除换行符 (LLM 输出可能包含多行)
        rule=$(printf '%s' "$rule" | tr '\n\r' '  ')

        # 去重检查: 检查规则内容是否已存在
        if grep -qF "$rule" "$rules_file" 2>/dev/null; then
            echo "[Knowledge] 规则已存在，跳过: $rule" >&2
            i=$((i + 1))
            continue
        fi

        # 检查 category 区块是否存在
        if grep -qF "## $category" "$rules_file" 2>/dev/null; then
            # 在该区块末尾追加 (用环境变量传值避免正则注入)
            RULE_LINE="- $rule ($DATE)" CATEGORY_HEADER="## $category" \
                perl -i -0pe 's/(^\Q$ENV{CATEGORY_HEADER}\E\n(?:(?!^## ).)*)/$1$ENV{RULE_LINE}\n/ms' "$rules_file" 2>/dev/null
            if [ $? -ne 0 ]; then
                # perl 失败，直接追加到文件末尾
                printf '\n- %s (%s)\n' "$rule" "$DATE" >> "$rules_file"
            fi
        else
            # 区块不存在，在文件末尾新建
            {
                echo ""
                echo "## $category"
                echo ""
                echo "- $rule ($DATE)"
            } >> "$rules_file"
        fi

        echo "[Knowledge] 规则已写入 $rules_file: [$category] $rule" >&2
        i=$((i + 1))
    done
}

# ============================================================
# 将避坑经验写入 MEMORY.md (去重)
# 输入: JSON 字符串
# ============================================================
write_avoidances() {
    local json="$1"

    local has_knowledge
    has_knowledge=$(printf '%s' "$json" | jq -r '.knowledge.has_knowledge // false')
    if [ "$has_knowledge" != "true" ]; then
        return 0
    fi

    local avoidances_count
    avoidances_count=$(printf '%s' "$json" | jq -r '.knowledge.avoidances // [] | length')
    if [ "$avoidances_count" -eq 0 ]; then
        return 0
    fi

    # 确保 MEMORY.md 存在
    if [ ! -f "$MEMORY_FILE" ]; then
        echo "[Knowledge] MEMORY.md 不存在，跳过避坑经验写入" >&2
        return 0
    fi

    # 确保 "## 避坑经验" 区块存在
    if ! grep -qF "## 避坑经验" "$MEMORY_FILE" 2>/dev/null; then
        echo "[Knowledge] MEMORY.md 中无 '## 避坑经验' 区块，跳过" >&2
        return 0
    fi

    # 加载屏蔽词列表 (已工具化的主题)
    local blocklist_keywords=""
    if [ -f "$AVOIDANCE_BLOCKLIST" ]; then
        blocklist_keywords=$(grep -v '^#' "$AVOIDANCE_BLOCKLIST" | grep -v '^$' | tr '\n' '|')
        blocklist_keywords="${blocklist_keywords%|}"  # 去掉末尾 |
    fi

    local i=0
    local written_count=0
    while [ "$i" -lt "$avoidances_count" ]; do
        # 硬上限检查
        if [ "$written_count" -ge "$MAX_AVOIDANCES_PER_SESSION" ]; then
            local remaining=$((avoidances_count - i))
            echo "[Knowledge] 已达硬上限 ${MAX_AVOIDANCES_PER_SESSION} 条/会话，跳过剩余 ${remaining} 条" >&2
            break
        fi

        local avoidance
        avoidance=$(printf '%s' "$json" | jq -r ".knowledge.avoidances[$i] // empty")

        if [ -z "$avoidance" ]; then
            i=$((i + 1))
            continue
        fi

        # 净化: 移除换行符 (LLM 输出可能包含多行)
        avoidance=$(printf '%s' "$avoidance" | tr '\n\r' '  ')

        # 屏蔽词检测: 已工具化的主题直接跳过
        if [ -n "$blocklist_keywords" ]; then
            if printf '%s' "$avoidance" | grep -qiE "$blocklist_keywords" 2>/dev/null; then
                echo "[Knowledge] 已工具化主题，跳过: $avoidance" >&2
                i=$((i + 1))
                continue
            fi
        fi

        # 去重: trigram 多策略 (Jaccard + 包含度 + 子串检测), 扫描所有去重源文件
        local is_duplicate
        is_duplicate=$(printf '%s' "$avoidance" | python3 -c "
import sys, re
def trigrams(s):
    s = re.sub(r'\s+', '', s)
    return set(s[i:i+3] for i in range(max(0, len(s)-2)))
def jaccard(a, b):
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)
def containment(small, big):
    if not small: return 0.0
    return len(small & big) / len(small)
def normalize(s):
    return re.sub(r'\s+', '', s)

new_text = sys.stdin.read().strip()
new_norm = normalize(new_text)
new_tri = trigrams(new_text)
# 阈值说明: Jaccard 0.30 捕获同长度改写, Containment 0.55 捕获浓缩版改写
# 扫描所有去重源文件 (AVOIDANCE_DEDUP_SOURCES)
for filepath in sys.argv[1:]:
    try:
        with open(filepath, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('- '):
                    continue
                # 匹配 **加粗** 格式 或 纯文本列表项的冒号前部分
                m = re.search(r'\*\*(.*?)\*\*', line)
                if m:
                    existing_text = m.group(1)
                else:
                    text = re.sub(r'^- ', '', line)
                    existing_text = re.split(r'[：:]', text)[0].strip()
                    if len(existing_text) <= 8:
                        continue
                existing_tri = trigrams(existing_text)
                existing_norm = normalize(existing_text)
                if jaccard(new_tri, existing_tri) > 0.30:
                    print('duplicate')
                    sys.exit(0)
                if containment(new_tri, existing_tri) > 0.55:
                    print('duplicate')
                    sys.exit(0)
                if len(new_norm) > 10 and new_norm in existing_norm:
                    print('duplicate')
                    sys.exit(0)
    except (FileNotFoundError, IsADirectoryError):
        continue
print('unique')
" "${AVOIDANCE_DEDUP_SOURCES[@]}" 2>/dev/null) || is_duplicate="unique"

        if [ "$is_duplicate" = "duplicate" ]; then
            echo "[Knowledge] 避坑经验已存在(Jaccard匹配)，跳过: $avoidance" >&2
            i=$((i + 1))
            continue
        fi

        # 在 "## 避坑经验" 区块末尾追加 (在下一个 ## 之前)
        # 使用 perl 环境变量传值，避免特殊字符问题
        AVOIDANCE_LINE="- **${avoidance}** ($DATE, auto)" \
            perl -i -0777 -pe 's/(^## 避坑经验\n(?:(?!^## ).)*)/$1$ENV{AVOIDANCE_LINE}\n/ms' "$MEMORY_FILE" 2>/dev/null

        # 修复格式: 确保每个 ## 标题前有空行
        perl -i -pe 'print "\n" if /^## / && defined($prev) && $prev !~ /^\s*$/; $prev = $_' "$MEMORY_FILE" 2>/dev/null

        if [ $? -ne 0 ]; then
            # perl 失败的 fallback: 直接追加到文件末尾
            printf '\n- **%s** (%s, auto)\n' "$avoidance" "$DATE" >> "$MEMORY_FILE"
        fi

        echo "[Knowledge] 避坑经验已写入 MEMORY.md: $avoidance" >&2
        written_count=$((written_count + 1))
        i=$((i + 1))
    done

    if [ "$written_count" -gt 0 ]; then
        echo "[Knowledge] 本会话共写入 ${written_count} 条避坑经验 (上限 ${MAX_AVOIDANCES_PER_SESSION})" >&2
    fi
}

# ============================================================
# 将摘要追加到每日笔记 (增强版)
# 输入: JSON 字符串, SESSION_ID
# ============================================================
write_daily_entry() {
    local json="$1"
    local session_id="$2"

    local topic
    topic=$(printf '%s' "$json" | jq -r '.summary.topic // "未知主题"')
    local key_points
    key_points=$(printf '%s' "$json" | jq -r '.summary.key_points // [] | .[:3] | .[] | "  - " + .')

    local has_knowledge
    has_knowledge=$(printf '%s' "$json" | jq -r '.knowledge.has_knowledge // false')

    {
        echo "## $TIME - 项目: $PROJECT"
        echo "- **主题**: $topic"
        echo "- **会话记录**: [sessions/${session_id}.md](sessions/${session_id}.md)"
        if [ -n "$key_points" ]; then
            echo "- **要点**:"
            echo "$key_points"
        fi
        if [ "$has_knowledge" = "true" ]; then
            echo "- **知识提取**: 已自动写入 rules/MEMORY"
        fi
        echo ""
    } >> "$DAILY_FILE"
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
        # 判断是否启用知识提取 (需要 AUTO_EXTRACT=true 且对话足够长)
        USE_JSON="false"
        if [ "$AUTO_EXTRACT" = "true" ] && [ "$CONV_LENGTH" -gt "$MIN_CHARS_FOR_EXTRACT" ]; then
            USE_JSON="true"
        fi

        # ===== LLM 摘要 (JSON 或 Markdown 模式) =====
        LLM_OUTPUT=$(generate_summary "$CONVERSATION" "$PROJECT" "$USE_JSON")

        if [ -n "$LLM_OUTPUT" ] && [ ${#LLM_OUTPUT} -gt 20 ]; then
            if [ "$USE_JSON" = "true" ]; then
                # ===== JSON 模式: 解析并分发 =====
                # 从 LLM 输出中提取 JSON (处理可能的 markdown 代码块标记和前后文字)
                CLEANED_JSON=$(printf '%s' "$LLM_OUTPUT" | perl -0pe 's/.*?(\{)/$1/s; s/\}[^}]*$/}/s' 2>/dev/null)

                # 验证 JSON 有效性
                if printf '%s' "$CLEANED_JSON" | jq '.' > /dev/null 2>&1; then
                    echo "[Memory] JSON 解析成功，开始分发写入..." >&2

                    # 1. 格式化 Markdown 摘要 → sessions/
                    FORMATTED_SUMMARY=$(format_summary_markdown "$CLEANED_JSON")
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
                        printf '%s\n' "$FORMATTED_SUMMARY"
                    } > "$SESSION_FILE"
                    echo "[Memory] 会话摘要已保存: $SESSION_FILE" >&2

                    # 2. 写入 rules.md (静默失败)
                    write_rules "$CLEANED_JSON" "$PROJECT" || true

                    # 3. 写入 MEMORY.md 避坑经验 (静默失败)
                    write_avoidances "$CLEANED_JSON" || true

                    # 4. 增强版每日笔记
                    if [ "$IS_NEW_SESSION" = "true" ]; then
                        write_daily_entry "$CLEANED_JSON" "$SESSION_ID"
                    fi

                    # 5. 后台记录会话结果 (非阻塞, 反馈闭环)
                    if [ -n "$SESSION_ID" ] && [ -f "$HOME/.claude/scripts/memory-feedback.py" ]; then
                        local has_errors_flag="0"
                        printf '%s' "$CLEANED_JSON" | jq -e '.knowledge.avoidances | length > 0' >/dev/null 2>&1 && has_errors_flag="1"
                        local corrections_count
                        corrections_count=$(printf '%s' "$CLEANED_JSON" | jq -r '.knowledge.rules // [] | length' 2>/dev/null)
                        python3 "$HOME/.claude/scripts/memory-feedback.py" outcome \
                            --session-id "$SESSION_ID" --project "$PROJECT" \
                            --has-errors "$has_errors_flag" --user-corrections "${corrections_count:-0}" 2>/dev/null &
                    fi

                    # 6. 后台提取信号 (非阻塞, 信号分析)
                    if [ -n "$SESSION_ID" ] && [ -f "$HOME/.claude/scripts/signal-analyzer.py" ]; then
                        python3 "$HOME/.claude/scripts/signal-analyzer.py" extract \
                            --session-id "$SESSION_ID" --summary-file "$SESSION_FILE" 2>/dev/null &
                    fi
                else
                    # JSON 解析失败 → 降级为纯文本摘要
                    echo "[Memory] JSON 解析失败，降级为纯文本模式" >&2
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
                        printf '%s\n' "$LLM_OUTPUT"
                    } > "$SESSION_FILE"
                    echo "[Memory] 会话摘要已保存 (JSON降级): $SESSION_FILE" >&2

                    # 降级模式下仍写简单每日笔记
                    if [ "$IS_NEW_SESSION" = "true" ]; then
                        {
                            echo "## $TIME - 项目: $PROJECT"
                            echo "- **会话记录**: [sessions/${SESSION_ID}.md](sessions/${SESSION_ID}.md)"
                            echo ""
                        } >> "$DAILY_FILE"
                    fi
                fi
            else
                # ===== Markdown 模式 (AUTO_EXTRACT=false 或对话太短) =====
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
                    printf '%s\n' "$LLM_OUTPUT"
                } > "$SESSION_FILE"

                echo "[Memory] 会话摘要已保存: $SESSION_FILE ($(wc -c < "$SESSION_FILE" | tr -d ' ') bytes)" >&2

                # Markdown 模式下的简单每日笔记
                if [ "$IS_NEW_SESSION" = "true" ]; then
                    {
                        echo "## $TIME - 项目: $PROJECT"
                        echo "- **会话记录**: [sessions/${SESSION_ID}.md](sessions/${SESSION_ID}.md)"
                        echo ""
                    } >> "$DAILY_FILE"
                fi
            fi
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

            echo "[Memory] 会话记录已保存 (摘要失败，降级模式): $SESSION_FILE" >&2

            if [ "$IS_NEW_SESSION" = "true" ]; then
                {
                    echo "## $TIME - 项目: $PROJECT"
                    echo "- **会话记录**: [sessions/${SESSION_ID}.md](sessions/${SESSION_ID}.md)"
                    echo ""
                } >> "$DAILY_FILE"
            fi
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

        echo "[Memory] 会话记录已保存: $SESSION_FILE (${FILE_SIZE_KB}KB)" >&2

        if [ "$IS_NEW_SESSION" = "true" ]; then
            {
                echo "## $TIME - 项目: $PROJECT"
                echo "- **会话记录**: [sessions/${SESSION_ID}.md](sessions/${SESSION_ID}.md)"
                echo ""
            } >> "$DAILY_FILE"
        fi
    fi
fi
} >&2  # 所有处理输出到 stderr

# 返回允许停止 (唯一的 stdout 输出)
echo '{"decision": "approve"}'
exit 0
