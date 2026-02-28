#!/bin/bash
# semantic-lint.sh - Tier 2 语义避坑检测 (默认关闭)
#
# 使用 LLM (haiku) 检测代码编辑是否违反语义级规则。
# 仅在 CLAUDE_SEMANTIC_LINT=true 时启用。
#
# 由 avoidance-gate.sh 调用，stdin 接收 PostToolUse JSON 输入。
# 输出: JSON {"decision": "block"|"approve", "reason": "..."} 或无输出 (放行)

# 默认关闭
[ "$CLAUDE_SEMANTIC_LINT" != "true" ] && exit 0

# 检查 claude CLI 是否可用
if ! command -v claude &>/dev/null; then
    echo "[semantic-lint] claude CLI 不可用，跳过语义检测" >&2
    exit 0
fi

INPUT=$(cat)

# 提取编辑信息
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)

[ -z "$FILE_PATH" ] && exit 0

# 根据工具类型提取变更内容
if [ "$TOOL_NAME" = "Edit" ]; then
    OLD_STRING=$(printf '%s' "$INPUT" | jq -r '.tool_input.old_string // empty' 2>/dev/null)
    NEW_STRING=$(printf '%s' "$INPUT" | jq -r '.tool_input.new_string // empty' 2>/dev/null)
    CHANGE_DESC="文件: $FILE_PATH\n旧代码:\n$OLD_STRING\n\n新代码:\n$NEW_STRING"
elif [ "$TOOL_NAME" = "Write" ]; then
    # Write 的 content 可能很大，截取前 2000 字符
    CONTENT=$(printf '%s' "$INPUT" | jq -r '.tool_input.content // empty' 2>/dev/null | head -c 2000)
    CHANGE_DESC="文件: $FILE_PATH\n写入内容 (前 2000 字符):\n$CONTENT"
else
    exit 0
fi

# 语义规则列表 (可扩展)
RULES=$(cat << 'RULES_END'
1. 不要在前端直接调用API进行数据转换（如gid→nickname），应在后端批量处理后一起返回
2. try-catch 中不要混合业务逻辑和导航跳转，应按关键性分离异步副作用
3. 表单验证需包含业务规则完整性校验，不仅仅是 required 基础校验
4. 复合数据保存需全链路对齐：收集→序列化→API存储→反序列化→回填
5. 确认弹框文案应动态生成，不要写死导致不同场景下文案不符
6. 不要为新增功能创建新的 MQ topic，应先评估现有 topic 是否可复用
7. 新增 UI 配置字段时务必同步核实后端是否有对应的消费逻辑
RULES_END
)

PROMPT=$(cat << PROMPT_END
你是一个代码审查助手。检查以下代码编辑是否违反了这些规则：

$RULES

编辑内容：
$CHANGE_DESC

严格检查。如果明确违反了某条规则，返回：
{"ok": false, "reason": "违反规则N: 具体说明"}

如果没有违反或无法确定，返回：
{"ok": true}

只返回 JSON，不要其他内容。
PROMPT_END
)

# 调用 claude CLI (haiku, 15s 超时)
RESULT=$(echo "$PROMPT" | timeout 15 claude --print --model haiku -p 2>/dev/null)

if [ -z "$RESULT" ]; then
    exit 0
fi

# 尝试解析 LLM 返回的 JSON
OK=$(printf '%s' "$RESULT" | jq -r '.ok // true' 2>/dev/null)
REASON=$(printf '%s' "$RESULT" | jq -r '.reason // empty' 2>/dev/null)

if [ "$OK" = "false" ] && [ -n "$REASON" ]; then
    echo "[semantic-lint] $REASON" >&2
    jq -n --arg reason "$REASON" '{
        "decision": "block",
        "reason": ("[semantic-lint] " + $reason)
    }'
    exit 0
fi

exit 0
