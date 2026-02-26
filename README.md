# code-memory

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
│                                                          │
│  ┌─ 自进化反馈 ────────────────────────────────────┐    │
│  │  经验引用追踪 → 信号分析 → 能力自动生成         │    │
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
                          │
                    evolution/
                    (自进化闭环)
                    反馈追踪
                    信号分析
                    能力生成
```

## 核心特性

- **三层记忆架构** — 知识图谱 + 时间线日志 + 隐性知识，按需加载节省上下文
- **RRF 混合搜索** — BM25 关键词 + 向量语义双路检索，Reciprocal Rank Fusion 融合
- **Hook 驱动自动化** — 文件编辑自动记录每日笔记，会话结束自动生成摘要 + 知识提取分发
- **自进化系统** — 经验有效性追踪 (Laplace 平滑 + 指数衰减)、跨会话信号分析 (循环问题检测)、能力自动生成 (从循环模式自动生成 Skill/Command)
- **斜杠命令** — `/memory-add`, `/memory-learn`, `/memory-avoid`, `/memory-summarize`, `/memory-health`, `/memory-signals`, `/memory-generate`
- **DB Schema 提取** — ast-grep + sqlglot 从 Java 代码提取 JOIN/DAO，MySQL 表结构按前缀分组
- **零外部服务** — 纯文件系统 + SQLite，不依赖任何云服务或数据库
- **优雅降级** — 未安装 fastembed 时自动回退纯 BM25 搜索

## 快速开始

### 一键安装

```bash
git clone https://github.com/panglisen/code-memory.git
cd code-memory
bash install.sh
```

脚本会自动完成:
- 复制脚本、斜杠命令、加载规则到 `~/.claude/`
- 创建记忆目录结构 (含 `evolution/` 自进化数据目录)
- 从模板初始化 `MEMORY.md` 和 `config.json` (已有文件不覆盖)
- 合并 Hooks 到 `~/.claude/settings.json` (已有配置不覆盖)
- 检查依赖并验证安装

安装完成后:

```bash
# 1. 编辑填入你的偏好
vim ~/.claude/memory/MEMORY.md

# 2. (可选) 配置项目映射
vim ~/.claude/memory/config.json

# 3. (可选) 安装向量搜索依赖
pip install fastembed  # ~200MB，启用 BM25 + 向量 RRF 混合搜索

# 4. 构建搜索索引
python3 ~/.claude/scripts/memory-search.py --rebuild
```

卸载: `bash install.sh --uninstall` (记忆数据保留，需手动删除)

<details>
<summary>手动安装 (不使用 install.sh)</summary>

```bash
git clone https://github.com/panglisen/code-memory.git
cd code-memory

