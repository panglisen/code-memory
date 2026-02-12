#!/bin/bash
# extract-memory.sh - 提取会话中的关键信息到每日笔记
# 由 PostToolUse Hook 在文件编辑后触发
# 改进: 同一分钟内同项目的编辑合并为一条，避免重复

# 配置
MEMORY_DIR="$HOME/.claude/memory"
DAILY_DIR="$MEMORY_DIR/daily"

# 获取当前日期和时间
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
DAILY_FILE="$DAILY_DIR/$DATE.md"

# 获取项目名称
PROJECT=$(basename "$PWD")

# 从 stdin 读取 JSON (Claude Code Hook 传入)
INPUT=$(cat)

# 尝试解析 JSON 获取文件路径
if command -v jq &> /dev/null; then
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.filePath // ""' 2>/dev/null || echo "")
else
    FILE_PATH=$(echo "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*: *"\([^"]*\)"/\1/' || echo "")
fi

# 提取文件名
FILE_NAME=""
if [ -n "$FILE_PATH" ]; then
    FILE_NAME=$(basename "$FILE_PATH")
fi

# 确保目录存在
mkdir -p "$DAILY_DIR"

# 创建每日笔记（如不存在）
if [ ! -f "$DAILY_FILE" ]; then
    cat > "$DAILY_FILE" << EOF
# $DATE

> 自动生成的每日开发笔记

EOF
fi

# 去重逻辑: 检查最近是否已有同一分钟、同项目的条目
# 如果有，尝试追加文件名到已有条目；如果没有，创建新条目
LAST_ENTRY_KEY="## $TIME - 项目: $PROJECT"

if grep -qF "$LAST_ENTRY_KEY" "$DAILY_FILE" 2>/dev/null; then
    # 同一分钟、同项目已有条目
    if [ -n "$FILE_NAME" ]; then
        # 检查该文件名是否已记录
        if ! grep -qF "\`$FILE_NAME\`" "$DAILY_FILE" 2>/dev/null; then
            # 找到 **文件** 行的行号并追加
            # 使用 grep -n 找到该条目下的 **文件** 行
            LINE_NUM=$(grep -n "^- \*\*文件\*\*:" "$DAILY_FILE" | tail -1 | cut -d: -f1)
            if [ -n "$LINE_NUM" ]; then
                if [[ "$OSTYPE" == "darwin"* ]]; then
                    sed -i '' "${LINE_NUM}s/$/, \`$FILE_NAME\`/" "$DAILY_FILE"
                else
                    sed -i "${LINE_NUM}s/$/, \`$FILE_NAME\`/" "$DAILY_FILE"
                fi
            fi
        fi
    fi
else
    # 新的时间段，创建新条目
    {
        echo "$LAST_ENTRY_KEY"
        if [ -n "$FILE_NAME" ]; then
            echo "- **文件**: \`$FILE_NAME\`"
        else
            echo "- **操作**: 文件编辑"
        fi
        echo ""
    } >> "$DAILY_FILE"
fi

# 静默退出，不干扰主流程
exit 0
