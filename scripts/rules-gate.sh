#!/bin/bash
# rules-gate.sh - PreToolUse Hook (Edit|Write)
# 安全网：编辑项目文件时检查规范是否已加载，未加载则 deny 提示先读规范
#
# 与 rules-injector.sh 配合:
#   - rules-injector.sh 在 Read 时加载规范 + 创建 flag
#   - rules-gate.sh 在 Edit|Write 时检查 flag 是否存在
#   - 如果直接 Edit 而未 Read → deny，提示先读规范

PROJECTS_DIR="$HOME/.claude/memory/areas/projects"
CONFIG_FILE="$HOME/.claude/memory/config.json"
INPUT=$(cat)

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)

[ -z "$FILE_PATH" ] && exit 0

# 路径 → 项目名 (通用检测，与 rules-injector.sh 相同逻辑)
detect_project() {
    local file_path="$1"

    # 方式 1: 从 config.json 的 project_name_map 匹配
    if [ -f "$CONFIG_FILE" ]; then
        local mappings
        mappings=$(jq -r '.project_name_map // {} | to_entries[] | "\(.key)\t\(.value)"' "$CONFIG_FILE" 2>/dev/null)
        while IFS=$'\t' read -r dir_name project_name; do
            [ -z "$dir_name" ] && continue
            if [[ "$file_path" == *"/$dir_name/"* ]]; then
                if [ -f "$PROJECTS_DIR/$project_name/rules.md" ]; then
                    echo "$project_name"
                    return
                fi
            fi
        done <<< "$mappings"
    fi

    # 方式 2: 直接匹配有 rules.md 的项目目录名
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

project=$(detect_project "$FILE_PATH")
[ -z "$project" ] && exit 0

SID="${SESSION_ID:0:8}"
[ -z "$SID" ] && SID="$$"
FLAG_FILE="/tmp/claude-rules-${SID}-${project}"

# 已加载 → 放行
[ -f "$FLAG_FILE" ] && exit 0

rules_file="$PROJECTS_DIR/$project/rules.md"
[ ! -f "$rules_file" ] && exit 0

# 创建 flag 避免重复 deny
touch "$FLAG_FILE"

# deny + reason（告诉 Claude 去读规范）
reason="首次编辑 ${project} 项目文件但未加载规范。请先 Read ${rules_file} 了解项目开发规范，然后重试。"
printf '%s' "$reason" | jq -Rs '{
    hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: .
    }
}'
exit 0
