#!/bin/bash
# avoidance-gate.sh - PostToolUse Hook (Edit|Write)
# 统一避坑规则检测入口：根据文件路径分发到对应规则引擎
#
# 规则引擎 (Tier 1 - 始终开启):
#   lib/lint-antd-v3-rules.py   — scrm-front: antd v3 反模式 (5 条)
#   lib/lint-java-sql-rules.py  — Java DAO: SQL 反模式 (4 条)
#   lib/lint-shell-rules.py     — Shell: bash 3.2 兼容 (2 条)
#   lib/lint-python-rules.py    — Python: 3.9 兼容 (2 条)
#   lib/lint-auto-rules.py      — 自动生成规则 (始终运行, 所有文件类型)
#
# 语义检测 (Tier 2 - 默认关闭):
#   semantic-lint.sh — CLAUDE_SEMANTIC_LINT=true 时启用 LLM 语义检查

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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

# --- Tier 1a: 特定规则引擎 (按文件类型路由) ---
RULE_ENGINE=""

case "$FILE_PATH" in
    # scrm-front: antd v3 规则
    */mantis-scrm-front/src/*.js|*/mantis-scrm-front/src/*.jsx)
        RULE_ENGINE="$SCRIPT_DIR/lib/lint-antd-v3-rules.py"
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
esac

# 运行特定引擎
SPECIFIC_RESULT=""
if [ -n "$RULE_ENGINE" ] && [ -f "$RULE_ENGINE" ]; then
    SPECIFIC_RESULT=$("$PYTHON" "$RULE_ENGINE" "$FILE_PATH" 2>/dev/null)
fi

# --- Tier 1b: auto-rules 引擎 (始终运行, 所有文件类型) ---
AUTO_RULES="$SCRIPT_DIR/lib/lint-auto-rules.py"
AUTO_RESULT=""
if [ -f "$AUTO_RULES" ]; then
    AUTO_RESULT=$("$PYTHON" "$AUTO_RULES" "$FILE_PATH" 2>/dev/null)
fi

# --- 合并结果 ---
# 逻辑: 任一引擎 block → block; 合并所有 warnings
FINAL_DECISION="approve"
FINAL_REASON=""
ALL_WARNINGS=""

# 处理特定引擎结果
if [ -n "$SPECIFIC_RESULT" ]; then
    S_DECISION=$(printf '%s' "$SPECIFIC_RESULT" | jq -r '.decision // "approve"' 2>/dev/null)
    S_REASON=$(printf '%s' "$SPECIFIC_RESULT" | jq -r '.reason // ""' 2>/dev/null)
    S_WARNINGS=$(printf '%s' "$SPECIFIC_RESULT" | jq -r '.warnings // [] | .[]' 2>/dev/null)

    if [ "$S_DECISION" = "block" ]; then
        FINAL_DECISION="block"
        FINAL_REASON="$S_REASON"
    fi

    if [ -n "$S_WARNINGS" ]; then
        ENGINE_NAME=$(basename "$RULE_ENGINE" .py | sed 's/lint-//' | sed 's/-rules//')
        echo "" >&2
        echo "⚠ [avoidance: $ENGINE_NAME] 检测到潜在问题 ($FILE_PATH):" >&2
        printf '%s\n' "$S_WARNINGS" | while read -r line; do
            echo "  $line" >&2
        done
        echo "" >&2
        ALL_WARNINGS="$S_WARNINGS"
    fi
fi

# 处理 auto-rules 引擎结果
if [ -n "$AUTO_RESULT" ]; then
    A_DECISION=$(printf '%s' "$AUTO_RESULT" | jq -r '.decision // "approve"' 2>/dev/null)
    A_REASON=$(printf '%s' "$AUTO_RESULT" | jq -r '.reason // ""' 2>/dev/null)
    A_WARNINGS=$(printf '%s' "$AUTO_RESULT" | jq -r '.warnings // [] | .[]' 2>/dev/null)

    if [ "$A_DECISION" = "block" ]; then
        FINAL_DECISION="block"
        if [ -n "$FINAL_REASON" ]; then
            FINAL_REASON="$FINAL_REASON"$'\n'"$A_REASON"
        else
            FINAL_REASON="$A_REASON"
        fi
    fi

    if [ -n "$A_WARNINGS" ]; then
        echo "" >&2
        echo "⚠ [avoidance: auto-rules] 检测到潜在问题 ($FILE_PATH):" >&2
        printf '%s\n' "$A_WARNINGS" | while read -r line; do
            echo "  $line" >&2
        done
        echo "" >&2
        if [ -n "$ALL_WARNINGS" ]; then
            ALL_WARNINGS="$ALL_WARNINGS"$'\n'"$A_WARNINGS"
        else
            ALL_WARNINGS="$A_WARNINGS"
        fi
    fi
fi

# 如果有 block，输出合并后的 JSON 结果
if [ "$FINAL_DECISION" = "block" ]; then
    # 构造合并的 warnings JSON 数组
    WARNINGS_JSON="[]"
    if [ -n "$ALL_WARNINGS" ]; then
        WARNINGS_JSON=$(printf '%s\n' "$ALL_WARNINGS" | jq -R -s 'split("\n") | map(select(length > 0))' 2>/dev/null)
        if [ -z "$WARNINGS_JSON" ]; then
            WARNINGS_JSON="[]"
        fi
    fi
    printf '{"decision":"block","reason":"%s","warnings":%s}' \
        "$(printf '%s' "$FINAL_REASON" | jq -R -s '.' | sed 's/^"//;s/"$//')" \
        "$WARNINGS_JSON"
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
