# 三层记忆架构设计文档

## 概述

claude-memory 是一个基于文件系统的 Claude Code 记忆系统。设计目标:

1. **零外部依赖** — 只用文件系统和 SQLite，不需要向量数据库或云服务
2. **节省上下文** — 按需加载，只在需要时读取相关文件
3. **自动化驱动** — Hook 机制自动记录，减少手动维护负担
4. **优雅降级** — 每一层都有降级方案，核心功能不依赖可选组件

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
- 从 JSONL transcript 提取对话 → LLM 提炼关键知识点
- 单个文件从 ~1MB 原始 transcript 压缩为 ~2KB 摘要
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
- Jaccard trigram 去重（阈值 0.83，参考 lss）

**降级策略:**
- fastembed 已安装 → 自动启用混合搜索
- fastembed 未安装 → 自动降级为纯 BM25
- 用户可通过 `--mode bm25` 显式指定

### 三级: 纯向量语义搜索

通过 `--mode vector` 使用纯语义搜索，适合:
- BM25 关键词搜索无结果时
- 用自然语言描述模糊概念（如"用户认证流程"找到"JWT 校验逻辑"）

## 自动化流程

### Hook 驱动

```
PostToolUse (Edit|Write)
    → extract-memory.sh
    → 更新每日笔记
    → 去重: 同分钟同项目合并

Stop / SessionEnd
    → session-summary.sh
    → 从 JSONL 提取对话 (jq)
    → LLM 摘要 (Haiku, 30s 超时)
    → 保存到 sessions/
    → 索引到每日笔记
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
