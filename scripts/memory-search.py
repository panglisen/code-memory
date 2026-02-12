#!/usr/bin/env python3
"""
memory-search.py - 记忆系统混合搜索工具 (BM25 + Vector + RRF)

基于 SQLite FTS5 + fastembed (ONNX Runtime) 实现。
BM25 零外部依赖；向量搜索需 fastembed（可选，pip install fastembed）。

用法:
    python3 ~/.claude/scripts/memory-search.py "关键词"
    python3 ~/.claude/scripts/memory-search.py --rebuild       # 重建 BM25 + 向量索引
    python3 ~/.claude/scripts/memory-search.py --rebuild-bm25  # 仅重建 BM25
    python3 ~/.claude/scripts/memory-search.py --rebuild-vec   # 仅重建向量索引
    python3 ~/.claude/scripts/memory-search.py --stats         # 查看索引统计
    python3 ~/.claude/scripts/memory-search.py --mode bm25 "关键词"   # 仅 BM25
    python3 ~/.claude/scripts/memory-search.py --mode vector "关键词" # 仅向量
    python3 ~/.claude/scripts/memory-search.py --mode hybrid "关键词" # RRF 混合 (默认)

三级检索架构:
    一级: 精确匹配 (memory-loader.md 规则表) - 由 LLM 执行
    二级: RRF 混合搜索 (本脚本) - BM25 + 向量 → RRF 融合
    三级: 纯向量语义搜索 (本脚本 --mode vector)

向量模型优先级 (fastembed 支持的，自动检测本地缓存):
    1. BAAI/bge-small-zh-v1.5  (中文优化, 384d, ~90MB)
    2. BAAI/bge-small-en-v1.5  (英文, 384d, fastembed 默认)
"""

import sqlite3
import os
import sys
import json
import glob
import re
import struct
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

MEMORY_DIR = os.path.expanduser("~/.claude/memory")
DB_PATH = os.path.join(MEMORY_DIR, ".search-index.db")
MAX_RESULTS = 6
MIN_SNIPPET_CONTEXT = 60  # FTS5 snippet 上下文字符数

# RRF 参数 (k=60 是标准值，参考 Cormack et al. 2009)
RRF_K = 60

# Jaccard 去重阈值 (参考 lss: 0.83)
JACCARD_THRESHOLD = 0.83

# 向量搜索候选数 (取更多候选再 RRF 融合)
VECTOR_CANDIDATES = 20
BM25_CANDIDATES = 20

# ============================================================
# 向量搜索模块 (可选依赖)
# ============================================================

_embedding_model = None
_embedding_dim = None
_model_name = None


def _detect_best_model() -> Optional[str]:
    """自动检测 fastembed 可用的最优嵌入模型"""
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None

    # fastembed 支持的中文优化模型优先
    candidates = [
        "BAAI/bge-small-zh-v1.5",
        "BAAI/bge-small-en-v1.5",
    ]

    # 检查本地 ONNX 缓存
    cache_dir = os.path.expanduser("~/.cache/fastembed_cache")
    if os.path.isdir(cache_dir):
        for model_name in candidates:
            # fastembed 缓存格式: models--BAAI--bge-small-zh-v1.5 或类似
            safe_name = model_name.replace("/", "--")
            for entry in os.listdir(cache_dir):
                if safe_name.lower() in entry.lower():
                    return model_name

    # 默认使用中文模型
    return candidates[0]


