# claude-memory

基于文件系统的 Claude Code 三层记忆系统，零外部服务依赖。

```
┌──────────────────────────────────────────────────────────┐
│                    Claude Code 会话                       │
│                                                          │
│  ┌─ 必加载 ─────────────────────────────────────────┐    │
│  │  MEMORY.md (偏好) + rules.md (项目规范)          │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─ 按需加载 (三级检索) ────────────────────────────┐    │
│  │  一级: 精确规则路由 (触发词 → 文件)              │    │
│  │  二级: RRF 混合搜索 (BM25 + 向量 → 融合排序)    │    │
│  │  三级: 纯向量语义搜索 (自然语言描述)             │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─ 自动写入 (Hooks) ──────────────────────────────┐    │
│  │  PostToolUse → 每日笔记  |  Stop → 会话摘要     │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘

                    ~/.claude/memory/
                          │
          ┌───────────────┼───────────────┐
          │               │               │
    Layer 1: areas/   Layer 2: daily/   Layer 3:
    (知识图谱)        + sessions/      MEMORY.md
    原子事实           时间线日志       隐性知识
    项目规范           会话摘要         偏好模式
    代码模式           (LLM 提炼)
```

## 核心特性

- **三层记忆架构** — 知识图谱 + 时间线日志 + 隐性知识，按需加载节省上下文
- **RRF 混合搜索** — BM25 关键词 + 向量语义双路检索，Reciprocal Rank Fusion 融合
- **Hook 驱动自动化** — 文件编辑自动记录每日笔记，会话结束自动生成 LLM 摘要
- **斜杠命令** — `/memory-add`, `/memory-learn`, `/memory-avoid`, `/memory-summarize`
- **DB Schema 提取** — ast-grep + sqlglot 从 Java 代码提取 JOIN/DAO，MySQL 表结构按前缀分组
- **零外部服务** — 纯文件系统 + SQLite，不依赖任何云服务或数据库
- **优雅降级** — 未安装 fastembed 时自动回退纯 BM25 搜索

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/panglisen/claude-memory.git
cd claude-memory
```

### 2. 复制文件到 `~/.claude/`

```bash
# 脚本
mkdir -p ~/.claude/scripts/lib
cp scripts/*.py scripts/*.sh ~/.claude/scripts/
cp scripts/lib/*.jq ~/.claude/scripts/lib/

# 斜杠命令
mkdir -p ~/.claude/commands
cp commands/*.md ~/.claude/commands/

# 加载规则 (复制到 rules/ 目录，Claude Code 会自动读取)
mkdir -p ~/.claude/rules
cp rules/memory-loader.md ~/.claude/rules/

# 初始化记忆目录
mkdir -p ~/.claude/memory/{daily,sessions,areas/{projects,patterns,tools}}
```

### 3. 配置 Hooks

将 `config/settings.example.json` 中的 hooks 部分合并到你的 `~/.claude/settings.json`:

```bash
# 查看示例配置
cat config/settings.example.json
```

手动将 `hooks` 字段合并到你已有的 `~/.claude/settings.json` 中。如果你还没有该文件，可以直接复制:

```bash
cp config/settings.example.json ~/.claude/settings.json
```

### 4. 初始化记忆文件

```bash
# 从模板创建 MEMORY.md (编辑填入你的偏好)
cp templates/MEMORY.md ~/.claude/memory/MEMORY.md

# (可选) 配置项目映射
cp config/project-config.example.json ~/.claude/memory/config.json
# 编辑 config.json 填入你的项目名映射
```

### 5. 验证安装

```bash
# 测试搜索脚本
python3 ~/.claude/scripts/memory-search.py --help

# (可选) 安装向量搜索依赖
pip install fastembed  # ~200MB，含 ONNX Runtime + bge 模型

# 构建搜索索引
python3 ~/.claude/scripts/memory-search.py --rebuild
```

## 目录结构

```
claude-memory/
├── README.md                          # 本文档
├── LICENSE                            # MIT
├── .gitignore
├── docs/
│   └── architecture.md                # 三层架构设计文档
├── scripts/
│   ├── memory-search.py               # RRF 混合搜索 (BM25 + 向量)
│   ├── session-summary.sh             # Stop Hook: 会话摘要
│   ├── extract-memory.sh              # PostToolUse Hook: 编辑记录
│   ├── weekly-consolidate.sh          # 周期整理
│   ├── auto-extract-facts.py          # 自动事实提取 (LLM 驱动)
│   ├── extract-schema.py              # DB Schema 提取 (ast-grep + sqlglot)
│   ├── migrate-sessions.sh            # 历史会话迁移工具
│   └── lib/
│       └── extract-conversation.jq    # JSONL 对话提取
├── commands/
│   ├── memory-add.md                  # /memory-add 添加原子事实
│   ├── memory-learn.md                # /memory-learn 学习代码模式
│   ├── memory-avoid.md                # /memory-avoid 记录避坑经验
│   └── memory-summarize.md            # /memory-summarize 会话提炼
├── rules/
│   └── memory-loader.md               # 三级检索加载规则
├── config/
│   ├── settings.example.json          # Hook 配置示例
│   └── project-config.example.json    # 项目映射配置示例
└── templates/
    ├── MEMORY.md                      # 隐性知识模板
    ├── project-summary.md             # 项目 summary 模板
    ├── project-facts.json             # facts.json 空模板
    └── daily.md                       # 每日笔记模板
```

## 工作原理

### 自动化流程

**1. 文件编辑后 (PostToolUse Hook)**

```
Claude Code 编辑文件
    → extract-memory.sh
    → 记录到 daily/YYYY-MM-DD.md
    → 同一分钟内去重合并
```

**2. 会话结束时 (Stop Hook)**

```
Claude Code 会话结束
    → session-summary.sh
    → 从 JSONL transcript 提取对话
    → LLM (Haiku) 生成摘要
    → 保存到 sessions/{id}.md (~2KB)
    → 在每日笔记中添加索引
```

**3. 周期整理 (手动或 cron)**

```
weekly-consolidate.sh
    → 统计本周活动
    → 分析高频编辑文件
    → 刷新搜索索引
    → 自动事实提取 (auto-extract-facts.py)
    → 清理过期文件
```

### 搜索架构

```
查询 "积分不一致"
     │
     ├─ BM25 路径 (SQLite FTS5)
     │  └─ unicode61 tokenizer → rank → top-20
     │
     ├─ 向量路径 (fastembed, 可选)
     │  ├─ bge-small-zh-v1.5 → query embedding (384d)
     │  └─ cosine similarity → top-20
     │
     └─ RRF 融合
        ├─ score = 1/(60+rank_bm25) + 1/(60+rank_vec)
        ├─ Jaccard trigram 去重 (阈值 0.83)
        └─ 返回 top-6
```

## 配置说明

### Hooks 配置

在 `~/.claude/settings.json` 中添加:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/extract-memory.sh"}]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/session-summary.sh"}]
      }
    ]
  }
}
```

### 项目映射配置

创建 `~/.claude/memory/config.json`:

```json
{
  "project_name_map": {
    "my-backend-parent": "backend-api"
  },
  "ignored_projects": ["mac", "git-repo"],
  "domain_hints": {
    "app_user": {"label": "用户", "keywords": ["用户表", "会员"]}
  },
  "special_prefixes": [],
  "source_labels": {}
}
```

- `project_name_map` — 代码仓库目录名到记忆系统项目名的映射
- `ignored_projects` — 忽略的目录名（非代码项目）
- `domain_hints` — Schema 提取时的业务域中文关键词映射
- `special_prefixes` — 表名中包含下划线但应视为整体的前缀
- `source_labels` — 源码路径到项目标签的映射

## 斜杠命令

| 命令 | 用途 | 示例 |
|------|------|------|
| `/memory-add` | 添加原子事实 | `/memory-add projects/myapp "WebSocket 需要心跳保活"` |
| `/memory-learn` | 学习代码模式 | `/memory-learn retry-backoff "指数退避重试，最大 30s"` |
| `/memory-avoid` | 记录避坑经验 | `/memory-avoid "macOS sed -i 需要空字符串参数"` |
| `/memory-summarize` | 提炼会话知识 | `/memory-summarize` |

## Schema 提取工具

`extract-schema.py` 从 MySQL 数据库和 Java 源码中自动提取表结构、JOIN 关系和 DAO 映射。

### 依赖

```bash
brew install ast-grep
pip3 install pymysql sqlglot
```

### 用法

```bash
python3 ~/.claude/scripts/extract-schema.py \
  --db-host 127.0.0.1 --db-port 3306 \
  --db-user root --db-password xxx --db-name my_database \
  --sources /path/to/project-a,/path/to/project-b \
  --source-labels project-a,project-b \
  --project shared-db \
  --dry-run
```

### 特性

- **分表折叠** — 自动识别 `user_main_1402` 类分表，折叠为逻辑表
- **前缀分组** — 按表名前缀自动分组，生成独立的 schema 文件
- **ast-grep 代码分析** — 从 `@Select/@Insert/@Update/@Delete` 注解中提取 SQL
- **sqlglot 解析** — 精确提取表名和 JOIN 关系，正则降级兜底
- **跨前缀标注** — JOIN 跨越不同前缀组时自动标注 `[跨前缀 → xxx]`

## 依赖

**必需:**
- Python 3.10+
- jq (用于 JSONL 解析)
- Claude CLI (`claude` 命令，用于 LLM 摘要)

**可选 (增强搜索):**
- `pip install fastembed` — 向量搜索 (BM25 + 向量 RRF 融合)

**可选 (Schema 提取):**
- `brew install ast-grep` — Java 代码 AST 分析
- `pip3 install pymysql sqlglot` — MySQL 连接和 SQL 解析

## 致谢

- [OpenClaw](https://github.com/PrajnaAvidya/openclaw) — 混合检索参数调优和知识提炼思路的启发来源
- RRF 算法参考: Cormack, Clarke & Butt (2009) "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods"

## License

MIT