# 脚本
mkdir -p ~/.claude/scripts/lib
cp scripts/*.py scripts/*.sh ~/.claude/scripts/
cp scripts/lib/*.jq ~/.claude/scripts/lib/
chmod +x ~/.claude/scripts/*.sh ~/.claude/scripts/*.py

# 斜杠命令
mkdir -p ~/.claude/commands
cp commands/*.md ~/.claude/commands/

# 加载规则
mkdir -p ~/.claude/rules
cp rules/memory-loader.md ~/.claude/rules/
cp rules/auto-capabilities.md ~/.claude/rules/

# 初始化记忆目录
mkdir -p ~/.claude/memory/{daily,sessions,evolution,areas/{projects,patterns,tools}}

# 模板文件
cp templates/MEMORY.md ~/.claude/memory/MEMORY.md
cp config/project-config.example.json ~/.claude/memory/config.json

# Hooks (手动合并到你的 settings.json)
cat config/settings.example.json
```

</details>

## 目录结构

```
code-memory/
├── README.md                          # 本文档
├── LICENSE                            # MIT
├── install.sh                         # 一键安装/卸载脚本
├── .gitignore
├── docs/
│   └── architecture.md                # 三层架构设计文档
├── scripts/
│   ├── memory-search.py               # RRF 混合搜索 (BM25 + 向量)
│   ├── session-summary.sh             # Stop Hook: 会话摘要 + 知识提取
│   ├── extract-memory.sh              # PostToolUse Hook: 编辑记录
│   ├── weekly-consolidate.sh          # 周期整理 + 信号分析 + 能力生成
│   ├── auto-extract-facts.py          # 自动事实提取 (LLM 驱动)
│   ├── extract-schema.py              # DB Schema 提取 (ast-grep + sqlglot)
│   ├── migrate-sessions.sh            # 历史会话迁移工具
│   ├── memory-feedback.py             # 经验有效性追踪 (Laplace + 指数衰减)
│   ├── signal-analyzer.py             # 跨会话信号分析 (循环问题检测)
│   ├── capability-generator.py        # 能力自动生成 (Skill/Command)
│   ├── cleanup-avoidances.sh          # 避坑经验去重整理
│   └── lib/
│       └── extract-conversation.jq    # JSONL 对话提取
├── commands/
│   ├── memory-add.md                  # /memory-add 添加原子事实
│   ├── memory-learn.md                # /memory-learn 学习代码模式
│   ├── memory-avoid.md                # /memory-avoid 记录避坑经验
│   ├── memory-summarize.md            # /memory-summarize 会话提炼
│   ├── memory-health.md               # /memory-health 健康看板
│   ├── memory-signals.md              # /memory-signals 信号分析
│   └── memory-generate.md             # /memory-generate 能力生成
├── rules/
│   ├── memory-loader.md               # 三级检索加载规则
│   └── auto-capabilities.md           # 自动生成的能力索引 (模板)
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

**2. 会话结束时 (Stop Hook) — 摘要 + 自动知识提取**

```
Claude Code 会话结束
    → session-summary.sh
    → 从 JSONL transcript 提取对话
    → LLM (Haiku) 生成 JSON (摘要 + 知识)
    → 分发写入:
       ├─ sessions/{id}.md    ← 格式化 Markdown 摘要
       ├─ rules.md            ← 提取的开发规范 (去重)
       ├─ MEMORY.md           ← 避坑经验 (去重, trigram 去重)
       └─ daily/YYYY-MM-DD.md ← 增强版每日笔记
    → 后台非阻塞:
       ├─ memory-feedback.py  ← 记录会话结果 (success/failed)
       └─ signal-analyzer.py  ← 提取会话信号
```

知识提取条件:
- `CLAUDE_AUTO_EXTRACT=true` (默认开启)
- 对话字符数 > 500 (`CLAUDE_SUMMARIZE_MIN_CHARS`)
- 对话中出现规范纠正、踩坑修复、最佳实践等内容时 `has_knowledge=true`
- 闲聊/简单问答不触发知识提取 (避免噪音)

**3. 周期整理 (手动或 cron)**

```
weekly-consolidate.sh
    → 统计本周活动
    → 分析高频编辑文件
    → 刷新搜索索引
    → 自动事实提取 (auto-extract-facts.py)
    → 跨会话信号分析 (signal-analyzer.py)
    → 能力自动生成 (capability-generator.py)
    → 清理过期文件
```

**4. 自进化闭环**

```
经验被引用
    → memory-feedback.py 记录引用事件 (append-only)
    → Laplace 平滑计算有效性: p = (s+1)/(s+f+2)
    → 指数衰减: w = 0.5^(age_days/half_life)
    → 联合评分: value = p × w

会话结束
    → signal-analyzer.py 提取 8 种信号
    → 频率统计 + 循环模式检测
    → count >= 3 自动升级为 HIGH 优先级

周期性检查
    → capability-generator.py (门控条件全部满足时)
    → 从循环模式自动生成 Skill/Command
    → 更新 auto-capabilities.md 索引
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

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLAUDE_SESSION_SUMMARIZE` | `true` | 会话摘要开关，设为 `false` 回退到保存原始对话文本 |
| `CLAUDE_AUTO_EXTRACT` | `true` | 自动知识提取开关，设为 `false` 只生成 Markdown 摘要 |
| `CLAUDE_SUMMARIZE_MIN_CHARS` | `500` | 最小对话字符数，低于此值跳过知识提取 (仍生成摘要) |
| `CAPABILITY_MODEL` | `haiku` | 能力自动生成使用的 LLM 模型 |
| `CAPABILITY_GENERATION` | `true` | 能力自动生成开关，设为 `false` 禁用 |

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
| `/memory-health` | 记忆系统健康看板 | `/memory-health` |
| `/memory-signals` | 跨会话信号分析 | `/memory-signals analyze` |
| `/memory-generate` | 手动触发能力生成 | `/memory-generate --dry-run` |

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

- **分表折叠** — 自动识别 `travel_1` 类分表，折叠为逻辑表
- **前缀分组** — 按表名前缀自动分组，生成独立的 schema 文件
- **ast-grep 代码分析** — 从 `@Select/@Insert/@Update/@Delete` 注解中提取 SQL
- **sqlglot 解析** — 精确提取表名和 JOIN 关系，正则降级兜底
- **跨前缀标注** — JOIN 跨越不同前缀组时自动标注 `[跨前缀 → xxx]`

## 依赖

**必需:**
- Python 3.9+
- jq (用于 JSONL 解析)
- Claude CLI (`claude` 命令，用于 LLM 摘要)

**可选 (增强搜索):**
- `pip install fastembed` — 向量搜索 (BM25 + 向量 RRF 融合)

**可选 (Schema 提取):**
- `brew install ast-grep` — Java 代码 AST 分析
- `pip3 install pymysql sqlglot` — MySQL 连接和 SQL 解析

## Roadmap

- [ ] **MCP Server 集成** — 将记忆搜索封装为 MCP Tool，Claude Code 可直接调用而非通过 Bash
- [ ] **多语言嵌入模型支持** — 可切换 Qwen3-Embedding 等更大模型，提升语义搜索质量
- [ ] **Web UI 可视化** — 知识图谱浏览器，可视化 facts 关联和时间线
- [ ] **多用户/团队共享** — 支持团队级知识库，个人记忆与团队记忆分层
- [ ] **工作流集成** — 集成一些比较成熟的规范开发工具：bmad
- [ ] **超级工厂** — agent集群模式下的多角色全自动开发
- [ ] **提取sql扩展** — 扩展从mybatis的xml中提取sql
- [x] **自进化系统** — 经验反馈闭环 + 跨会话信号分析 + 能力自动生成
- [x] **自动记忆衰减** — 基于访问频率和时间自动调整事实优先级 (Laplace + 指数衰减)
- [x] **install.sh 一键安装** — 自动检测环境、复制文件、配置 hooks
- [x] **会话知识自动提取** — Stop Hook 自动提取规范和避坑经验，分发写入 rules.md 和 MEMORY.md

> 欢迎通过 [Issues](https://github.com/panglisen/code-memory/issues) 提出建议或参与讨论

## 致谢

- RRF 算法参考: Cormack, Clarke & Butt (2009) "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods"

## License

MIT
