# v0.3.0 - 规范无损注入 + Subagent Schema 注入

## 中文

### 新增功能

**规范无损注入** — PreToolUse Hook 通过 `additionalContext` 零浪费注入项目开发规范。

- **rules-injector.sh** (PreToolUse — Read)
  - 首次 Read 项目文件时，通过 `allow + additionalContext` 注入规范
  - Read 不被 block，文件内容与规范同时到达，零浪费
  - 通过 `config.json` 的 `project_name_map` 自动检测项目
  - 支持 `project_groups` 共享规范（如多个 Java 项目共享 `shared-java/rules.md`）

- **rules-gate.sh** (PreToolUse — Edit|Write)
  - 安全网：编辑项目文件时检查规范是否已通过 Read 加载
  - 未加载则 deny，提示先读规范

- **schema-injector.sh** (SubagentStart)
  - 当 Plan/Explore/architect 等设计 subagent 启动时，自动注入 schema 索引
  - subagent 无需手动查询就知道有哪些表、怎么查详细结构
  - 同会话只注入一次

- **pre-compact.sh** (PreCompact)
  - 上下文压缩前自动清除规范注入 flag
  - 压缩后下次 Read 项目文件时自动重新注入规范，不会因压缩丢失

### 变更

- `config/settings.example.json` 新增 PreToolUse 和 SubagentStart hooks 配置
- `config/project-config.example.json` 新增 `project_groups` 字段（项目组共享规范）
- `install.sh` 新增 3 个脚本的安装/卸载，hooks 合并逻辑扩展
- `docs/architecture.md` 新增 Hook 架构设计决策

### 效果对比

| 场景 | v0.2.x | v0.3.0 |
|------|--------|--------|
| 首次 Read 项目文件 | 无规范注入 | allow + additionalContext 零浪费注入 |
| 上下文压缩后 | 规范丢失，无法恢复 | PreCompact 清除 flag → 自动重新注入 |
| 设计阶段 (Plan agent) | 无覆盖 | SubagentStart 注入 schema 索引 |
| Hook API 格式 | 旧格式 | `hookSpecificOutput` 新格式 |

### 升级指南

已安装 v0.2.x 的用户:

```bash
cd code-memory
git pull
bash install.sh  # 会覆盖脚本和命令，不覆盖记忆数据
```

手动升级:
```bash
# 复制新增脚本
cp scripts/rules-injector.sh scripts/rules-gate.sh \
   scripts/schema-injector.sh scripts/pre-compact.sh ~/.claude/scripts/

# 设置权限
chmod +x ~/.claude/scripts/rules-injector.sh \
         ~/.claude/scripts/rules-gate.sh \
         ~/.claude/scripts/schema-injector.sh \
         ~/.claude/scripts/pre-compact.sh

# 配置项目映射 (可选，如需要 project_name_map 或 project_groups)
vim ~/.claude/memory/config.json

# 手动添加 hooks 到 settings.json (参考 config/settings.example.json)
```

### 配置说明

#### project_name_map（目录名 → 项目名映射）

如果代码仓库目录名与记忆系统项目名不同，配置映射:

```json
{
  "project_name_map": {
    "my-backend-parent": "backend-api"
  }
}
```

当 Read 路径包含 `/my-backend-parent/` 时，自动匹配到 `backend-api` 项目。

#### project_groups（项目组共享规范）

多个项目共享同一套规范时，配置组:

```json
{
  "project_groups": {
    "shared-java": ["backend-api", "worker-service", "data-pipeline"]
  }
}
```

Read `backend-api` 项目文件时，除加载 `backend-api/rules.md`，还会追加 `shared-java/rules.md`。

---

## English

### What's New

**Zero-Waste Rules Injection** — PreToolUse Hook injects project rules via `additionalContext` without blocking Read.

- **rules-injector.sh** (PreToolUse — Read)
  - On first Read of project files, injects rules via `allow + additionalContext`
  - Read is NOT blocked — file content and rules arrive simultaneously
  - Auto-detects projects via `config.json` `project_name_map`
  - Supports `project_groups` for shared rules across projects

- **rules-gate.sh** (PreToolUse — Edit|Write)
  - Safety net: denies Edit if project rules haven't been loaded via Read

- **schema-injector.sh** (SubagentStart)
  - Injects DB schema index when Plan/Explore/architect subagents start
  - Subagents know what tables exist without manual lookup
  - Injected once per session

- **pre-compact.sh** (PreCompact)
  - Clears rules injection flags before context compaction
  - Rules auto-re-inject on next Read after compaction, never lost

### Changes

- `config/settings.example.json` adds PreToolUse and SubagentStart hooks
- `config/project-config.example.json` adds `project_groups` field
- `install.sh` includes 3 new scripts, expanded hooks merging
- `docs/architecture.md` adds Hook architecture design decisions

### Upgrade Guide

For existing v0.2.x users:

```bash
cd code-memory
git pull
bash install.sh
```