def _get_embedding_model():
    """懒加载 fastembed 嵌入模型"""
    global _embedding_model, _embedding_dim, _model_name

    if _embedding_model is not None:
        return _embedding_model

    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None

    _model_name = _detect_best_model()
    if _model_name is None:
        return None

    print(f"  加载嵌入模型: {_model_name} ...", file=sys.stderr, end="", flush=True)

    try:
        _embedding_model = TextEmbedding(model_name=_model_name)
        # fastembed bge-small 系列维度为 384
        _embedding_dim = 384
        print(f" 完成 (dim={_embedding_dim})", file=sys.stderr)
        return _embedding_model
    except Exception as e:
        print(f" 失败: {e}", file=sys.stderr)
        _embedding_model = None
        return None


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成嵌入向量"""
    model = _get_embedding_model()
    if model is None:
        return []

    # fastembed.embed() 返回 generator of numpy arrays
    embeddings = list(model.embed(texts))
    return [emb.tolist() for emb in embeddings]


def _embed_query(query: str) -> Optional[list[float]]:
    """生成查询向量"""
    model = _get_embedding_model()
    if model is None:
        return None

    # fastembed.query_embed() 针对查询优化 (添加 "query: " 前缀)
    embeddings = list(model.query_embed(query))
    if embeddings:
        return embeddings[0].tolist()
    return None


# ============================================================
# 向量序列化 (SQLite BLOB 存储)
# ============================================================

def _vec_to_blob(vec: list[float]) -> bytes:
    """向量 → BLOB (little-endian float32)"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """BLOB → 向量"""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算余弦相似度 (已归一化向量 = 点积)"""
    return sum(x * y for x, y in zip(a, b))


# ============================================================
# 数据库初始化
# ============================================================

def get_file_hash(filepath: str) -> str:
    """计算文件变更指纹 (mtime + size，比 md5 更快)"""
    stat = os.stat(filepath)
    return f"{stat.st_mtime}:{stat.st_size}"


def init_db(conn: sqlite3.Connection) -> None:
    """初始化数据库表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            hash TEXT NOT NULL,
            category TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            path,
            category,
            title,
            content,
            tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS chunks_meta (
            rowid INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            line_start INTEGER,
            line_end INTEGER
        );

        -- 向量索引表
        CREATE TABLE IF NOT EXISTS embeddings (
            chunk_rowid INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            model TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );

        -- 向量索引元数据
        CREATE TABLE IF NOT EXISTS vec_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)


# ============================================================
# 文件分类与分块 (复用原有逻辑)
# ============================================================

def categorize_file(filepath: str) -> str:
    """根据文件路径推断分类"""
    rel = filepath.replace(MEMORY_DIR + "/", "")

    if rel.startswith("areas/projects/shared-db/"):
        return "schema"
    elif rel.startswith("areas/projects/") and "/rules.md" in rel:
        return "rules"
    elif rel.startswith("areas/projects/") and "/summary.md" in rel:
        return "project-summary"
    elif rel.startswith("areas/projects/") and "facts.json" in rel:
        return "facts"
    elif rel.startswith("areas/patterns/"):
        return "pattern"
    elif rel.startswith("areas/tools/"):
        return "tool"
    elif rel.startswith("daily/"):
        return "daily"
    elif rel.startswith("sessions/"):
        return "session"
    elif rel == "MEMORY.md":
        return "memory"
    else:
        return "other"


def chunk_markdown(content: str, filepath: str, max_chunk_lines: int = 30) -> list:
    """
    按 Markdown 标题分块。
    借鉴 OpenClaw: 按语义边界分块，而非固定字符数。
    """
    lines = content.split("\n")
    chunks = []
    current_title = os.path.basename(filepath)
    current_lines = []
    current_start = 1

    for i, line in enumerate(lines, 1):
        if line.startswith("#") and current_lines:
            # 保存当前块
            chunk_text = "\n".join(current_lines).strip()
            if len(chunk_text) > 20:
                chunks.append({
                    "title": current_title,
                    "content": chunk_text,
                    "line_start": current_start,
                    "line_end": i - 1,
                })
            current_title = line.lstrip("#").strip() or current_title
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

            # 超过最大行数时强制分块
            if len(current_lines) >= max_chunk_lines:
                chunk_text = "\n".join(current_lines).strip()
                if len(chunk_text) > 20:
                    chunks.append({
                        "title": current_title,
                        "content": chunk_text,
                        "line_start": current_start,
                        "line_end": i,
                    })
                current_lines = []
                current_start = i + 1

    # 最后一块
    if current_lines:
        chunk_text = "\n".join(current_lines).strip()
        if len(chunk_text) > 20:
            chunks.append({
                "title": current_title,
                "content": chunk_text,
                "line_start": current_start,
                "line_end": len(lines),
            })

    return chunks


def chunk_json(content: str, filepath: str) -> list:
    """处理 JSON 文件 (facts.json, prefix-map.json)"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return [{"title": os.path.basename(filepath), "content": content[:2000], "line_start": 1, "line_end": 1}]

    chunks = []

    if "facts" in data and isinstance(data["facts"], list):
        # facts.json 格式: id, fact, category, status, evidence, timestamp
        for fact in data["facts"]:
            if fact.get("status") == "active":
                fact_text = fact.get("fact", "")
                text = f"[{fact.get('category', '')}] {fact_text}"
                if fact.get("evidence"):
                    text += f" (evidence: {fact['evidence']})"
                chunks.append({
                    "title": fact.get("id", os.path.basename(filepath)),
                    "content": text,
                    "line_start": 1,
                    "line_end": 1,
                })
    elif isinstance(data, dict):
        # prefix-map.json 等字典格式: 按 key 分块
        for key, value in data.items():
            chunks.append({
                "title": key,
                "content": f"{key}: {json.dumps(value, ensure_ascii=False)[:500]}",
                "line_start": 1,
                "line_end": 1,
            })

    return chunks


