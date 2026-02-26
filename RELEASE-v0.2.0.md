# v0.2.0 - 自进化系统 (Self-Evolution System)

## 中文

### 新增功能

**自进化系统** — 记忆系统增加反馈闭环，从被动记录升级为主动进化。

- **经验有效性追踪** (`memory-feedback.py`)
  - 记录经验被引用的频率和效果
  - Laplace 平滑 + 指数衰减联合评分
  - 自动识别低效和过时的经验

- **跨会话信号分析** (`signal-analyzer.py`)
  - 从会话摘要提取 8 种结构化信号
  - 频率统计 + 循环模式自动检测
  - 优先级自动升级 (count >= 3 → HIGH, >= 5 → CRITICAL)

- **能力自动生成** (`capability-generator.py`)
  - 从循环模式自动生成 Claude Code Skill/Command
  - 四重门控: 环境变量、24h 冷却期、循环模式数量、成功率阈值
  - 生成的 Skill 支持自动发现和调用

- **避坑经验去重** (`cleanup-avoidances.sh`)
  - Jaccard trigram + Containment 多策略去重
  - LLM 批量合并相似经验

- **新增斜杠命令**
  - `/memory-health` — 记忆系统健康看板
  - `/memory-signals` — 跨会话信号分析
  - `/memory-generate` — 手动触发能力生成

- **session-summary.sh 增强**
  - 后台非阻塞调用 feedback 和 signal 脚本
  - trigram 去重避免重复避坑经验
  - 屏蔽词列表过滤无效经验
  - 项目名映射从 config.json 动态读取

- **weekly-consolidate.sh 增强**
  - 新增 §10b 跨会话信号分析
  - 新增 §10c 能力自动生成

### 变更

- Python 版本要求从 3.10+ **放宽到 3.9+**
- 所有脚本使用 `#!/usr/bin/env python3` (不硬编码路径)
- 所有 Python 调用改为 `python3` (便于用户配置)
- `install.sh` 新增 Python 版本检查
- `install.sh` 安装列表扩展 (4 新脚本 + 3 新命令 + 1 新规则)
- `install.sh` 创建 `evolution/` 目录
- `memory-loader.md` 新增 evolution 触发规则和目录结构
- `auto-capabilities.md` 模板文件 (空索引表)

### 升级指南

已安装 v0.1.x 的用户:

```bash
cd code-memory
git pull
bash install.sh  # 会覆盖脚本和命令，不覆盖记忆数据
```

手动升级:
```bash
# 复制新增脚本
cp scripts/memory-feedback.py scripts/signal-analyzer.py \
   scripts/capability-generator.py scripts/cleanup-avoidances.sh \
   ~/.claude/scripts/

# 复制新增命令
cp commands/memory-health.md commands/memory-signals.md \
   commands/memory-generate.md ~/.claude/commands/

# 复制新增规则
cp rules/auto-capabilities.md ~/.claude/rules/

# 创建 evolution 目录
mkdir -p ~/.claude/memory/evolution

# 更新已有文件
cp scripts/session-summary.sh scripts/weekly-consolidate.sh ~/.claude/scripts/
cp rules/memory-loader.md ~/.claude/rules/

# 设置权限
chmod +x ~/.claude/scripts/*.sh ~/.claude/scripts/*.py
```

---

## English

### What's New

**Self-Evolution System** — Memory system gains a feedback loop, upgrading from passive recording to active evolution.

- **Experience Effectiveness Tracking** (`memory-feedback.py`)
  - Tracks how often experiences are referenced and their outcomes
  - Laplace smoothing + exponential decay for scoring
  - Automatically identifies ineffective and outdated experiences

- **Cross-Session Signal Analysis** (`signal-analyzer.py`)
  - Extracts 8 structured signal types from session summaries
  - Frequency statistics + automatic recurring pattern detection
  - Auto-priority escalation (count >= 3 → HIGH, >= 5 → CRITICAL)

- **Automatic Capability Generation** (`capability-generator.py`)
  - Auto-generates Claude Code Skills/Commands from recurring patterns
  - Four-gate control: env var, 24h cooldown, pattern count, success rate
  - Generated Skills support auto-discovery and invocation

- **Avoidance Deduplication** (`cleanup-avoidances.sh`)
  - Jaccard trigram + Containment multi-strategy dedup
  - LLM batch merging of similar experiences

- **New Slash Commands**
  - `/memory-health` — Memory system health dashboard
  - `/memory-signals` — Cross-session signal analysis
  - `/memory-generate` — Manual capability generation trigger

### Changes

- Python version requirement **relaxed from 3.10+ to 3.9+**
- All scripts use `#!/usr/bin/env python3` (no hardcoded paths)
- All Python calls changed to `python3` (user-configurable)
- `install.sh` now includes Python version check
- `install.sh` install list expanded (4 new scripts + 3 new commands + 1 new rule)
- `install.sh` creates `evolution/` directory
- `memory-loader.md` adds evolution trigger rules and directory structure
- `auto-capabilities.md` template file (empty index table)

### Upgrade Guide

For existing v0.1.x users:

```bash
cd code-memory
git pull
bash install.sh  # Overwrites scripts and commands, preserves memory data
```
