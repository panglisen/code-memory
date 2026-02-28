# 三层记忆架构设计文档

## 概述

code-memory 是一个基于文件系统的 Claude Code 记忆系统。设计目标:

1. **零外部依赖** — 只用文件系统和 SQLite，不需要向量数据库或云服务
2. **节省上下文** — 按需加载，只在需要时读取相关文件
3. **自动化驱动** — Hook 机制自动记录，减少手动维护负担
4. **优雅降级** — 每一层都有降级方案，核心功能不依赖可选组件
5. **自进化闭环** — 经验有效性追踪 + 跨会话信号分析 + 能力自动生成

## 三层模型

### Layer 1: 知识图谱 (`areas/`)

**定位**: 结构化的长期知识存储

```
areas/
├── projects/{name}/       # 项目知识
│   ├── summary.md         # 项目概览（架构、技术栈、关键模块）
│   ├── rules.md           # 项目开发规范（编码约定、禁止项）
│   └── facts.json         # 原子事实（可检索、可追溯）
├── patterns/{name}/       # 代码模式
│   ├── summary.md         # 模式描述和适用场景
│   └── facts.json
└── tools/{name}/          # 工具经验
    ├── summary.md         # 工具使用技巧
    └── facts.json
```

**facts.json 格式:**

```json
{
  "facts": [
    {
      "id": "fact-001",
      "fact": "WebSocket 连接需要心跳保活，间隔 30 秒",
      "evidence": "用户手动添加",
      "timestamp": "2026-02-08",
      "status": "active",
      "category": "configuration"
    }
  ]
}
```

**事实生命周期:**
- `active` → `superseded` (被新事实替代，保留 `supersededBy` 追溯链)
- `active` → `archived` (已过时，不再适用)
- 新事实不删除旧事实，只标记状态

### Layer 2: 时间线日志 (`daily/` + `sessions/`)

**定位**: 按时间组织的事件记录

**每日笔记** (`daily/YYYY-MM-DD.md`):
- 由 PostToolUse Hook 自动生成
- 记录编辑了哪些文件、在哪个项目工作
- 同一分钟内同项目的编辑合并为一条

**会话摘要** (`sessions/{id}.md`):
- 由 Stop Hook 触发 LLM (Haiku) 生成
- 从 JSONL transcript 提取对话 → LLM 生成 JSON (摘要 + 知识提取)
- 单个文件从 ~1MB 原始 transcript 压缩为 ~2KB 摘要
- JSON 模式输出: 摘要 + 规则 + 避坑经验，自动分发到各目标文件
- 格式: 主题 + 关键知识点 + 涉及文件 + 决策记录

### Layer 3: 隐性知识 (`MEMORY.md`)

**定位**: 用户的个人偏好和跨项目经验

- 个人偏好（语言、模型、代码风格）
- 工作模式（TDD、代码审查流程）
- 技术栈总览
- 避坑经验（跨项目通用）
- 最佳实践（从周期性整理中提炼）

**每次会话开始时必读此文件。**

## 检索层设计

### 一级: 精确规则路由

在 `rules/memory-loader.md` 中定义触发词到文件的映射:

| 触发条件 | 加载文件 |
|----------|----------|
| 涉及项目架构 | `projects/{项目}/summary.md` |
| 涉及数据库 | `projects/shared-db/schema-index.md` |
| 提到具体表名 | `prefix-map.json` → `schema/{前缀}.md` |
| 用户问"记忆系统状态" | `memory-feedback.py report` |
| 用户问"循环问题" | `signal-analyzer.py recurring` |
| 用户问"能力生成" | `capability-generator.py --status` |

优点: 零延迟、精确命中
局限: 只能处理预定义的触发词

### 二级: RRF 混合搜索

当一级规则无法匹配时，使用 `memory-search.py` 进行全文搜索:

```
BM25 (SQLite FTS5)  ──┐
                       ├──→ RRF 融合 → top-6
向量 (fastembed)    ──┘
```

**BM25 路径:**
- SQLite FTS5 + unicode61 tokenizer
- CJK 字符自动前缀匹配（解决分词问题）
- 零外部依赖

