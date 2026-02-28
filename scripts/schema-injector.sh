#!/bin/bash
# schema-injector.sh - SubagentStart Hook
# 向设计相关 subagent (Plan/Explore/architect 等) 注入数据库 schema 查询指引
# 让 subagent 知道记忆系统有表结构信息可查
#
# 前提: 需要已运行 extract-schema.py 生成 schema-index.md
# 同会话只注入一次 (flag 文件控制)

PROJECTS_DIR="$HOME/.claude/memory/areas/projects"
SCHEMA_INDEX="$PROJECTS_DIR/shared-db/schema-index.md"
INPUT=$(cat)

SESSION_ID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

# session_id flag：同会话只注入一次
SID="${SESSION_ID:0:8}"
[ -z "$SID" ] && SID="$$"
FLAG_FILE="/tmp/claude-schema-${SID}"

# 已注入 → 静默放行
[ -f "$FLAG_FILE" ] && exit 0

# schema-index 不存在 → 放行
[ ! -f "$SCHEMA_INDEX" ] && exit 0

# 创建 flag
touch "$FLAG_FILE"

# 读取 schema 索引
schema_index=$(cat "$SCHEMA_INDEX")

# 构建查询指引
context=$(cat << GUIDE
## 数据库表结构查询指引

记忆系统中有数据库表结构索引可供查询。
设计和开发阶段需要了解表结构时，请按以下步骤查询（优先于读取代码中的 Entity/DAO）：

1. 根据下方索引确定业务域对应的前缀
2. Read ~/.claude/memory/areas/projects/shared-db/schema/{前缀}.md 获取详细表结构
3. 如果文件中标注了 [跨前缀 → xxx]，按需追加加载关联前缀文件

### Schema 索引

${schema_index}
GUIDE
)

# 注入到 subagent 上下文
printf '%s' "$context" | jq -Rs '{
    hookSpecificOutput: {
        hookEventName: "SubagentStart",
        additionalContext: .
    }
}'
exit 0
