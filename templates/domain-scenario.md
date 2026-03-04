# {业务域名称}

> 业务架构知识，不记录具体文件路径/类名。实际开发时用搜索关键词动态定位代码。

## 业务概述

[一段话描述这个业务域的核心功能和定位]

## 服务职责划分

| 服务 | 职责 | 搜索关键词 |
|------|------|-----------|
| service-a | [职责描述] | `Controller`, `Service` |
| service-b | [职责描述] | `Handler`, `Processor` |

## 核心业务流程

### 1. [流程名称]
```
触发条件
  ├─ 步骤 1
  ├─ 步骤 2
  └─ 步骤 3
```

## 核心表

| 表名 | 用途 |
|------|------|
| table_a | [用途描述] |
| table_b | [用途描述] |

## 跨服务调用链

```
service-a
  → [HTTP/Feign] service-b
    ├─ [MQ] → service-c
    └─ [DB] → table_a
```

## MQ 消息

| 类型 | 用途 | 方向 |
|------|------|------|
| RocketMQ (topic) | [用途] | service-a → service-b |

## 改动影响域 Checklist

> 修改该业务域时，逐项检查以下联动点。
> 每项提供搜索关键词，用 Grep 在实际代码中验证。

### [子模块 1]
- [ ] [检查项] → 搜索 `SearchKeyword`
- [ ] [检查项] → 搜索 `SearchKeyword`

### [子模块 2]
- [ ] [检查项] → 搜索 `SearchKeyword`

### 在其他模块的引用
- [ ] [引用点] → 搜索 `SearchKeyword`