**向量路径 (可选):**
- fastembed + BAAI/bge-small-zh-v1.5 (384d, 90MB ONNX)
- CPU 推理 ~5ms/条
- 增量索引，只嵌入变化的文件

**RRF 融合:**
- 公式: `score(d) = Σ 1/(k + rank_i)`，k=60
- 不依赖分数量纲，对小数据集鲁棒
- 工业标准（Azure AI Search、Elasticsearch 都使用）
- 实现简单，无需调参

**降级策略:**
- fastembed 已安装 → 自动启用混合搜索
- fastembed 未安装 → 自动降级为纯 BM25
- 用户可通过 `--mode bm25` 显式指定

### 三级: 纯向量语义搜索

通过 `--mode vector` 使用纯语义搜索，适合:
- BM25 关键词搜索无结果时
- 用自然语言描述模糊概念（如"用户认证流程"找到"JWT 校验逻辑"）

## 自进化系统 (Evolution)

### 概述

自进化系统为记忆系统增加了反馈闭环，由三个模块组成:

1. **经验有效性追踪** (`memory-feedback.py`) — 记录经验被引用的频率和效果
2. **跨会话信号分析** (`signal-analyzer.py`) — 检测循环出现的问题模式
3. **能力自动生成** (`capability-generator.py`) — 从循环模式自动生成 Skill/Command

数据存储在 `~/.claude/memory/evolution/` 目录下，与原有三层记忆解耦。

### 反馈闭环

经验被引用时，`memory-feedback.py` 记录引用事件到 append-only 审计日志:

```
经验被引用 → ref 事件 (experience_id, session_id, context)
会话结束 → outcome 事件 (session_id, success/failed, corrections_count)
```

**有效性评分算法:**

1. **Laplace 平滑**: `p = (success + 1) / (success + fail + 2)`
   - 避免零次引用时评分为 0
   - 少量数据时趋向 0.5 (不确定)

2. **指数衰减**: `w = 0.5^(age_days / half_life)`
   - `half_life` 默认 90 天
   - 老旧经验自然衰减

3. **联合评分**: `decay_value = p × w`
   - `decay_value < 0.3` 为低效候选，可标记 archived
   - `decay_value > 0.7` 为高效经验，优先展示

### 信号分析与循环检测

`signal-analyzer.py` 从每次会话摘要中提取 8 种结构化信号:

| 信号类型 | 描述 |
|----------|------|
| `error` | 错误和异常 |
| `correction` | 纠正和修复 |
| `capability_gap` | 能力缺口 |
| `recurring_issue` | 循环问题 |
| `environment_issue` | 环境问题 |
| `dependency_issue` | 依赖问题 |
| `performance_issue` | 性能问题 |
| `security_issue` | 安全问题 |

**循环检测算法:**

```
信号提取 → 按信号类型汇总 → 频率统计 (8 会话窗口)
    → count >= 3: 自动升级为 HIGH 优先级
    → count >= 5: 升级为 CRITICAL
    → 连续失败串检测: 最后 N 个会话连续失败时标记
```

**循环模式状态:**
- `active` — 活跃，持续监控
- `addressed` — 已处理，降低优先级
- `suppressed` — 已抑制 (count >= 3 在 8 会话窗口)

### 能力自动生成

`capability-generator.py` 在满足门控条件时，从循环模式自动生成 Claude Code Skill 或 Command:

**门控条件 (全部满足才生成):**

1. `CAPABILITY_GENERATION` 环境变量不为 `false`
2. 距上次生成 >= 24 小时 (冷却期)
3. 至少 2 个 count >= 2 的未处理循环模式
4. 最近 5 个会话 >= 60% 成功率 (系统稳定时才生成)

**生成流程:**

```
门控检查通过
    → 收集候选循环模式
    → LLM (Haiku) 合成 Skill YAML + 执行步骤
    → 写入 ~/.claude/skills/{skill-name}/SKILL.md
    → 更新 ~/.claude/rules/auto-capabilities.md 索引
    → 记录到 evolution/capabilities.jsonl 审计日志
```

