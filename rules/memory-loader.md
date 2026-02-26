# 记忆系统加载规则

> 指导 Claude Code 如何按需加载和使用三层记忆系统
> 核心原则: 节省上下文，只加载当前需要的内容

## 三层架构

| 层 | 存储位置 | 用途 |
|----|----------|------|
| Layer 1 | `areas/` (知识图谱) | 原子事实 + 动态摘要 + 项目规范 |
| Layer 2 | `daily/` + `sessions/` | 时间线事件日志 + 完整会话记录 |
| Layer 3 | `MEMORY.md` | 隐性知识、模式、偏好 |

## 按需加载策略

### 必加载 (会话开始时)

1. `~/.claude/memory/MEMORY.md` — 用户偏好和通用习惯
2. 当前项目的 `rules.md` — 识别规则如下:

**项目识别规则:**

- **普通项目**: `basename $PWD` 即为项目名，加载 `projects/{项目名}/rules.md`
- **需求工作区**: 如果当前目录包含 `projects/` 子目录（内含多个代码项目），
  则读取工作区 CLAUDE.md 中的项目索引表，加载**所有子项目**的 rules.md

  例: 在 `my-workspace/` 目录下，检测到 `projects/` 子目录包含:
  - `backend-service-a` → 加载 `projects/project-a/rules.md`
  - `backend-service-b` → 加载 `projects/project-b/rules.md`
  - `frontend-app` → 加载 `projects/frontend-app/rules.md`
  - `worker-agent` → 加载 `projects/project-c/rules.md`

  只加载存在的 rules.md，不存在则跳过

### 按需加载 (出现相关话题时才读取)

**一级: 确定性路由 (精确匹配)**

| 触发条件 | 加载文件 |
|----------|----------|
| 涉及项目架构、技术选型 | `projects/{项目}/summary.md` |
| 需要查看项目具体事实 | `projects/{项目}/facts.json` |
| 涉及特定编码模式 | `patterns/{模式名}/summary.md` |
| 使用特定工具遇到问题 | `tools/{工具名}/summary.md` |
| 涉及数据库/SQL/DAO（泛泛提及） | `projects/shared-db/schema-index.md` |
| 提到具体表名 | 先查 `projects/shared-db/prefix-map.json` 定位前缀，再加载 `projects/shared-db/schema/{前缀}.md` |
| 会话开始时（如存在） | `evolution/signal_report_latest.md` — 最新信号分析报告 |
| 用户问"记忆系统状态" | 运行 `python3 ~/.claude/scripts/memory-feedback.py report` |
| 用户问"循环问题" | 运行 `python3 ~/.claude/scripts/signal-analyzer.py recurring` |
| 用户问"能力生成" | 运行 `python3 ~/.claude/scripts/capability-generator.py --status` |

**二级: RRF 混合搜索 (Fallback)**

当一级规则表无法匹配时（用户提到的概念不在触发词列表中），使用混合搜索:

```bash
# 默认混合模式 (BM25 + 向量 → RRF 融合)
python3 ~/.claude/scripts/memory-search.py "搜索关键词"
# 按分类过滤
python3 ~/.claude/scripts/memory-search.py -c rules "搜索关键词"
# 仅关键词搜索 (精确匹配场景，如表名、类名)
python3 ~/.claude/scripts/memory-search.py --mode bm25 "ClassName"
# 仅语义搜索 (模糊概念场景，BM25 无结果时尝试)
python3 ~/.claude/scripts/memory-search.py --mode vector "用户标签关系"
# 分类: schema, rules, project-summary, facts, pattern, tool, daily, session, memory
```

触发条件:
- 用户提问涉及记忆系统中可能存在的知识，但不匹配上述任何精确触发条件
- 用户搜索某个业务概念、历史决策、或特定文件的上下文信息
- 需要在 sessions 或 daily 中查找历史记录

搜索模式选择:
- **hybrid (默认)**: BM25 关键词 + 向量语义 → RRF 融合排序，两路都命中的结果排名提升
- **bm25**: 精确关键词匹配，适合搜索类名、表名、配置项等确定性内容
- **vector**: 纯语义搜索，适合 BM25 搜不到时用自然语言描述搜索

搜索工具特性:
- BM25 层零依赖 (SQLite FTS5)，向量层需 fastembed (ONNX Runtime)
- 向量模型: BAAI/bge-small-zh-v1.5 (中文优化, 384d)
- 增量索引: 首次搜索自动建索引，后续按文件 hash 增量更新
- RRF 融合: `score = Σ 1/(60 + rank_i)`，Jaccard trigram 去重 (阈值 0.83)
- 返回 6 条最相关结果 (含 snippet)
- 优雅降级: fastembed 不可用时自动回退纯 BM25
- 重建索引: `python3 ~/.claude/scripts/memory-search.py --rebuild`

