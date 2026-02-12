---
description: 添加原子事实到知识图谱
argument-hint: <area/entity> <fact description>
allowed-tools: Read, Write, Edit, Bash
---

向记忆系统的知识图谱中添加一条原子事实。

**目标区域/实体:** $1
**事实描述:** $2

## 指令

1. 解析参数:
   - `$1` 是 `area/entity` 格式，如 `projects/hotMsg`、`patterns/tdd`、`tools/docker`
   - `$2` 是事实的具体描述
   - 如果参数缺失，提示用户正确用法

2. 检查目标目录是否存在:
   - 路径: `~/.claude/memory/areas/$1/`
   - 如果目录不存在，创建目录并初始化 `summary.md` 和 `facts.json`

3. 读取现有的 `facts.json`:
   - 路径: `~/.claude/memory/areas/$1/facts.json`
   - 如果文件不存在，创建初始结构 `{"facts": []}`

4. 生成新的事实条目:
   ```json
   {
     "id": "fact-NNN",
     "fact": "$2 的内容",
     "evidence": "用户手动添加",
     "timestamp": "当前日期 YYYY-MM-DD",
     "status": "active",
     "category": "manual"
   }
   ```
   - id 从现有最大编号递增
   - timestamp 使用当前日期

5. 将新事实追加到 facts 数组中，保存文件

6. 确认输出:
   - 显示添加的事实内容
   - 显示当前该实体的活跃事实总数

## 用法示例

```
/memory-add projects/hotMsg "WebSocket 连接需要心跳保活，间隔 30 秒"
/memory-add patterns/error-handling "使用 Result 类型替代 try-catch 可提高可读性"
/memory-add tools/docker "多阶段构建可减少 70% 镜像体积"
```
