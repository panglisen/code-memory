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
│  │  PreToolUse → 规范注入  |  PostToolUse → 每日笔记│    │
│  │  PostToolUse → 避坑规则 |  SubagentStart → Schema│    │
│  │  PreCompact → flag清除  |  SessionEnd → 摘要     │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌─ 自进化反馈 ────────────────────────────────────┐    │
│  │  经验引用追踪 → 信号分析 → 能力自动生成         │    │
│  │  避坑经验 → 规则蒸馏 → auto-rules 自动检测      │    │
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
                    规则蒸馏
```

## 核心特性

- **三层记忆架构** — 知识图谱 + 时间线日志 + 隐性知识，按需加载节省上下文
- **RRF 混合搜索** — BM25 关键词 + 向量语义双路检索，Reciprocal Rank Fusion 融合
- **Hook 驱动自动化** — 文件编辑自动记录每日笔记，会话结束自动生成摘要 + 知识提取分发
- **规范无损注入** — PreToolUse Hook 通过 `additionalContext` 零浪费注入项目规范，Read 不被阻断
- **Subagent Schema 注入** — SubagentStart Hook 向 Plan/Explore 等设计 agent 自动注入数据库 schema 索引
- **自进化系统** — 经验有效性追踪 (Laplace 平滑 + 指数衰减)、跨会话信号分析 (循环问题检测)、能力自动生成 (从循环模式自动生成 Skill/Command)
- **避坑规则自动拦截** — PostToolUse Hook 编辑后自动检测代码反模式 (antd v3、Java SQL、Shell/Python 兼容性)，Tier 2 可选 LLM 语义检测
- **避坑经验规则蒸馏** — 新避坑经验写入后，LLM 自动判断可否正则化，生成 JSON 规则配置，avoidance-gate 自动加载检测
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
cp scripts/lib/*.jq scripts/lib/*.py ~/.claude/scripts/lib/
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
│   ├── session-summary.sh             # SessionEnd Hook: 会话摘要 + 知识提取
│   ├── extract-memory.sh              # PostToolUse Hook: 编辑记录
│   ├── avoidance-gate.sh              # PostToolUse Hook: 避坑规则自动检测入口
│   ├── semantic-lint.sh               # Tier 2 语义检测 (LLM, 默认关闭)
│   ├── rules-injector.sh              # PreToolUse Hook (Read): 项目规范无损注入
│   ├── rules-gate.sh                  # PreToolUse Hook (Edit|Write): 规范加载安全网
│   ├── schema-injector.sh             # SubagentStart Hook: Schema 索引注入
│   ├── pre-compact.sh                 # PreCompact Hook: 压缩前清除 flag 确保重新注入
│   ├── weekly-consolidate.sh          # 周期整理 + 信号分析 + 能力生成 + 规则蒸馏
│   ├── auto-extract-facts.py          # 自动事实提取 (LLM 驱动)
│   ├── extract-schema.py              # DB Schema 提取 (ast-grep + sqlglot)
│   ├── migrate-sessions.sh            # 历史会话迁移工具
│   ├── memory-feedback.py             # 经验有效性追踪 (Laplace + 指数衰减)
│   ├── signal-analyzer.py             # 跨会话信号分析 (循环问题检测)
│   ├── capability-generator.py        # 能力自动生成 (Skill/Command)
│   ├── rule-distiller.py              # 避坑经验规则蒸馏 (LLM → JSON 规则配置)
│   ├── cleanup-avoidances.sh          # 避坑经验去重整理
│   └── lib/
│       ├── extract-conversation.jq    # JSONL 对话提取
│       ├── lint-antd-v3-rules.py      # 规则引擎: 前端 antd v3 反模式
│       ├── lint-java-sql-rules.py     # 规则引擎: Java DAO SQL 反模式
│       ├── lint-shell-rules.py        # 规则引擎: Shell bash 3.2 兼容
│       ├── lint-python-rules.py       # 规则引擎: Python 3.9 兼容
│       └── lint-auto-rules.py        # 规则引擎: 自动生成规则 (读取 auto-rules.json)
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

**0. 读取项目文件时 (PreToolUse Hook — Read)**

```
Claude Code 读取项目文件
    → rules-injector.sh
    → 检测文件路径属于哪个项目 (config.json 映射或目录名匹配)
    → 首次: allow + additionalContext 注入规范 (零浪费，Read 正常执行)
    → 后续: 静默放行 (同会话 flag 文件控制)