**数据库 schema 三级加载说明（shared-db 模式）:**

多个项目（project-a, project-b, project-c）共享同一个数据库时，
schema 统一存放在 `projects/shared-db/` 下，按表名前缀分组。

- **第一级（索引）**: 用户提到"数据库"、"表"、"建表"、"DAO"、"SQL"等关键词时，
  加载 `projects/shared-db/schema-index.md`（~40行），含前缀→业务域映射和中文搜索提示
- **第二级（定位）**: 确定具体表名后，查 `projects/shared-db/prefix-map.json` 找到所属前缀文件
- **第三级（详情）**: 加载 `projects/shared-db/schema/{前缀}.md`（50-150行），含表清单、JOIN关系、DAO映射
- **跨前缀追加**: 如果文件中标注了 `[跨前缀 → xxx]`，按需追加加载关联前缀文件
- 不涉及数据库时不加载任何 schema 文件

**注意**: 如果之前各项目有单独的 schema 文件（`projects/project-a/schema/`、
`projects/project-b/schema/`），迁移到 shared-db 后应废弃旧文件，
统一使用 `projects/shared-db/` 下的文件。

### 不主动加载

| 文件 | 加载时机 |
|------|----------|
| `daily/*.md` | 仅当用户问"最近做了什么"、"昨天做了什么" |
| `sessions/*.md` | 仅当用户问"上次会话内容"或需要回溯具体对话 |
| `facts.json` | 仅当 `summary.md` 信息不够需要细节时 |

## 经验引用反馈

每次从 MEMORY.md 或 rules.md 中实际引用某条经验辅助决策时，
后台记录引用（不阻塞主流程）:
```bash
python3 ~/.claude/scripts/memory-feedback.py ref \
    --experience-id "avoidance:<hash>" --session-id "<当前会话ID>" --context "引用上下文"
```

## 记忆写入时机

### 自动写入 (Hooks 触发，不消耗 token)
- 文件编辑后 → `extract-memory.sh` 更新每日笔记 (PostToolUse)
- 会话结束时 → `session-summary.sh` 保存 transcript (Stop/SessionEnd)
- 会话结束时 → `memory-feedback.py outcome` 记录会话结果 (由 session-summary.sh 后台调用)
- 会话结束时 → `signal-analyzer.py extract` 提取会话信号 (由 session-summary.sh 后台调用)

### 手动写入 (用户触发，消耗 token)
- `/memory-summarize` - 提取规范、知识、总结会话
- `/memory-add <area/entity> <fact>` - 添加原子事实
- `/memory-learn <name> <desc>` - 添加代码模式
- `/memory-avoid <desc>` - 添加避坑经验

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
├── MEMORY.md              # Layer 3: 隐性知识 ← 必加载
├── .search-index.db       # BM25 + 向量混合索引 + 反馈追踪表 (SQLite)
├── daily/                 # Layer 2: 每日笔记
│   └── YYYY-MM-DD.md
├── sessions/              # Layer 2: 会话摘要 (LLM 提炼的知识点)
│   └── {session-id}.md
├── evolution/             # 自进化系统数据
│   ├── audit.jsonl              # 经验引用 append-only 审计日志
│   ├── capabilities.jsonl       # 能力生成审计日志
│   ├── generator_state.json     # 能力生成器状态
│   └── signal_report_latest.md  # 最新信号分析报告 (按需加载)
└── areas/                 # Layer 1: 知识图谱
    ├── projects/          # 项目知识
    │   ├── {name}/
    │   │   ├── summary.md
    │   │   ├── rules.md   # ← 必加载 (当前项目)
    │   │   └── facts.json
    │   └── shared-db/         # ← 共享数据库 schema (涉及DB时加载)
    │       ├── schema-index.md    # 前缀→业务域索引 (~40行)
    │       ├── prefix-map.json    # 表名→前缀文件映射
    │       └── schema/            # 按前缀分组的详细 schema
    │           └── {前缀}.md      # 50-150行/文件
    ├── patterns/          # 代码模式
    │   └── {name}/
    │       ├── summary.md
    │       └── facts.json
    └── tools/             # 工具经验
        └── {name}/
            ├── summary.md
            └── facts.json
```

## 周期性整理

每周日运行 `~/.claude/scripts/weekly-consolidate.sh`:
1. 扫描本周每日笔记
2. 提取重复出现的模式
3. 更新 MEMORY.md 的最佳实践
4. 自动清理超过 30 天的会话文件
5. 清理超过 90 天的每日笔记
6. 标记过时的原子事实
7. 跨会话信号分析 (循环问题检测)
8. 能力自动生成 (从循环模式生成 Skill/Command)

用法: `~/.claude/scripts/weekly-consolidate.sh [--dry-run]`
