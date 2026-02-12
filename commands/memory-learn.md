---
description: 学习并记录一个代码模式到知识图谱
argument-hint: <pattern-name> <pattern description>
allowed-tools: Read, Write, Edit, Bash
---

学习一个新的代码模式，记录到记忆系统的 patterns 知识图谱中。

**模式名称:** $1
**模式描述:** $2

## 指令

1. 解析参数:
   - `$1` 是模式名称 (如 `repository-pattern`、`retry-with-backoff`、`event-sourcing`)
   - `$2` 是模式的描述或从当前上下文中学到的内容
   - 如果参数缺失，提示用户正确用法

2. 检查模式目录:
   - 路径: `~/.claude/memory/areas/patterns/$1/`
   - 如果不存在，创建目录

3. 处理 summary.md:
   - 如果已存在，读取并追加新内容到合适的位置
   - 如果不存在，创建新的 summary.md，结构如下:

   ```markdown
   # [模式名称]

   > [模式简要描述]

   ## 适用场景

   - [根据描述推断]

   ## 模式示例

   [如果用户提供了代码或从上下文中可以提取]

   ## 注意事项

   [相关经验和注意点]
   ```

4. 更新 facts.json:
   - 添加一条新事实记录这个模式
   - 如果文件不存在，创建初始结构

5. 确认输出:
   - 显示模式名称和存储路径
   - 显示 summary 的关键内容

## 用法示例

```
/memory-learn retry-with-backoff "网络请求失败时使用指数退避重试，初始 1s，最大 30s，最多 3 次"
/memory-learn repository-pattern "使用 Repository 接口抽象数据访问层，便于测试和切换数据源"
/memory-learn circuit-breaker "当下游服务连续失败 5 次，熔断 60 秒，期间直接返回降级结果"
```
