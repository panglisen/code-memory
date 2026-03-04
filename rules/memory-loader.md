# 记忆系统加载规则

> 指导 Claude 如何按需加载记忆文件
> 核心原则: 节省上下文，只加载当前需要的内容

## 项目规范加载

项目规范（rules.md）通过 Hook 自动加载，无需手动操作：

1. **PreToolUse Read**: `rules-injector.sh` — 首次读取项目文件时自动通过 additionalContext 注入规范
2. **PreToolUse Edit|Write**: `rules-gate.sh` — 安全网，未加载规范直接编辑时 deny 提示先读规范
3. **PreCompact**: `pre-compact.sh` — 上下文压缩后清除 flag，下次 Read 自动重新注入

## 按需加载 (出现相关话题时才读取)

**一级: 确定性路由 (精确匹配)**

| 触发条件 | 加载文件 |
|----------|----------|
| 涉及项目架构、技术选型 | `projects/{项目}/summary.md` |
| 需要查看项目具体事实 | `projects/{项目}/facts.json` |
| 涉及特定编码模式 | `patterns/{模式名}/summary.md` |
| 使用特定工具遇到问题 | `tools/{工具名}/summary.md` |
| 涉及数据库/SQL/DAO（泛泛提及） | `projects/shared-db/schema-index.md` |
| 提到具体表名 | 先查 `projects/shared-db/prefix-map.json` 定位前缀，再加载 `projects/shared-db/schema/{前缀}.md` |
| 涉及业务需求分析、功能设计、影响分析 | `domains/index.md` — 业务域索引，定位改动影响域 |
| 会话开始时（如存在） | `evolution/signal_report_latest.md` — 最新信号分析报告 |

> 注: 子 agent (Plan/Explore/planner/architect) 启动时，`schema-injector.sh` 会自动注入 schema-index.md，无需手动加载。

**二级: RRF 混合搜索 (Fallback)**

当一级规则表无法匹配时，使用混合搜索:

```bash
python3 ~/.claude/scripts/memory-search.py "搜索关键词"           # 默认混合 (BM25 + 向量)
python3 ~/.claude/scripts/memory-search.py -c rules "搜索关键词"  # 按分类过滤
python3 ~/.claude/scripts/memory-search.py --mode bm25 "ClassName"    # 精确匹配
python3 ~/.claude/scripts/memory-search.py --mode vector "用户标签关系" # 语义搜索
# 分类: schema, rules, project-summary, facts, pattern, tool, daily, session, memory
```

搜索模式选择:
- **hybrid (默认)**: BM25 + 向量 → RRF 融合，两路都命中的结果排名提升
- **bm25**: 精确匹配类名、表名、配置项
- **vector**: 语义搜索，BM25 无结果时尝试

**数据库 schema 加载（主会话）:**

多个项目共享同一个数据库时，schema 按表名前缀分组存放在 `projects/shared-db/` 下。

1. 用户提到数据库相关话题 → 加载 `schema-index.md`（~40行，含前缀→业务域映射）
2. 确定具体表名后 → 查 `prefix-map.json` 定位前缀文件
3. 加载 `schema/{前缀}.md`（50-150行，含表清单、JOIN关系、DAO映射）
4. 如标注 `[跨前缀 → xxx]`，按需追加加载关联前缀文件

不涉及数据库时不加载任何 schema 文件。

### 不主动加载

| 文件 | 加载时机 |
|------|----------|
| `daily/*.md` | 仅当用户问"最近做了什么"、"昨天做了什么" |
| `sessions/*.md` | 仅当用户问"上次会话内容"或需要回溯具体对话 |
| `facts.json` | 仅当 `summary.md` 信息不够需要细节时 |

## 经验引用反馈

每次从 MEMORY.md 或 rules.md 中实际引用某条经验辅助决策时，后台记录引用（不阻塞主流程）:
```bash
python3 ~/.claude/scripts/memory-feedback.py ref \
    --experience-id "avoidance:<hash>" --session-id "<当前会话ID>" --context "引用上下文"
```

## 原子事实管理

### 事实状态
- `active` - 当前有效
- `superseded` - 已被新事实替代
- `archived` - 已归档不再使用

### 替代规则
- 新事实不删除旧事实
- 旧事实标记为 `superseded`
- 保留 `supersededBy` 追溯链

## 目录结构

```
~/.claude/memory/
├── MEMORY.md              # 隐性知识 ← 自动加载
├── .search-index.db       # 搜索索引 (SQLite)
├── daily/                 # 每日笔记
│   └── YYYY-MM-DD.md
├── sessions/              # 会话摘要
│   └── {session-id}.md
├── evolution/             # 自进化系统数据
│   ├── auto-rules.json          # 自动蒸馏的检测规则
│   ├── audit.jsonl              # 经验引用审计日志
│   ├── capabilities.jsonl       # 能力生成审计日志
│   ├── generator_state.json     # 能力生成器状态
│   └── signal_report_latest.md  # 最新信号分析报告
└── areas/                 # 知识图谱
    ├── projects/          # 项目知识
    │   ├── {name}/
    │   │   ├── summary.md
    │   │   ├── rules.md   # ← Hook 自动注入 (当前项目)
    │   │   └── facts.json
    │   └── shared-db/         # 共享数据库 schema
    │       ├── schema-index.md    # 前缀→业务域索引
    │       ├── prefix-map.json    # 表名→前缀文件映射
    │       └── schema/            # 按前缀分组的详细 schema
    │           └── {前缀}.md
    ├── domains/           # 业务域知识
    │   ├── index.md           # 关键词→场景文件映射
    │   └── {业务域}/
    │       └── scenario.md    # 业务场景文档
    ├── patterns/          # 代码模式
    │   └── {name}/
    │       ├── summary.md
    │       └── facts.json
    └── tools/             # 工具经验
        └── {name}/
            ├── summary.md
            └── facts.json
```