**Skill 自动发现:**
- 生成的 Skill YAML description 包含明确触发条件词
- `auto-capabilities.md` 每次会话自动加载
- Claude 通过触发条件词自动匹配并调用

**成本控制:**
- 反馈和信号分析阶段: 纯 SQL + 正则，零 LLM 调用
- 能力生成: 仅门控通过时调用，使用轻量级模型 (Haiku)
- 估算月成本 < $2

### Evolution 数据格式

```
evolution/
├── audit.jsonl                # 经验引用事件 (append-only)
├── capabilities.jsonl         # 能力生成事件 (append-only)
├── generator_state.json       # 生成器状态 (冷却期、上次生成时间)
└── signal_report_latest.md    # 最新信号分析报告
```

**audit.jsonl 记录格式:**

```json
{"timestamp": "2026-02-26T10:00:00", "event_type": "ref", "experience_id": "avoidance:abc123", "session_id": "sess-001", "context": "引用上下文"}
{"timestamp": "2026-02-26T11:00:00", "event_type": "outcome", "session_id": "sess-001", "outcome": "success", "corrections": 0}
```

**capabilities.jsonl 记录格式:**

```json
{"timestamp": "2026-02-26T12:00:00", "event_type": "generation", "capability_name": "auto-env-fix", "type": "Skill", "trigger": "environment_issue", "pattern_count": 3}
```

## 自动化流程

### Hook 驱动

```
PreToolUse (Read)
    → rules-injector.sh
    → 检测文件路径 → 项目名 (config.json 映射 or 目录名匹配)
    → 首次: allow + additionalContext 注入规范
    →   ├─ 读取 projects/{name}/rules.md
    →   └─ 追加 project_groups 组规范 (如 shared-java/rules.md)
    → 后续: 静默放行 (flag: /tmp/claude-rules-{sid}-{project})

PreToolUse (Edit|Write)
    → rules-gate.sh (安全网)
    → 检查 flag 文件是否存在
    → 已加载: 放行
    → 未加载: deny + 提示先 Read rules.md

SubagentStart (Plan|Explore|architect)
    → schema-injector.sh
    → 首次: 注入 shared-db/schema-index.md 到 subagent 上下文
    → 后续: 静默放行 (flag: /tmp/claude-schema-{sid})

PreCompact (*)
    → pre-compact.sh
    → 清除 /tmp/claude-rules-{sid}-* 和 /tmp/claude-schema-{sid}
    → 压缩后下次 Read 项目文件时 rules-injector 自动重新注入

PostToolUse (Edit|Write)
    → extract-memory.sh
    → 更新每日笔记
    → 去重: 同分钟同项目合并

PostToolUse (Edit|Write)
    → avoidance-gate.sh (避坑规则自动检测)
    → 根据文件路径路由到规则引擎:
       ├─ *.js/*.jsx + 前端项目 → lint-antd-v3-rules.py
       ├─ *Dao.java/*Mapper.java → lint-java-sql-rules.py
       ├─ *.sh                   → lint-shell-rules.py
       └─ *.py                   → lint-python-rules.py
    → 规则引擎返回:
       ├─ block: Claude 必须修复后重试
       ├─ approve + warnings: stderr 提示 (不阻断)
       └─ 无输出: 通过
    → Tier 2 (CLAUDE_SEMANTIC_LINT=true): LLM 语义检测

SessionEnd
    → session-summary.sh
    → 从 JSONL 提取对话 (jq)
    → 判断模式:
       ├─ JSON 模式 (AUTO_EXTRACT=true 且对话 > 500字符):
       │  → LLM 生成 JSON (摘要 + knowledge)
       │  → 验证 JSON → 分发写入:
       │     ├─ sessions/{id}.md  ← 格式化 Markdown 摘要
       │     ├─ rules.md          ← 提取的开发规范 (去重)
       │     ├─ MEMORY.md         ← 避坑经验 (trigram 去重)
       │     └─ daily/            ← 增强版每日笔记
       └─ Markdown 模式 (短对话或关闭提取):
          → LLM 生成纯 Markdown 摘要
          → 保存到 sessions/
          → 索引到每日笔记
    → 后台非阻塞:
       ├─ memory-feedback.py outcome ← 记录会话结果
       └─ signal-analyzer.py extract ← 提取会话信号
```