# ============================================================
# 索引构建
# ============================================================

def index_file(conn: sqlite3.Connection, filepath: str) -> int:
    """索引单个文件 (BM25)，返回新增块数"""
    file_hash = get_file_hash(filepath)
    category = categorize_file(filepath)

    # 检查是否需要更新
    existing = conn.execute(
        "SELECT hash FROM files WHERE path = ?", (filepath,)
    ).fetchone()

    if existing and existing[0] == file_hash:
        return 0  # 文件未变，跳过

    # 删除旧索引 (先获取 rowid 确保 chunks 和 chunks_meta 一致)
    old_rowids = conn.execute(
        "SELECT rowid FROM chunks WHERE path = ?", (filepath,)
    ).fetchall()
    conn.execute("DELETE FROM chunks WHERE path = ?", (filepath,))
    for (rid,) in old_rowids:
        conn.execute("DELETE FROM chunks_meta WHERE rowid = ?", (rid,))
        conn.execute("DELETE FROM embeddings WHERE chunk_rowid = ?", (rid,))

    # 读取文件
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 分块
    if filepath.endswith(".json"):
        chunks = chunk_json(content, filepath)
    else:
        chunks = chunk_markdown(content, filepath)

    # 写入索引
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO chunks (path, category, title, content) VALUES (?, ?, ?, ?)",
            (filepath, category, chunk["title"], chunk["content"]),
        )
        conn.execute(
            "INSERT INTO chunks_meta (rowid, path, line_start, line_end) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, filepath, chunk.get("line_start", 0), chunk.get("line_end", 0)),
        )

    # 更新文件记录
    conn.execute(
        "INSERT OR REPLACE INTO files (path, hash, category, updated_at) VALUES (?, ?, ?, ?)",
        (filepath, file_hash, category, datetime.now().isoformat()),
    )

    return len(chunks)


