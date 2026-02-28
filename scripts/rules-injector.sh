#!/bin/bash
# rules-injector.sh - PreToolUse Hook (Read)
# 首次读取已知项目文件时，通过 additionalContext 注入项目开发规范（不 block）
# 后续读取同项目文件 → 静默放行（exit 0 无输出）
#
# 项目检测方式:
#   1. 从 config.json 的 project_name_map 匹配目录名 → 项目名
#   2. 直接匹配 areas/projects/{name}/ 下有 rules.md 的项目名
#
# 共享规范:
#   config.json 的 project_groups 定义项目组，如:
#   { "shared-java": ["backend-api", "worker-service"] }
#   匹配到 backend-api 时，会追加加载 shared-java/rules.md

PROJECTS_DIR="$HOME/.claude/memory/areas/projects"
CONFIG_FILE="$HOME/.claude/memory/config.json"
INPUT=$(cat)

# 提取 session_id 和 file_path
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)

# 无 file_path → 放行
[ -z "$FILE_PATH" ] && exit 0

# 路径 → 项目名 (通用检测)
detect_project() {
    local file_path="$1"

    # 方式 1: 从 config.json 的 project_name_map 匹配
    if [ -f "$CONFIG_FILE" ]; then
        local mappings
        mappings=$(jq -r '.project_name_map // {} | to_entries[] | "\(.key)\t\(.value)"' "$CONFIG_FILE" 2>/dev/null)
        while IFS=$'\t' read -r dir_name project_name; do
            [ -z "$dir_name" ] && continue
            if [[ "$file_path" == *"/$dir_name/"* ]]; then
                # 确认 rules.md 存在
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
        # 跳过 shared- 开头的组规范
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

# session_id 前 8 位作为标识
SID="${SESSION_ID:0:8}"
[ -z "$SID" ] && SID="$$"
FLAG_FILE="/tmp/claude-rules-${SID}-${project}"

# 已加载 → 静默放行
[ -f "$FLAG_FILE" ] && exit 0

# 检查 rules.md 是否存在
rules_file="$PROJECTS_DIR/$project/rules.md"
[ ! -f "$rules_file" ] && exit 0

# 创建 flag
touch "$FLAG_FILE"

# 读取规范内容
rules_content=$(cat "$rules_file")

# 检查是否有共享组规范 (project_groups)
shared_content=""
if [ -f "$CONFIG_FILE" ]; then
    # 查找包含当前项目的组
    local_groups=$(jq -r --arg proj "$project" '
        .project_groups // {} | to_entries[] |
        select(.value | index($proj)) | .key
    ' "$CONFIG_FILE" 2>/dev/null)

    while IFS= read -r group_name; do
        [ -z "$group_name" ] && continue
        shared_file="$PROJECTS_DIR/$group_name/rules.md"
        if [ -f "$shared_file" ]; then
            shared_content="${shared_content}$(printf '\n\n--- %s 通用规范 ---\n\n%s' "$group_name" "$(cat "$shared_file")")"
        fi
    done <<< "$local_groups"
fi

# allow + additionalContext 注入规范（零浪费，Read 正常执行）
context=$(printf '已加载 %s 项目开发规范，请遵循以下规范编写代码：\n\n%s%s' \
    "$project" "$rules_content" "$shared_content")

printf '%s' "$context" | jq -Rs '{
    hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "allow",
        additionalContext: .
    }
}'
exit 0
