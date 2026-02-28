#!/bin/bash
# pre-compact.sh - PreCompact Hook
# 上下文压缩前清除规范注入 flag，确保压缩后下次 Read 自动重新注入
#
# 原理:
#   1. 上下文压缩会丢失 additionalContext 注入的规范内容
#   2. 清除 flag 后，下次 Read 项目文件时 rules-injector.sh 会重新注入
#   3. schema flag 也一并清除，subagent 重启时会重新注入

INPUT=$(cat)
SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

SID="${SESSION_ID:0:8}"
[ -z "$SID" ] && SID="$$"

# 清除当前会话的所有 flag
rm -f "/tmp/claude-rules-${SID}-"* 2>/dev/null
rm -f "/tmp/claude-schema-${SID}" 2>/dev/null

exit 0