### 周期任务

```
weekly-consolidate.sh (每周日)
    → 统计本周活动
    → 高频编辑文件分析
    → 会话主题分析
    → 知识图谱状态检查
    → 搜索索引刷新
    → 自动事实提取 (auto-extract-facts.py)
    → 跨会话信号分析 (signal-analyzer.py)
    → 能力自动生成 (capability-generator.py)
    → 过期文件清理 (sessions 30天, daily 90天)
```

### 自动事实提取

```
auto-extract-facts.py
    → 扫描本周 sessions，按项目分组
    → 过滤 >= 2 个 session 的项目
    → 提取关键知识点文本
    → LLM (Haiku) 生成 1-3 条原子事实
    → 验证 + 去重 → 写入 facts.json
```

### 会话知识自动提取 (session-summary.sh JSON 模式)

session-summary.sh 的 JSON 模式会从每次会话中自动提取结构化知识:

**LLM 输出 JSON 结构:**

```json
{
  "summary": {
    "topic": "一句话主题",
    "key_points": ["要点1", "要点2"],
    "files": ["被编辑的文件"],
    "decisions": ["技术决策"],
    "todos": ["待办事项"]
  },
  "knowledge": {
    "has_knowledge": true,
    "project": "识别的项目名",
    "rules": [
      {"category": "编码规范", "rule": "规则描述"}
    ],
    "avoidances": ["避坑经验描述"]
  }
}
```

**分发写入逻辑:**

1. **sessions/{id}.md** — `format_summary_markdown()` 将 JSON 格式化为可读的 Markdown
2. **rules.md** — `write_rules()` 将 rules 按 category 分区写入项目 rules.md (去重: `grep -qF`)
3. **MEMORY.md** — `write_avoidances()` 将 avoidances 追加到"避坑经验"区块 (去重: trigram Jaccard + Containment)
4. **daily/{date}.md** — `write_daily_entry()` 生成增强版每日笔记条目 (含主题、要点、知识提取标记)

**知识提取条件:**
- `has_knowledge: true` 仅当会话中出现规范纠正、踩坑修复、最佳实践发现
- 闲聊、简单问答 → `has_knowledge: false`，不触发知识写入
- 避免 LLM 编造不存在的知识

**降级策略:**
- JSON 解析失败 → 降级为纯文本摘要保存
- LLM 超时 (45s) → 保存原始对话文本
- 对话太短 (< 500字符) → 仅 Markdown 模式，不提取知识

## 数据格式

### 会话摘要格式

```markdown
# 会话摘要: {session-id}

- **项目**: my-project
- **时间**: 2026-02-10 14:30 (最后更新)
- **工作目录**: /path/to/project
- **原始大小**: 256KB (1200 行)

---

1. **主题**: 实现用户认证模块
2. **关键知识点**:
   - JWT token 过期时间设为 24 小时
   - 刷新 token 存储在 HttpOnly cookie 中
   - 密码使用 bcrypt 加密
3. **涉及文件**: auth.ts, middleware/jwt.ts
4. **决策记录**: 选择 JWT 而非 Session，因为需要支持微服务间认证
```

### 搜索索引结构

```sql
-- BM25 全文搜索
CREATE VIRTUAL TABLE chunks USING fts5(path, category, title, content);

-- 分块元数据
CREATE TABLE chunks_meta (rowid INTEGER PRIMARY KEY, path TEXT, line_start INTEGER, line_end INTEGER);

-- 向量嵌入 (可选)
CREATE TABLE embeddings (chunk_rowid INTEGER PRIMARY KEY, embedding BLOB, model TEXT, content_hash TEXT);

-- 经验引用追踪 (自进化系统)
CREATE TABLE experience_refs (id INTEGER PRIMARY KEY, experience_id TEXT, session_id TEXT, timestamp TEXT, context TEXT);

-- 会话结果记录
CREATE TABLE session_outcomes (id INTEGER PRIMARY KEY, session_id TEXT, timestamp TEXT, outcome TEXT, corrections INTEGER);

-- 经验有效性统计视图
CREATE TABLE experience_effectiveness (experience_id TEXT PRIMARY KEY, total_refs INTEGER, success_refs INTEGER, fail_refs INTEGER, laplace_score REAL, last_ref TEXT);
```

