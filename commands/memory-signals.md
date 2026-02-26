---
description: "跨会话信号分析 (循环问题检测 + 频率统计)"
argument-hint: "[analyze | recurring | resolve <id> | backfill]"
allowed-tools: Read, Bash
---

# 跨会话信号分析

## 参数说明

- `analyze` (默认) - 分析近 14 天的信号
- `recurring` - 显示活跃的循环问题
- `resolve <id>` - 标记循环模式为已解决
- `backfill` - 从现有会话历史回填信号数据

## 执行步骤

### analyze (默认)

```bash
python3 ~/.claude/scripts/signal-analyzer.py analyze --days 14
```

### recurring

```bash
python3 ~/.claude/scripts/signal-analyzer.py recurring
```

### resolve

```bash
python3 ~/.claude/scripts/signal-analyzer.py resolve --pattern-id <id> --resolution "解决方案描述"
```

### backfill

```bash
python3 ~/.claude/scripts/signal-analyzer.py backfill --days 30
```

## 输出

将分析结果以结构化方式呈现，包括：
- 信号频率排名
- 被抑制的高频信号
- 循环模式清单及优先级
- 警告和建议