def build_embeddings(conn: sqlite3.Connection, force: bool = False) -> int:
    """为所有未嵌入的 chunks 生成向量，返回新增数"""
    model = _get_embedding_model()
    if model is None:
        print("  向量索引跳过 (fastembed 不可用，pip install fastembed)", file=sys.stderr)
        return 0

    current_model = _model_name or "unknown"

    # 检查模型是否变更 (模型切换时需全量重建)
    stored_model = conn.execute(
        "SELECT value FROM vec_meta WHERE key = 'model_name'"
    ).fetchone()

    if stored_model and stored_model[0] != current_model:
        print(f"  模型变更: {stored_model[0]} → {current_model}，清空向量索引", file=sys.stderr)
        conn.execute("DELETE FROM embeddings")
        force = True

    conn.execute(
        "INSERT OR REPLACE INTO vec_meta (key, value) VALUES ('model_name', ?)",
        (current_model,),
    )

    # 查找需要嵌入的 chunks
    if force:
        rows = conn.execute("""
            SELECT c.rowid, c.title, c.content
            FROM chunks c
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT c.rowid, c.title, c.content
            FROM chunks c
            LEFT JOIN embeddings e ON c.rowid = e.chunk_rowid
            WHERE e.chunk_rowid IS NULL
        """).fetchall()

    if not rows:
        return 0

    print(f"  生成向量: {len(rows)} 个 chunks ...", file=sys.stderr, end="", flush=True)

    # 准备文本 (title + content 拼接，增强语义)
    texts = [f"{row[1]}: {row[2]}" for row in rows]
    rowids = [row[0] for row in rows]

    # 批量嵌入
    embeddings = _embed_texts(texts)
    if not embeddings:
        print(" 失败", file=sys.stderr)
        return 0

    # 写入数据库
    for rowid, text, emb in zip(rowids, texts, embeddings):
        content_hash = hashlib.md5(text.encode()).hexdigest()[:16]
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (chunk_rowid, embedding, model, content_hash) VALUES (?, ?, ?, ?)",
            (rowid, _vec_to_blob(emb), current_model, content_hash),
        )

    conn.commit()
    print(f" 完成", file=sys.stderr)
    return len(embeddings)


def collect_files() -> list:
    """收集所有需要索引的文件"""
    files = []
    patterns = [
        os.path.join(MEMORY_DIR, "MEMORY.md"),
        os.path.join(MEMORY_DIR, "areas", "**", "*.md"),
        os.path.join(MEMORY_DIR, "areas", "**", "*.json"),
        os.path.join(MEMORY_DIR, "daily", "*.md"),
        os.path.join(MEMORY_DIR, "sessions", "*.md"),
    ]

    for pattern in patterns:
        files.extend(glob.glob(pattern, recursive=True))

    return sorted(set(files))


def rebuild_index(rebuild_bm25: bool = True, rebuild_vec: bool = True) -> None:
    """重建索引"""
    if rebuild_bm25:
        # 删除旧数据库
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)

        conn = sqlite3.connect(DB_PATH)
        init_db(conn)

        files = collect_files()
        total_chunks = 0

        for filepath in files:
            try:
                count = index_file(conn, filepath)
                total_chunks += count
                if count > 0:
                    rel = filepath.replace(MEMORY_DIR + "/", "")
                    print(f"  索引: {rel} ({count} 块)")
            except Exception as e:
                print(f"  跳过: {filepath} ({e})", file=sys.stderr)

        conn.commit()
        print(f"\nBM25 索引完成: {len(files)} 个文件, {total_chunks} 个块")
    else:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)

    if rebuild_vec:
        count = build_embeddings(conn, force=True)
        print(f"向量索引完成: {count} 个嵌入")

    conn.close()


def sync_index(conn: sqlite3.Connection) -> int:
    """增量同步索引，返回更新的文件数"""
    files = collect_files()
    updated = 0

    for filepath in files:
        try:
            count = index_file(conn, filepath)
            if count > 0:
                updated += 1
        except Exception:
            pass

    if updated > 0:
        conn.commit()

    return updated


# ============================================================
# BM25 搜索
# ============================================================

def sanitize_fts_query(query: str) -> str:
    """
    转义 FTS5 特殊字符，优化中文搜索。

    unicode61 tokenizer 将连续 CJK 字符作为整体 token（如"解密失败"是一个 token），
    而非拆成单字。因此用户搜"解密"不会匹配"解密失败"。
    解决方案: CJK 词使用前缀匹配 (token*)，英文 token 用双引号包裹。
    """
    tokens = query.split()
    sanitized = []

    for t in tokens:
        t = t.strip()
        if not t:
            continue

        # 检查是否包含 CJK 字符
        cjk_part = re.sub(r'[^\u4e00-\u9fff\u3400-\u4dbf]', '', t)
        non_cjk = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf]', '', t).strip()

        if cjk_part and not non_cjk:
            # 纯中文: 前缀匹配 (如 "积分" -> 积分*)
            sanitized.append(f'{cjk_part}*')
        elif cjk_part and non_cjk:
            # 混合: CJK 前缀匹配 + 英文引号包裹
            sanitized.append(f'{cjk_part}*')
            escaped_non_cjk = non_cjk.replace('"', '""')
            sanitized.append(f'"{escaped_non_cjk}"')
        else:
            # 纯英文/数字: 引号包裹
            escaped = t.replace('"', '""')
            sanitized.append(f'"{escaped}"')

    return " ".join(sanitized)