## 设计决策

### 为什么用文件系统而非数据库?

- Claude Code 原生支持文件读取（Read 工具）
- 用户可以用任何编辑器查看和修改记忆内容
- Git 友好，可以版本控制
- 不需要额外的服务进程

### 为什么用 RRF 而非加权求和?

- RRF 不依赖分数量纲（BM25 rank 和余弦相似度量纲不同）
- 对小数据集更鲁棒
- 工业标准（Azure AI Search、Elasticsearch 都使用）
- 实现简单，无需调参

### 为什么用 fastembed 而非 API 嵌入?

- 零 API key，零网络依赖
- CPU 推理足够快（~5ms/条）
- 模型首次运行自动下载到 `~/.cache/fastembed/`
- 可选依赖，不影响基础功能

### 为什么用 Laplace 平滑而非简单计数?

- 简单计数 (success/total) 在样本量小时不可靠
- Laplace 平滑 `(s+1)/(s+f+2)` 在零次引用时给出 0.5 (不确定)
- 结合指数衰减避免老旧经验永远高权重
- 算法简单，零 LLM 调用

### 为什么用 append-only 审计日志?

- 保留完整的变更历史供审计追溯
- 避免就地修改导致数据丢失
- JSONL 格式便于流式处理和增量分析
- 文件系统原生支持 append 操作

### 为什么用 additionalContext 而非 block+reason 注入规范?

- **零浪费**: `block+reason` 会阻断 Read，Claude 需要重试一次才能读到文件内容。`additionalContext` 配合 `permissionDecision:"allow"` 让 Read 正常执行的同时注入规范
- **API 对齐**: `hookSpecificOutput` 是 Claude Code Hooks 的官方新格式，旧的 `{"decision":"block"}` 已 deprecated
- **一次调用 = 读文件 + 加载规范**: 大幅减少上下文浪费

### 为什么用 SubagentStart 注入 Schema 而非 PreToolUse?

- 设计阶段的 subagent (Plan/Explore/architect) 有独立的上下文窗口，无法看到主会话加载的信息
- SubagentStart Hook 的 `additionalContext` 直接注入到 subagent 上下文中
- schema 索引只需注入一次（同会话 flag 控制），subagent 就能按需 Read 详细 schema 文件
- 比在主会话中预加载 schema 更节省上下文（只有用到 subagent 时才注入）

### 为什么 project_groups 而非硬编码共享规范?

- 不同用户有不同的项目组合（可能是 3 个 Java 项目共享，也可能是 Go + Python 混合）
- `config.json` 的 `project_groups` 让用户自由定义哪些项目共享同一套规范
- 脚本只需遍历 groups，不需要知道具体有哪些项目

### 为什么避坑规则检测放在 PostToolUse 而非 PreToolUse?

- **PostToolUse** 在编辑完成后运行，此时文件内容已经写入磁盘，规则引擎可以读到完整的新代码
- **PreToolUse** 只能看到 tool_input (old_string/new_string)，无法获取编辑后的完整文件上下文
- PostToolUse 的 `block` 仍然可以阻止 Claude 继续，Claude 会看到 block 原因并自动修复

### 为什么用两层规则引擎 (Tier 1 + Tier 2)?

- **Tier 1 (正则/AST)**: 零 LLM 调用、零延迟、零成本，适合可精确模式匹配的规则（如 `declare -A`、`match/case`、SQL 反模式）
- **Tier 2 (LLM 语义)**: 处理无法用正则表达的语义规则（如"是否硬编码了业务常量"），但有延迟和成本
- Tier 2 默认关闭 (`CLAUDE_SEMANTIC_LINT=true` 启用)，不需要时零开销
- 两层分离让 Tier 1 始终高效运行，Tier 2 按需启用

