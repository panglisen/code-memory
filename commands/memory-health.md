---
description: "记忆系统健康看板 (经验有效性 + 信号分析 + 能力生成)"
argument-hint: "[feedback | signals | capabilities | full]"
allowed-tools: Read, Bash
---

# 记忆系统健康看板

根据用户指定的模块（或默认 full），执行对应的健康检查。

## 参数说明

- `feedback` - 仅查看经验有效性报告
- `signals` - 仅查看信号分析报告
- `capabilities` - 仅查看能力生成状态
- `full` (默认) - 查看所有模块

## 执行步骤

### 1. 经验有效性 (feedback / full)

运行以下命令查看经验有效性报告：

```bash
python3 ~/.claude/scripts/memory-feedback.py report
```

如果用户需要 JSON 格式：

```bash
python3 ~/.claude/scripts/memory-feedback.py report --json
```

查看低效经验候选：

```bash
python3 ~/.claude/scripts/memory-feedback.py stale --threshold 0.3
```

### 2. 信号分析 (signals / full)

运行信号分析：

```bash
python3 ~/.claude/scripts/signal-analyzer.py analyze --days 14
```

查看活跃循环模式：

```bash
python3 ~/.claude/scripts/signal-analyzer.py recurring
```

查看统计：

```bash
python3 ~/.claude/scripts/signal-analyzer.py stats
```

### 3. 能力生成 (capabilities / full)

查看生成器状态：

```bash
python3 ~/.claude/scripts/capability-generator.py --status
```

预览可生成的能力：

```bash
python3 ~/.claude/scripts/capability-generator.py --dry-run --force
```

### 4. 综合建议

根据以上数据，给出以下建议：
- 低效经验是否需要归档或修订
- 循环模式是否需要手动介入
- 是否应该触发能力生成

## 输出格式

以结构化的 Markdown 看板形式输出，包含指标、趋势和建议。