def bm25_search(conn: sqlite3.Connection, query: str,
                category_filter: str = None,
                max_results: int = BM25_CANDIDATES) -> list:
    """
    BM25 全文搜索

    返回:
        [{"rowid": int, "path": str, "category": str, "title": str,
          "snippet": str, "content": str, "score": float}]
    """
    sanitized_query = sanitize_fts_query(query)

    where_clause = ""
    params = [sanitized_query]

    if category_filter:
        where_clause = "AND category = ?"
        params.append(category_filter)

    params.append(max_results)

    try:
        results = conn.execute(f"""
            SELECT
                chunks.rowid,
                path,
                category,
                title,
                snippet(chunks, 3, '>>>', '<<<', '...', {MIN_SNIPPET_CONTEXT}) as snippet,
                content,
                rank
            FROM chunks
            WHERE chunks MATCH ?
            {where_clause}
            ORDER BY rank
            LIMIT ?
        """, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"搜索语法错误: {e}", file=sys.stderr)
        results = []

    return [
        {
            "rowid": r[0],
            "path": r[1].replace(MEMORY_DIR + "/", ""),
            "category": r[2],
            "title": r[3],
            "snippet": r[4],
            "content": r[5],
            "score": round(-r[6], 4),
        }
        for r in results
    ]


# ============================================================
# 向量搜索
# ============================================================

