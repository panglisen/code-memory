#!/bin/bash
# avoidance-gate.sh - PostToolUse Hook (Edit|Write)
# 统一避坑规则检测入口：根据文件路径分发到对应规则引擎
#
# 规则引擎 (Tier 1 - 始终开启):
#   lib/lint-antd-v3-rules.py   — 前端 React/antd 反模式
#   lib/lint-java-sql-rules.py  — Java DAO: SQL 反模式
#   lib/lint-shell-rules.py     — Shell: bash 3.2 兼容
#   lib/lint-python-rules.py    — Python: 3.9 兼容
#
# 语义检测 (Tier 2 - 默认关闭):
#   CLAUDE_SEMANTIC_LINT=true 时启用 LLM 语义检查
#
# 项目检测:
#   1. config.json 的 project_name_map 匹配目录名 → 项目名
#   2. 直接匹配 areas/projects/{name}/ 下有 rules.md 的项目名
#   前端项目名含 "front" → antd 规则; Java 项目 *Dao.java → SQL 规则
#   .sh/.py 文件 → 通用兼容规则

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$HOME/.claude/memory/areas/projects"
CONFIG_FILE="$HOME/.claude/memory/config.json"
PYTHON="/usr/bin/python3"

# 读 stdin (Hook 输入 JSON)
INPUT=$(cat)

# 提取 file_path
if command -v jq &> /dev/null; then
    FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)
else
    FILE_PATH=$(printf '%s' "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*: *"\([^"]*\)"/\1/')
    if [ -z "$FILE_PATH" ]; then
        FILE_PATH=$(printf '%s' "$INPUT" | grep -o '"filePath"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*: *"\([^"]*\)"/\1/')
    fi
fi

# 无文件路径 → 放行
[ -z "$FILE_PATH" ] && exit 0

# Python 不可用 → 放行
[ ! -x "$PYTHON" ] && exit 0

# 检测项目名 (用于确定前端/后端项目类型)
detect_project() {
    local file_path="$1"

    # 方式 1: config.json project_name_map
    if [ -f "$CONFIG_FILE" ] && command -v jq &>/dev/null; then
        local mappings
        mappings=$(jq -r '.project_name_map // {} | to_entries[] | "\(.key)\t\(.value)"' "$CONFIG_FILE" 2>/dev/null)
        while IFS=$'\t' read -r dir_name project_name; do
            [ -z "$dir_name" ] && continue
            if [[ "$file_path" == *"/$dir_name/"* ]]; then
                echo "$project_name"
                return
            fi
        done <<< "$mappings"
    fi

    # 方式 2: 直接匹配 areas/projects/ 下的项目名
    for rules_file in "$PROJECTS_DIR"/*/rules.md; do
        [ -f "$rules_file" ] || continue
        local project_name
        project_name=$(basename "$(dirname "$rules_file")")
        [[ "$project_name" == shared-* ]] && continue
        if [[ "$file_path" == *"/$project_name/"* ]]; then
            echo "$project_name"
            return
        fi
    done

    echo ""
}

# 检查项目是否为前端项目 (用于 antd 规则)
is_frontend_project() {
    local project="$1"
    # 项目名含 "front" 或 config.json 中标记为前端
    if [[ "$project" == *front* ]]; then
        return 0
    fi
    # 检查 config.json 中的 frontend_projects 列表
    if [ -f "$CONFIG_FILE" ] && command -v jq &>/dev/null; then
        local is_fe
        is_fe=$(jq -r --arg p "$project" '.frontend_projects // [] | index($p) // empty' "$CONFIG_FILE" 2>/dev/null)
        [ -n "$is_fe" ] && return 0
    fi
    return 1
}

# 路由: 根据文件类型和项目选择规则引擎
RULE_ENGINE=""

case "$FILE_PATH" in
    # JavaScript/JSX: 前端 antd 规则 (需要匹配前端项目)
    *.js|*.jsx)
        PROJECT=$(detect_project "$FILE_PATH")
        if [ -n "$PROJECT" ] && is_frontend_project "$PROJECT"; then
            if [[ "$FILE_PATH" == */src/* ]]; then
                RULE_ENGINE="$SCRIPT_DIR/lib/lint-antd-v3-rules.py"
            fi
        fi
        ;;
    # Java DAO/Mapper: SQL 规则
    *Dao.java|*Mapper.java|*DAO.java)
        RULE_ENGINE="$SCRIPT_DIR/lib/lint-java-sql-rules.py"
        ;;
    # Shell 脚本: bash 3.2 兼容规则
    *.sh)
        RULE_ENGINE="$SCRIPT_DIR/lib/lint-shell-rules.py"
        ;;
    # Python 脚本: 3.9 兼容规则
    *.py)
        RULE_ENGINE="$SCRIPT_DIR/lib/lint-python-rules.py"
        ;;
    *)
        exit 0
        ;;
esac

# 无匹配规则引擎 → 放行
[ -z "$RULE_ENGINE" ] && exit 0

# 规则引擎文件不存在 → 放行
if [ ! -f "$RULE_ENGINE" ]; then
    echo "[avoidance-gate] 规则引擎不存在: $RULE_ENGINE" >&2
    exit 0
fi

# 调用规则引擎
RESULT=$("$PYTHON" "$RULE_ENGINE" "$FILE_PATH" 2>/dev/null)

if [ -z "$RESULT" ]; then
    exit 0
fi

# 解析结果
DECISION=$(printf '%s' "$RESULT" | jq -r '.decision // "approve"' 2>/dev/null)
REASON=$(printf '%s' "$RESULT" | jq -r '.reason // ""' 2>/dev/null)
WARNINGS=$(printf '%s' "$RESULT" | jq -r '.warnings // [] | .[]' 2>/dev/null)

# 输出 warnings 到 stderr
if [ -n "$WARNINGS" ]; then
    ENGINE_NAME=$(basename "$RULE_ENGINE" .py | sed 's/lint-//' | sed 's/-rules//')
    echo "" >&2
    echo "⚠ [avoidance: $ENGINE_NAME] 检测到潜在问题 ($FILE_PATH):" >&2
    printf '%s\n' "$WARNINGS" | while read -r line; do
        echo "  $line" >&2
    done
    echo "" >&2
fi

if [ "$DECISION" = "block" ]; then
    printf '%s' "$RESULT"
    exit 0
fi

# Tier 2: 语义检测 (默认关闭)
if [ "$CLAUDE_SEMANTIC_LINT" = "true" ]; then
    SEMANTIC="$SCRIPT_DIR/semantic-lint.sh"
    if [ -f "$SEMANTIC" ] && [ -x "$SEMANTIC" ]; then
        SEMANTIC_RESULT=$(printf '%s' "$INPUT" | "$SEMANTIC" 2>/dev/null)
        if [ -n "$SEMANTIC_RESULT" ]; then
            S_DECISION=$(printf '%s' "$SEMANTIC_RESULT" | jq -r '.decision // "approve"' 2>/dev/null)
            if [ "$S_DECISION" = "block" ]; then
                printf '%s' "$SEMANTIC_RESULT"
                exit 0
            fi
        fi
    fi
fi

exit 0
