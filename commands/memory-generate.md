---
description: "手动触发能力自动生成 (从循环模式中生成 Skill/Command)"
argument-hint: "[--dry-run | --force | --pattern-id <id>]"
allowed-tools: Read, Write, Bash, Glob
---

# 能力自动生成

从循环模式中自动生成 Claude Code Skill 或 Command。

## 参数说明

- `--dry-run` - 预览模式，不实际生成文件
- `--force` - 跳过门控检查强制生成
- `--pattern-id <id>` - 针对特定循环模式生成

## 执行步骤

### 1. 查看当前状态

```bash
python3 ~/.claude/scripts/capability-generator.py --status
```

### 2. 预览候选

```bash
python3 ~/.claude/scripts/capability-generator.py --dry-run --force
```

### 3. 执行生成

根据用户参数执行：

```bash
# 默认 (带门控检查)
python3 ~/.claude/scripts/capability-generator.py

# 强制生成
python3 ~/.claude/scripts/capability-generator.py --force

# 针对特定模式
python3 ~/.claude/scripts/capability-generator.py --pattern-id <id> --force
```

### 4. 验证生成结果

生成后检查：
1. 读取生成的 Skill/Command 文件，确认格式正确
2. 确认 `~/.claude/rules/auto-capabilities.md` 已更新
3. 确认审计日志 `~/.claude/memory/evolution/capabilities.jsonl` 已记录

## 门控条件

自动生成需满足以下全部条件：
1. `CAPABILITY_GENERATION` 环境变量不为 `false`
2. 距上次生成 >= 24 小时
3. 至少 2 个 count>=2 的未处理循环模式
4. 最近 5 个会话 >= 60% 成功率