def vector_search(conn: sqlite3.Connection, query: str,
                  category_filter: str = None,
                  max_results: int = VECTOR_CANDIDATES) -> list:
    """
    向量余弦相似度搜索

    返回:
        [{"rowid": int, "path": str, "category": str, "title": str,
          "snippet": str, "content": str, "score": float}]
    """
    query_vec = _embed_query(query)
    if query_vec is None:
        return []

    # 从数据库加载所有向量 (语料小，全量加载可接受)
    if category_filter:
        rows = conn.execute("""
            SELECT e.chunk_rowid, e.embedding, c.path, c.category, c.title, c.content
            FROM embeddings e
            JOIN chunks c ON c.rowid = e.chunk_rowid
            WHERE c.category = ?
        """, (category_filter,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT e.chunk_rowid, e.embedding, c.path, c.category, c.title, c.content
            FROM embeddings e
            JOIN chunks c ON c.rowid = e.chunk_rowid
        """).fetchall()

    if not rows:
        return []

    # 计算相似度
    scored = []
    for row in rows:
        chunk_vec = _blob_to_vec(row[1])
        sim = _cosine_similarity(query_vec, chunk_vec)
        scored.append({
            "rowid": row[0],
            "path": row[2].replace(MEMORY_DIR + "/", ""),
            "category": row[3],
            "title": row[4],
            "snippet": row[5][:200],  # 向量搜索没有 FTS5 snippet，取前 200 字符
            "content": row[5],
            "score": round(sim, 4),
        })

    # 按相似度排序
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:max_results]


# ============================================================
# RRF 融合 + 去重
# ============================================================

def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """基于字符级 n-gram 的 Jaccard 相似度 (用于去重)"""
    n = 3  # trigram
    if len(text_a) < n or len(text_b) < n:
        return 1.0 if text_a == text_b else 0.0

    set_a = {text_a[i:i+n] for i in range(len(text_a) - n + 1)}
    set_b = {text_b[i:i+n] for i in range(len(text_b) - n + 1)}

    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def rrf_fuse(bm25_results: list, vector_results: list,
             k: int = RRF_K, max_results: int = MAX_RESULTS) -> list:
    """
    Reciprocal Rank Fusion (RRF)

    公式: score(d) = Σ 1/(k + rank_i(d))

    其中 rank_i(d) 是文档 d 在第 i 个排名列表中的位置 (从 1 开始)。
    k=60 是标准值，平衡高排名和低排名文档的影响。

    参数:
        bm25_results: BM25 搜索结果
        vector_results: 向量搜索结果
        k: RRF 常数 (默认 60)
        max_results: 最终返回数

    返回:
        融合后的结果列表，每个结果包含 rrf_score 和 sources 字段
    """
    # 以 rowid 为 key 汇总分数
    fused = {}

    for rank, result in enumerate(bm25_results, 1):
        rid = result["rowid"]
        if rid not in fused:
            fused[rid] = {**result, "rrf_score": 0.0, "sources": [], "bm25_rank": None, "vec_rank": None}
        fused[rid]["rrf_score"] += 1.0 / (k + rank)
        fused[rid]["sources"].append("bm25")
        fused[rid]["bm25_rank"] = rank

    for rank, result in enumerate(vector_results, 1):
        rid = result["rowid"]
        if rid not in fused:
            fused[rid] = {**result, "rrf_score": 0.0, "sources": [], "bm25_rank": None, "vec_rank": None}
        fused[rid]["rrf_score"] += 1.0 / (k + rank)
        fused[rid]["sources"].append("vector")
        fused[rid]["vec_rank"] = rank
        # 向量搜索的 snippet 可能不如 BM25 的（没有高亮），优先用 BM25 的
        if "bm25" in fused[rid]["sources"] and ">>>" in fused[rid].get("snippet", ""):
            pass  # 保留 BM25 的 snippet
        elif ">>>" not in fused[rid].get("snippet", ""):
            fused[rid]["snippet"] = result["snippet"]

    # 按 RRF 分数排序
    ranked = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)

    # Jaccard 去重 (参考 lss)
    deduped = []
    for item in ranked:
        is_dup = False
        for existing in deduped:
            if _jaccard_similarity(item.get("content", ""), existing.get("content", "")) > JACCARD_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            deduped.append(item)
        if len(deduped) >= max_results:
            break

    return deduped


# ============================================================
# 统一搜索入口
# ============================================================

def search(query: str, category_filter: str = None,
           max_results: int = MAX_RESULTS,
           mode: str = "hybrid") -> list:
    """
    统一搜索入口

    参数:
        query: 搜索关键词
        category_filter: 可选分类过滤
        max_results: 最大结果数
        mode: 搜索模式 - "hybrid" (RRF), "bm25", "vector"

    返回:
        [{"path": str, "category": str, "title": str, "snippet": str, "score": float, ...}]
    """
    if not os.path.exists(DB_PATH):
        rebuild_index()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)

    # 增量同步 BM25
    sync_index(conn)

    # 增量同步向量 (仅 hybrid/vector 模式)
    if mode in ("hybrid", "vector"):
        build_embeddings(conn)

    if mode == "bm25":
        results = bm25_search(conn, query, category_filter, max_results)
        for r in results:
            r["sources"] = ["bm25"]
        conn.close()
        return results[:max_results]

    elif mode == "vector":
        results = vector_search(conn, query, category_filter, max_results)
        for r in results:
            r["sources"] = ["vector"]
        conn.close()
        return results[:max_results]

    else:  # hybrid (RRF)
        bm25_results = bm25_search(conn, query, category_filter)
        vector_results = vector_search(conn, query, category_filter)

        conn.close()

        if not vector_results:
            # 向量搜索不可用，降级为纯 BM25
            for r in bm25_results:
                r["sources"] = ["bm25"]
            return bm25_results[:max_results]

        return rrf_fuse(bm25_results, vector_results, max_results=max_results)


# ============================================================
# 统计与输出
# ============================================================

def show_stats() -> None:
    """显示索引统计"""
    if not os.path.exists(DB_PATH):
        print("索引不存在，请先运行 --rebuild")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    print(f"索引统计:")
    print(f"  文件数: {file_count}")
    print(f"  块数: {chunk_count}")
    print(f"  数据库: {DB_PATH}")
    print(f"  大小: {os.path.getsize(DB_PATH) / 1024:.1f} KB")
    print()

    # 向量索引统计
    emb_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    stored_model = conn.execute(
        "SELECT value FROM vec_meta WHERE key = 'model_name'"
    ).fetchone()

    print(f"向量索引:")
    print(f"  嵌入数: {emb_count}/{chunk_count}")
    print(f"  模型: {stored_model[0] if stored_model else '未设置'}")
    if emb_count > 0:
        # 计算向量维度
        sample = conn.execute("SELECT embedding FROM embeddings LIMIT 1").fetchone()
        if sample:
            dim = len(sample[0]) // 4
            print(f"  维度: {dim}")
            vec_size = emb_count * len(sample[0])
            print(f"  向量存储: {vec_size / 1024:.1f} KB")
    print()

    print("按分类:")
    for row in conn.execute(
        "SELECT category, COUNT(*) FROM files GROUP BY category ORDER BY COUNT(*) DESC"
    ):
        print(f"  {row[0]}: {row[1]} 个文件")

    conn.close()


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 memory-search.py <关键词>             # 混合搜索 (RRF)")
        print('  python3 memory-search.py -c <分类> <关键词>    # 按分类搜索')
        print("  python3 memory-search.py --mode bm25 <关键词>  # 仅 BM25")
        print("  python3 memory-search.py --mode vector <关键词> # 仅向量")
        print("  python3 memory-search.py --rebuild             # 重建全部索引")
        print("  python3 memory-search.py --rebuild-bm25        # 仅重建 BM25")
        print("  python3 memory-search.py --rebuild-vec         # 仅重建向量")
        print("  python3 memory-search.py --stats               # 查看统计")
        print()
        print("分类: schema, rules, project-summary, facts, pattern, tool, daily, session, memory")
        sys.exit(1)

    if sys.argv[1] == "--rebuild":
        rebuild_index(rebuild_bm25=True, rebuild_vec=True)
        return

    if sys.argv[1] == "--rebuild-bm25":
        rebuild_index(rebuild_bm25=True, rebuild_vec=False)
        return

    if sys.argv[1] == "--rebuild-vec":
        rebuild_index(rebuild_bm25=False, rebuild_vec=True)
        return

    if sys.argv[1] == "--stats":
        show_stats()
        return

    # 解析参数
    category = None
    mode = "hybrid"
    query_args = []
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "-c" and i + 1 < len(sys.argv):
            category = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--mode" and i + 1 < len(sys.argv):
            mode = sys.argv[i + 1]
            i += 2
        else:
            query_args.append(sys.argv[i])
            i += 1

    query = " ".join(query_args)

    if not query:
        print("错误: 请提供搜索关键词", file=sys.stderr)
        sys.exit(1)

    results = search(query, category_filter=category, mode=mode)

    if not results:
        print(f'未找到 "{query}" 的相关结果')
        return

    print(f'搜索 "{query}" 的结果 ({len(results)} 条, 模式: {mode}):\n')

    for i, r in enumerate(results, 1):
        sources = r.get("sources", [])
        source_tag = "+".join(sources) if sources else "?"
        score_key = "rrf_score" if "rrf_score" in r else "score"
        score_val = r.get(score_key, 0)

        detail_parts = []
        if r.get("bm25_rank"):
            detail_parts.append(f"BM25#{r['bm25_rank']}")
        if r.get("vec_rank"):
            detail_parts.append(f"Vec#{r['vec_rank']}")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""

        print(f"[{i}] {r['title']}")
        print(f"    文件: {r['path']} ({r['category']})")
        print(f"    来源: {source_tag} | 分数: {score_val:.4f}{detail}")
        print(f"    片段: {r['snippet']}")
        print()


if __name__ == "__main__":
    main()
