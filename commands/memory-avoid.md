---
description: 记录避坑经验到隐性知识
argument-hint: <pitfall description>
allowed-tools: Read, Write, Edit
---

将一条避坑经验记录到 MEMORY.md 的「避坑经验」部分。

**避坑描述:** $ARGUMENTS

## 指令

1. 解析参数:
   - `$ARGUMENTS` 是完整的避坑经验描述
   - 如果参数为空，提示用户正确用法

2. 读取 `~/.claude/memory/MEMORY.md`

3. 找到 `## 避坑经验` 部分:
   - 在该部分的 HTML 注释 `<!-- 由 Hooks 自动更新 -->` 之后添加新条目
   - 如果注释不存在，在该 section 末尾添加
   - 格式: `- [当前日期] <描述>`

4. 保存文件

5. 确认输出:
   - 显示添加的避坑经验
   - 显示当前避坑经验总数

## 用法示例

```
/memory-avoid macOS 上 sed -i 需要空字符串参数 sed -i ''，Linux 不需要
/memory-avoid Docker 容器内 localhost 不指向宿主机，需要用 host.docker.internal
/memory-avoid git rebase -i 在 Claude Code 中不可用，因为不支持交互式输入
```