```

**0.5 编辑项目文件时 (PreToolUse Hook — Edit|Write)**

```
Claude Code 编辑项目文件
    → rules-gate.sh (安全网)
    → 检查规范是否已加载 (flag 文件)
    → 已加载: 放行
    → 未加载: deny，提示先 Read rules.md
```

**0.8 启动设计 Subagent 时 (SubagentStart Hook)**

```
Claude Code 启动 Plan/Explore/architect subagent
    → schema-injector.sh
    → 首次: 注入 schema 索引到 subagent 上下文
    → 后续: 静默放行 (同会话只注入一次)
```

**0.9 上下文压缩前 (PreCompact Hook)**

```
Claude Code 触发上下文压缩
    → pre-compact.sh
    → 清除当前会话的 rules/schema flag 文件
    → 压缩后下次 Read 项目文件时自动重新注入规范
```

**1. 文件编辑后 — 每日笔记 (PostToolUse Hook)**

```
Claude Code 编辑文件
    → extract-memory.sh
    → 记录到 daily/YYYY-MM-DD.md
    → 同一分钟内去重合并
```

**1.5 文件编辑后 — 避坑规则检测 (PostToolUse Hook)**

```
Claude Code 编辑文件
    → avoidance-gate.sh (统一分发入口)
    → Tier 1a: 特定规则引擎 (按文件类型路由):
       ├─ *.js/*.jsx (前端项目) → lint-antd-v3-rules.py (antd v3 反模式)
       ├─ *Dao.java/*Mapper.java → lint-java-sql-rules.py (SQL 反模式)
       ├─ *.sh                   → lint-shell-rules.py (bash 3.2 兼容)
       └─ *.py                   → lint-python-rules.py (Python 3.9 兼容)
    → Tier 1b: auto-rules 引擎 (始终运行, 所有文件类型):
       └─ lint-auto-rules.py → 读取 auto-rules.json → 正则检测
    → 合并结果:
       ├─ 任一引擎 block → Claude 必须修复后重试
       ├─ warnings 合并 → stderr 提示，编辑正常通过
       └─ 无输出 → 通过
    → (可选) CLAUDE_SEMANTIC_LINT=true 时启用 Tier 2 LLM 语义检测
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
       ├─ signal-analyzer.py  ← 提取会话信号
       └─ rule-distiller.py   ← 避坑经验规则蒸馏 (有新经验时触发)
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
    → 规则蒸馏 (rule-distiller.py)
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

避坑经验写入
    → rule-distiller.py (后台触发)
    → LLM (Haiku) 判断是否可正则化
    → 验证 regex (re.compile)
    → 写入 auto-rules.json (severity 一律 warn)
    → 下次编辑时 avoidance-gate.sh 自动加载检测

周期性检查
    → capability-generator.py (门控条件全部满足时)
    → 从循环模式自动生成 Skill/Command
    → 更新 auto-capabilities.md 索引
    → rule-distiller.py 批量蒸馏新规则
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

## 自进化系统

自进化系统由四个组件组成，形成「经验写入 → 追踪反馈 → 信号检测 → 能力生成 + 规则蒸馏」的完整闭环。绝大部分自动运行，无需手动干预。

```
┌─────────────────────────────────────────────────────────────────┐
│                         自进化数据流                              │
│                                                                 │
│  会话中踩坑/纠正                                                 │
│       │                                                         │
│       ▼                                                         │
│  session-summary.sh ──写入──→ MEMORY.md / rules.md              │
│       │                            │                            │
│       ├── memory-feedback.py       │                            │
│       │   (记录引用+会话结果)       ▼                            │
│       │                     rule-distiller.py                   │
│       ├── signal-analyzer.py    (LLM 判断可否正则化)             │
│       │   (提取信号+循环检测)       │                            │
│       │         │                  ▼                            │
│       │         ▼            auto-rules.json                    │
│       │   recurring_patterns       │                            │
│       │         │                  ▼                            │
│       │         ▼            avoidance-gate.sh                  │
│       │   capability-generator.py  (下次编辑时自动检测)           │
│       │   (生成 Skill/Command)                                  │
│       │         │                                               │
│       │         ▼                                               │
│       │   auto-capabilities.md                                  │
│       │   (Claude 自动加载)                                     │
│       │                                                         │
│  ┌────┴────────────────────────────────────────────────┐        │
│  │ evolution/ 数据文件                                  │        │
│  │  audit.jsonl          — 经验引用审计日志 (append-only)│        │
│  │  auto-rules.json      — 自动生成的检测规则配置       │        │
│  │  generator_state.json — 能力生成器状态               │        │
│  │  capabilities.jsonl   — 能力生成审计日志             │        │
│  │  signal_report_latest.md — 最新信号分析报告          │        │
│  └─────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────┘
```

### 组件一览

| 组件 | 脚本 | 触发方式 | 作用 |
|------|------|----------|------|
| 经验追踪 | `memory-feedback.py` | SessionEnd 自动 | Laplace 平滑 + 指数衰减评估经验有效性 |
| 信号分析 | `signal-analyzer.py` | SessionEnd 自动 | 从会话中提取 8 种信号，检测循环模式 |
| 能力生成 | `capability-generator.py` | 周期整理/手动 | 从循环模式自动生成 Skill/Command |
| 规则蒸馏 | `rule-distiller.py` | 写入避坑经验后自动 | 避坑经验 → 正则检测规则 (JSON 配置) |

### 1. 经验有效性追踪 (memory-feedback.py)

追踪每条避坑经验/规范的引用次数和成功率，使用 Laplace 平滑 + 指数衰减算法评估有效性。

**自动行为** — 会话结束时自动记录会话结果，无需手动操作。

**手动操作:**

```bash
# 查看有效性报告 (哪些经验被引用、成功率如何)
python3 ~/.claude/scripts/memory-feedback.py report

# JSON 格式输出 (便于程序处理)
python3 ~/.claude/scripts/memory-feedback.py report --json

# 查看低效经验候选 (有效性低于阈值)
python3 ~/.claude/scripts/memory-feedback.py stale --threshold 0.3

# 扫描 MEMORY.md 和 rules.md，注册所有经验
python3 ~/.claude/scripts/memory-feedback.py scan

# 重新计算所有经验分数
python3 ~/.claude/scripts/memory-feedback.py recalculate
```

**算法说明:**

- **Laplace 平滑**: `p = (success + 1) / (success + failure + 2)`，避免零概率
- **指数衰减**: `weight = 0.5^(age_days / 30)`，30 天半衰期，老经验权重自动降低
- **联合评分**: `value = p × weight`，综合评估经验价值

### 2. 跨会话信号分析 (signal-analyzer.py)

从会话摘要中提取信号，自动检测跨会话的循环问题。

**检测的 8 种基础信号:**

| 信号 | 含义 | 触发关键词 |
|------|------|-----------|
| `session_had_errors` | 会话出错 | 报错、失败、bug、error |
| `user_correction` | 用户纠正 | 不要、禁止、错了、revert |
| `capability_gap` | 能力缺口 | 不支持、缺少、无法 |
| `recurring_issue` | 重复问题 | 又遇到、同样的、again |
| `performance_issue` | 性能问题 | 慢、超时、timeout、OOM |
| `architecture_decision` | 架构决策 | 架构、设计、重构 |
| `dependency_issue` | 依赖问题 | 依赖、版本、冲突 |
| `environment_issue` | 环境问题 | 环境、配置、JDK |

**4 种复合信号** (需多个关键词同时出现): `import_path_error`、`database_schema_issue`、`test_failure`、`deployment_issue`

**自动行为:**

- 会话结束时 `signal-analyzer.py extract` 自动提取信号
- 同一信号在窗口内出现 >= 3 次自动抑制 (避免噪音)
- 循环模式出现 >= 3 次自动升级为 HIGH 优先级

**手动操作:**

```bash
# 分析近 14 天信号 (生成报告到 evolution/signal_report_latest.md)
python3 ~/.claude/scripts/signal-analyzer.py analyze --days 14

# 查看活跃的循环问题
python3 ~/.claude/scripts/signal-analyzer.py recurring

# 标记循环模式为已解决
python3 ~/.claude/scripts/signal-analyzer.py resolve --pattern-id 3 --resolution "已通过 auto-rules 解决"

# 查看统计信息
python3 ~/.claude/scripts/signal-analyzer.py stats

# 从现有会话历史回填信号数据 (首次安装后使用)
python3 ~/.claude/scripts/signal-analyzer.py backfill --days 30

# 自动升级高频模式
python3 ~/.claude/scripts/signal-analyzer.py auto-escalate
```

### 3. 能力自动生成 (capability-generator.py)

从循环模式中自动生成 Claude Code Skill 或 Command。生成的能力会写入 `~/.claude/skills/` 或 `~/.claude/commands/`，并更新 `~/.claude/rules/auto-capabilities.md` 索引。

**门控条件** — 必须同时满足才会触发自动生成:

1. `CAPABILITY_GENERATION` 环境变量不为 `false`
2. 距上次生成 >= 24 小时
3. 至少 2 个 `count >= 2` 的未处理循环模式
4. 最近 5 个会话 >= 60% 成功率

**手动操作:**

```bash
# 查看生成器状态 (上次生成时间、候选模式数)
python3 ~/.claude/scripts/capability-generator.py --status

# 预览可生成的能力 (不实际写文件)
python3 ~/.claude/scripts/capability-generator.py --dry-run

# 跳过门控强制预览
python3 ~/.claude/scripts/capability-generator.py --dry-run --force

# 针对特定循环模式生成
python3 ~/.claude/scripts/capability-generator.py --force --pattern-id 3

# 执行生成 (带门控)
python3 ~/.claude/scripts/capability-generator.py
```

### 4. 避坑经验规则蒸馏 (rule-distiller.py)

将避坑经验自动转化为正则检测规则。LLM (Haiku) 判断经验是否可正则化，生成 JSON 规则配置，avoidance-gate 自动加载。

**安全约束:**

- 自动生成的规则 severity 一律 `warn` (不 block)，避免误报阻断编辑
- 只有人工审核后手动改 auto-rules.json 中的 severity 为 `block` 才会强制阻断
- `confidence < 0.7` 的规则不写入
- 生成的 regex 必须通过 `re.compile()` 验证

**自动行为** — 会话结束写入避坑经验后自动后台触发，周期整理时也会批量运行。

**手动操作:**

```bash
# 预览模式 (不写入文件)
python3 ~/.claude/scripts/rule-distiller.py --dry-run

# 执行蒸馏
python3 ~/.claude/scripts/rule-distiller.py

# 强制重新分析所有经验 (跳过已处理的 hash 去重)
python3 ~/.claude/scripts/rule-distiller.py --force

# 列出已生成的规则
python3 ~/.claude/scripts/rule-distiller.py --list

# 删除指定规则
python3 ~/.claude/scripts/rule-distiller.py --remove auto-003
```

**auto-rules.json 规则格式:**

```json
{
  "id": "auto-001",
  "source": "macOS bash 3.2 不支持 declare -A 关联数组",
  "file_globs": ["*.sh"],
  "pattern": "^\\s*declare\\s+-A\\b",
  "pattern_flags": ["MULTILINE"],
  "severity": "warn",
  "message": "declare -A 在 macOS bash 3.2 中不可用",
  "fix_hint": "使用 case 语句替代",
  "confidence": 0.9
}
```

### 斜杠命令 (会话内使用)

| 命令 | 用途 | 示例 |
|------|------|------|
| `/memory-health` | 综合健康看板 | `/memory-health full` |
| `/memory-health feedback` | 经验有效性报告 | — |
| `/memory-health signals` | 信号分析报告 | — |
| `/memory-health capabilities` | 能力生成状态 | — |
| `/memory-signals` | 信号分析 | `/memory-signals analyze` |
| `/memory-signals recurring` | 查看循环问题 | — |
| `/memory-signals resolve 3` | 标记已解决 | — |
| `/memory-generate` | 手动触发能力生成 | `/memory-generate --dry-run` |

### 典型使用场景

**场景 1: 日常开发 (全自动，无需手动操作)**

正常使用 Claude Code 即可。会话结束时自动提取知识、记录信号、蒸馏规则。下次编辑文件时自动检测。

**场景 2: 查看系统健康状态**

```
/memory-health
```

输出经验有效性评分、信号频率排名、循环模式清单、能力生成状态。

**场景 3: 发现某个避坑经验被反复忽略**

```bash
# 1. 查看低效经验
python3 ~/.claude/scripts/memory-feedback.py stale

# 2. 检查是否已有对应的自动规则
python3 ~/.claude/scripts/rule-distiller.py --list

# 3. 如果没有，手动触发蒸馏
python3 ~/.claude/scripts/rule-distiller.py --dry-run
python3 ~/.claude/scripts/rule-distiller.py

# 4. 如果蒸馏成功但想升级为 block，手动编辑:
# vim ~/.claude/memory/evolution/auto-rules.json
# 将对应规则的 "severity": "warn" 改为 "severity": "block"
```

**场景 4: 首次安装后初始化自进化数据**

```bash
# 从现有会话历史回填信号
python3 ~/.claude/scripts/signal-analyzer.py backfill --days 30

# 扫描注册所有经验
python3 ~/.claude/scripts/memory-feedback.py scan

# 运行一次完整分析
python3 ~/.claude/scripts/signal-analyzer.py analyze --days 30

# 蒸馏已有避坑经验为检测规则
python3 ~/.claude/scripts/rule-distiller.py --dry-run
python3 ~/.claude/scripts/rule-distiller.py
```

**场景 5: 周期整理 (weekly-consolidate.sh 自动执行)**

```bash
# 手动运行周期整理 (或配置 cron: 0 10 * * 0)
~/.claude/scripts/weekly-consolidate.sh

# 预览模式
~/.claude/scripts/weekly-consolidate.sh --dry-run
```

周期整理会依次执行: 统计 → 搜索索引刷新 → 事实提取 → 信号分析 → 能力生成 → 规则蒸馏 → 过期清理。

## 配置说明

### Hooks 配置

在 `~/.claude/settings.json` 中添加:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/rules-injector.sh"}]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/rules-gate.sh"}]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/extract-memory.sh"}]
      },
      {
        "matcher": "Edit|Write",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/avoidance-gate.sh"}]
      }
    ],
    "SubagentStart": [
      {
        "matcher": "Plan|Explore|everything-claude-code:planner|everything-claude-code:architect",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/schema-injector.sh"}]
      }
    ],
    "PreCompact": [
      {
        "matcher": "*",
        "hooks": [{"type": "command", "command": "~/.claude/scripts/pre-compact.sh"}]
      }
    ],
    "SessionEnd": [
      {
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
| `CLAUDE_SEMANTIC_LINT` | (未设置) | 设为 `true` 启用 Tier 2 LLM 语义检测 (避坑规则) |

### 项目映射配置

创建 `~/.claude/memory/config.json`:

```json
{
  "project_name_map": {
    "my-backend-parent": "backend-api"
  },
  "project_groups": {
    "shared-java": ["backend-api", "worker-service"]
  },
  "frontend_projects": ["frontend"],
  "ignored_projects": ["mac", "git-repo"],
  "domain_hints": {
    "app_user": {"label": "用户", "keywords": ["用户表", "会员"]}
  },
  "special_prefixes": [],
  "source_labels": {}
}
```

- `project_name_map` — 代码仓库目录名到记忆系统项目名的映射
- `project_groups` — 项目组定义，组内项目共享规范（如多个 Java 项目共享 `shared-java/rules.md`）
- `frontend_projects` — 前端项目列表（用于路由 antd 避坑规则）
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
- [x] **避坑经验规则蒸馏** — 新避坑经验自动转化为正则检测规则 (LLM 判断 + JSON 配置 + avoidance-gate 自动加载)
- [x] **自动记忆衰减** — 基于访问频率和时间自动调整事实优先级 (Laplace + 指数衰减)
- [x] **install.sh 一键安装** — 自动检测环境、复制文件、配置 hooks
- [x] **会话知识自动提取** — Stop Hook 自动提取规范和避坑经验，分发写入 rules.md 和 MEMORY.md
- [x] **避坑规则自动拦截** — PostToolUse Hook 编辑后自动检测代码反模式，Tier 2 可选 LLM 语义检测
- [x] **规范无损注入** — PreToolUse `additionalContext` 零浪费注入项目规范 + SubagentStart Schema 索引注入

> 欢迎通过 [Issues](https://github.com/panglisen/code-memory/issues) 提出建议或参与讨论

## 致谢

- RRF 算法参考: Cormack, Clarke & Butt (2009) "Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods"

## License

MIT
