#!/usr/bin/env python3
"""
memory-feedback.py - 经验有效性追踪引擎

移植自 evolver memoryGraph.js 的 Laplace 平滑 + 指数衰减算法。
追踪每条避坑经验/规范的引用次数和成功率，实现反馈闭环。

用法:
    memory-feedback.py ref --experience-id "avoidance:xxx" --session-id "abc" [--context "..."]
    memory-feedback.py outcome --session-id "abc" --status success [--score 0.85] [--project "xxx"]
    memory-feedback.py report [--json]
    memory-feedback.py stale [--threshold 0.3]
    memory-feedback.py register --experience-id "avoidance:xxx" --source-file "MEMORY.md" [--hash "xxx"]

Python 3.9 兼容。
"""

import sqlite3
import os
import sys
import json
import hashlib
import argparse
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

MEMORY_DIR = os.path.expanduser("~/.claude/memory")
DB_PATH = os.path.join(MEMORY_DIR, ".search-index.db")
AUDIT_PATH = os.path.join(MEMORY_DIR, "evolution", "audit.jsonl")

# 算法参数 (移植自 memoryGraph.js)
DEFAULT_HALF_LIFE = 30.0  # 天 (信号→基因边衰减)


# ============================================================
# 核心算法 (移植自 memoryGraph.js:152-223)
# ============================================================

def decay_weight(updated_at_iso: str, half_life_days: float = DEFAULT_HALF_LIFE) -> float:
    """指数衰减权重 (移植自 memoryGraph.js decayWeight)

    公式: weight = 0.5^(age_days / half_life_days)

    Args:
        updated_at_iso: ISO 格式时间戳
        half_life_days: 半衰期（天）

    Returns:
        衰减权重 [0, 1]
    """
    if half_life_days <= 0:
        return 1.0

    try:
        ts = datetime.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
        now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
        age_days = (now - ts).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 1.0

    if age_days <= 0:
        return 1.0

    return 0.5 ** (age_days / half_life_days)


def expected_success(success: int, fail: int, last_ts: str,
                     half_life: float = DEFAULT_HALF_LIFE) -> Dict[str, float]:
    """Laplace 平滑 + 指数衰减 (移植自 memoryGraph.js)

    公式:
        p = (success + 1) / (success + fail + 2)  # Laplace 平滑
        w = decay_weight(last_ts, half_life)       # 时间衰减
        value = p * w                               # 加权期望

    Args:
        success: 成功次数
        fail: 失败次数
        last_ts: 最后引用时间 (ISO 格式)
        half_life: 半衰期（天）

    Returns:
        {"p": float, "w": float, "total": int, "value": float}
    """
    total = success + fail
    p = (success + 1) / (total + 2)
    w = decay_weight(last_ts, half_life) if last_ts else 1.0
    return {
        "p": round(p, 4),
        "w": round(w, 4),
        "total": total,
        "value": round(p * w, 4),
    }


def trigram_hash(text: str) -> str:
    """计算文本的 trigram hash (用于经验去重识别)"""
    text = re.sub(r"\s+", "", text)
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


# ============================================================
# SQLite Schema
# ============================================================

SCHEMA_SQL = """
-- 经验引用追踪
CREATE TABLE IF NOT EXISTS experience_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experience_id TEXT NOT NULL,
    experience_hash TEXT NOT NULL,
    session_id TEXT NOT NULL,
    referenced_at TEXT NOT NULL,
    context TEXT
);

CREATE INDEX IF NOT EXISTS idx_exp_refs_eid
    ON experience_refs(experience_id);
CREATE INDEX IF NOT EXISTS idx_exp_refs_sid
    ON experience_refs(session_id);

-- 会话结果
CREATE TABLE IF NOT EXISTS session_outcomes (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    has_errors INTEGER DEFAULT 0,
    user_corrections INTEGER DEFAULT 0,
    outcome_status TEXT DEFAULT 'unknown',
    outcome_score REAL DEFAULT 0.5,
    signals TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_outcomes_project
    ON session_outcomes(project);

-- 经验有效性聚合
CREATE TABLE IF NOT EXISTS experience_effectiveness (
    experience_id TEXT PRIMARY KEY,
    experience_hash TEXT NOT NULL,
    source_file TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_referenced TEXT,
    ref_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    laplace_p REAL DEFAULT 0.5,
    decay_value REAL DEFAULT 0.5,
    last_updated TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """初始化反馈追踪表 (不影响已有搜索索引表)"""
    conn.executescript(SCHEMA_SQL)


def get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# ============================================================
# Append-only 审计日志
# ============================================================

def audit_log(event_type: str, data: Dict[str, Any]) -> None:
    """写入 append-only 审计日志"""
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    entry = {
        "type": event_type,
        "ts": datetime.now().isoformat(),
        **data,
    }
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 审计日志写入失败不影响主流程


# ============================================================
# CLI 命令实现
# ============================================================

def cmd_register(args: argparse.Namespace) -> None:
    """注册一条经验到追踪系统"""
    conn = get_conn()
    now = datetime.now().isoformat()
    exp_hash = trigram_hash(args.experience_id)
    if args.hash:
        exp_hash = args.hash

    existing = conn.execute(
        "SELECT experience_id FROM experience_effectiveness WHERE experience_id = ?",
        (args.experience_id,),
    ).fetchone()

    if existing:
        print("experience already registered: %s" % args.experience_id)
        conn.close()
        return

    conn.execute(
        """INSERT INTO experience_effectiveness
           (experience_id, experience_hash, source_file, first_seen,
            last_referenced, ref_count, success_count, fail_count,
            laplace_p, decay_value, last_updated)
           VALUES (?, ?, ?, ?, NULL, 0, 0, 0, 0.5, 0.5, ?)""",
        (args.experience_id, exp_hash, args.source_file, now, now),
    )
    conn.commit()

    audit_log("ExperienceRegistered", {
        "experience_id": args.experience_id,
        "source_file": args.source_file,
    })

    print("registered: %s" % args.experience_id)
    conn.close()


def cmd_ref(args: argparse.Namespace) -> None:
    """记录一次经验引用"""
    conn = get_conn()
    now = datetime.now().isoformat()
    exp_hash = trigram_hash(args.experience_id)

    # 插入引用记录
    conn.execute(
        """INSERT INTO experience_refs
           (experience_id, experience_hash, session_id, referenced_at, context)
           VALUES (?, ?, ?, ?, ?)""",
        (args.experience_id, exp_hash, args.session_id, now,
         args.context if hasattr(args, "context") and args.context else None),
    )

    # 更新或创建聚合记录
    existing = conn.execute(
        "SELECT * FROM experience_effectiveness WHERE experience_id = ?",
        (args.experience_id,),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE experience_effectiveness
               SET ref_count = ref_count + 1,
                   last_referenced = ?,
                   last_updated = ?
               WHERE experience_id = ?""",
            (now, now, args.experience_id),
        )
    else:
        source_file = _guess_source_file(args.experience_id)
        conn.execute(
            """INSERT INTO experience_effectiveness
               (experience_id, experience_hash, source_file, first_seen,
                last_referenced, ref_count, success_count, fail_count,
                laplace_p, decay_value, last_updated)
               VALUES (?, ?, ?, ?, ?, 1, 0, 0, 0.5, 0.5, ?)""",
            (args.experience_id, exp_hash, source_file, now, now, now),
        )

    conn.commit()

    audit_log("ExperienceReferenced", {
        "experience_id": args.experience_id,
        "session_id": args.session_id,
    })

    print("ref recorded: %s in session %s" % (args.experience_id, args.session_id))
    conn.close()


def _guess_source_file(experience_id: str) -> str:
    """从 experience_id 推断来源文件"""
    if experience_id.startswith("avoidance:"):
        return "MEMORY.md"
    elif experience_id.startswith("rule:"):
        parts = experience_id.split(":")
        if len(parts) >= 3:
            return "areas/projects/%s/rules.md" % parts[1]
    return "unknown"


def cmd_outcome(args: argparse.Namespace) -> None:
    """记录会话结果"""
    conn = get_conn()
    now = datetime.now().isoformat()

    # 推断 outcome_status 和 score
    status = getattr(args, "status", "unknown") or "unknown"
    score = getattr(args, "score", None)
    has_errors = int(getattr(args, "has_errors", 0) or 0)
    user_corrections = int(getattr(args, "user_corrections", 0) or 0)

    if score is None:
        # 基于信号推断分数 (移植自 memoryGraph.js inferOutcomeFromSignals)
        if status == "success":
            score = 0.85 if has_errors == 0 else 0.6
        elif status == "failed":
            score = 0.2
        else:
            score = 0.5
            if has_errors:
                score = 0.35
            if user_corrections > 0:
                score = max(0.2, score - user_corrections * 0.1)

    # Upsert session outcome
    conn.execute(
        """INSERT OR REPLACE INTO session_outcomes
           (session_id, project, started_at, ended_at,
            has_errors, user_corrections, outcome_status, outcome_score, signals)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (args.session_id, getattr(args, "project", None),
         now, now,
         has_errors, user_corrections, status, score, None),
    )

    # 更新该会话中引用的所有经验的成功/失败计数
    refs = conn.execute(
        "SELECT DISTINCT experience_id FROM experience_refs WHERE session_id = ?",
        (args.session_id,),
    ).fetchall()

    for ref in refs:
        exp_id = ref["experience_id"]
        if status == "success":
            conn.execute(
                """UPDATE experience_effectiveness
                   SET success_count = success_count + 1, last_updated = ?
                   WHERE experience_id = ?""",
                (now, exp_id),
            )
        elif status == "failed":
            conn.execute(
                """UPDATE experience_effectiveness
                   SET fail_count = fail_count + 1, last_updated = ?
                   WHERE experience_id = ?""",
                (now, exp_id),
            )

    # 重新计算所有相关经验的 Laplace 分数
    for ref in refs:
        exp_id = ref["experience_id"]
        _recalculate_effectiveness(conn, exp_id)

    conn.commit()

    audit_log("SessionOutcome", {
        "session_id": args.session_id,
        "status": status,
        "score": score,
        "has_errors": has_errors,
        "user_corrections": user_corrections,
        "experiences_updated": len(refs),
    })

    print("outcome recorded: session=%s status=%s score=%.2f (%d experiences updated)" % (
        args.session_id, status, score, len(refs)))
    conn.close()


def _recalculate_effectiveness(conn: sqlite3.Connection, experience_id: str) -> None:
    """重新计算单条经验的有效性分数"""
    row = conn.execute(
        "SELECT * FROM experience_effectiveness WHERE experience_id = ?",
        (experience_id,),
    ).fetchone()

    if not row:
        return

    result = expected_success(
        row["success_count"], row["fail_count"],
        row["last_referenced"] or row["first_seen"],
    )

    conn.execute(
        """UPDATE experience_effectiveness
           SET laplace_p = ?, decay_value = ?, last_updated = ?
           WHERE experience_id = ?""",
        (result["p"], result["value"], datetime.now().isoformat(), experience_id),
    )


def cmd_report(args: argparse.Namespace) -> None:
    """生成有效性报告"""
    conn = get_conn()

    # 总计
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM experience_effectiveness"
    ).fetchone()["cnt"]

    if total == 0:
        print("no data yet")
        conn.close()
        return

    # 按有效性排序
    rows = conn.execute(
        """SELECT * FROM experience_effectiveness
           ORDER BY decay_value DESC"""
    ).fetchall()

    # 会话统计
    session_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_outcomes"
    ).fetchone()["cnt"]

    success_sessions = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_outcomes WHERE outcome_status = 'success'"
    ).fetchone()["cnt"]

    total_refs = conn.execute(
        "SELECT COUNT(*) as cnt FROM experience_refs"
    ).fetchone()["cnt"]

    if getattr(args, "json", False):
        # JSON 输出
        data = {
            "summary": {
                "total_experiences": total,
                "total_sessions": session_count,
                "success_sessions": success_sessions,
                "total_references": total_refs,
            },
            "experiences": [],
        }
        for row in rows:
            data["experiences"].append({
                "id": row["experience_id"],
                "source": row["source_file"],
                "ref_count": row["ref_count"],
                "success": row["success_count"],
                "fail": row["fail_count"],
                "laplace_p": row["laplace_p"],
                "decay_value": row["decay_value"],
                "first_seen": row["first_seen"],
                "last_referenced": row["last_referenced"],
            })
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        # 可读文本报告
        print("=" * 60)
        print("  经验有效性报告")
        print("=" * 60)
        print("")
        print("总览:")
        print("  追踪经验数: %d" % total)
        print("  会话总数: %d (成功: %d)" % (session_count, success_sessions))
        print("  引用总数: %d" % total_refs)
        print("")

        if rows:
            # 高效经验 (decay_value > 0.6)
            high = [r for r in rows if r["decay_value"] > 0.6 and r["ref_count"] > 0]
            if high:
                print("高效经验 (value > 0.6):")
                for r in high[:10]:
                    print("  [%.2f] %s (引用%d次, 成功%d/失败%d)" % (
                        r["decay_value"], r["experience_id"][:60],
                        r["ref_count"], r["success_count"], r["fail_count"]))
                print("")

            # 低效经验 (decay_value < 0.3, 有引用)
            low = [r for r in rows if r["decay_value"] < 0.3 and r["ref_count"] > 0]
            if low:
                print("低效经验候选 (value < 0.3):")
                for r in low[:10]:
                    print("  [%.2f] %s (引用%d次, 成功%d/失败%d)" % (
                        r["decay_value"], r["experience_id"][:60],
                        r["ref_count"], r["success_count"], r["fail_count"]))
                print("")

            # 未引用经验
            unused = [r for r in rows if r["ref_count"] == 0]
            if unused:
                print("未引用经验: %d 条" % len(unused))
                for r in unused[:5]:
                    print("  - %s (注册于 %s)" % (
                        r["experience_id"][:60], r["first_seen"][:10]))
                if len(unused) > 5:
                    print("  ... 及其他 %d 条" % (len(unused) - 5))
                print("")

    conn.close()


def cmd_stale(args: argparse.Namespace) -> None:
    """列出低效经验候选"""
    threshold = float(getattr(args, "threshold", 0.3) or 0.3)
    conn = get_conn()

    # 先刷新所有经验的衰减值
    rows = conn.execute("SELECT * FROM experience_effectiveness").fetchall()
    now_iso = datetime.now().isoformat()

    for row in rows:
        result = expected_success(
            row["success_count"], row["fail_count"],
            row["last_referenced"] or row["first_seen"],
        )
        conn.execute(
            """UPDATE experience_effectiveness
               SET laplace_p = ?, decay_value = ?, last_updated = ?
               WHERE experience_id = ?""",
            (result["p"], result["value"], now_iso, row["experience_id"]),
        )
    conn.commit()

    # 查询低于阈值的
    stale = conn.execute(
        """SELECT * FROM experience_effectiveness
           WHERE decay_value < ? AND ref_count > 0
           ORDER BY decay_value ASC""",
        (threshold,),
    ).fetchall()

    if not stale:
        print("no stale experiences found (threshold=%.2f)" % threshold)
        conn.close()
        return

    print("低效经验候选 (decay_value < %.2f):" % threshold)
    print("")
    for r in stale:
        print("  [%.2f] %s" % (r["decay_value"], r["experience_id"]))
        print("         来源: %s | 引用: %d | 成功: %d | 失败: %d" % (
            r["source_file"], r["ref_count"],
            r["success_count"], r["fail_count"]))
        if r["last_referenced"]:
            print("         最后引用: %s" % r["last_referenced"][:10])
        print("")

    conn.close()


def cmd_recalculate(args: argparse.Namespace) -> None:
    """重新计算所有经验的有效性分数"""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM experience_effectiveness").fetchall()

    if not rows:
        print("no experiences to recalculate")
        conn.close()
        return

    now_iso = datetime.now().isoformat()
    updated = 0

    for row in rows:
        result = expected_success(
            row["success_count"], row["fail_count"],
            row["last_referenced"] or row["first_seen"],
        )
        conn.execute(
            """UPDATE experience_effectiveness
               SET laplace_p = ?, decay_value = ?, last_updated = ?
               WHERE experience_id = ?""",
            (result["p"], result["value"], now_iso, row["experience_id"]),
        )
        updated += 1

    conn.commit()
    print("recalculated %d experiences" % updated)
    conn.close()


def cmd_scan_and_register(args: argparse.Namespace) -> None:
    """扫描 MEMORY.md 和 rules.md，自动注册所有经验"""
    conn = get_conn()
    memory_file = os.path.join(MEMORY_DIR, "MEMORY.md")
    registered = 0

    # 扫描 MEMORY.md 中的避坑经验
    if os.path.isfile(memory_file):
        registered += _scan_file_for_experiences(
            conn, memory_file, "MEMORY.md", "avoidance")

    # 扫描所有 rules.md
    projects_dir = os.path.join(MEMORY_DIR, "areas", "projects")
    if os.path.isdir(projects_dir):
        for entry in os.listdir(projects_dir):
            rules_file = os.path.join(projects_dir, entry, "rules.md")
            if os.path.isfile(rules_file):
                source = "areas/projects/%s/rules.md" % entry
                registered += _scan_file_for_experiences(
                    conn, rules_file, source, "rule:%s" % entry)

    conn.commit()
    print("scan complete: %d new experiences registered" % registered)
    conn.close()


def _scan_file_for_experiences(
    conn: sqlite3.Connection,
    filepath: str,
    source_rel: str,
    prefix: str,
) -> int:
    """扫描文件中的列表项，注册为经验"""
    registered = 0
    now = datetime.now().isoformat()

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return 0

    current_category = ""
    for line in lines:
        stripped = line.strip()

        # 跟踪当前 section
        if stripped.startswith("## "):
            current_category = stripped[3:].strip()
            continue

        # 匹配列表项
        if not stripped.startswith("- "):
            continue

        text = stripped[2:].strip()
        if len(text) < 10:
            continue

        # 提取核心文本 (去掉日期标记和加粗)
        core = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
        core = re.sub(r"\([^)]*\)\s*$", "", core).strip()

        if len(core) < 10:
            continue

        # 生成 experience_id
        exp_hash = trigram_hash(core)
        if prefix == "avoidance":
            exp_id = "avoidance:%s" % exp_hash
        else:
            exp_id = "%s:%s:%s" % (prefix, current_category, exp_hash)

        # 检查是否已注册
        existing = conn.execute(
            "SELECT experience_id FROM experience_effectiveness WHERE experience_id = ?",
            (exp_id,),
        ).fetchone()

        if not existing:
            conn.execute(
                """INSERT INTO experience_effectiveness
                   (experience_id, experience_hash, source_file, first_seen,
                    last_referenced, ref_count, success_count, fail_count,
                    laplace_p, decay_value, last_updated)
                   VALUES (?, ?, ?, ?, NULL, 0, 0, 0, 0.5, 0.5, ?)""",
                (exp_id, exp_hash, source_rel, now, now),
            )
            registered += 1

    return registered


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="经验有效性追踪引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # ref: 记录引用
    p_ref = subparsers.add_parser("ref", help="记录经验引用")
    p_ref.add_argument("--experience-id", required=True, help="经验 ID")
    p_ref.add_argument("--session-id", required=True, help="会话 ID")
    p_ref.add_argument("--context", help="引用上下文")

    # outcome: 记录会话结果
    p_out = subparsers.add_parser("outcome", help="记录会话结果")
    p_out.add_argument("--session-id", required=True, help="会话 ID")
    p_out.add_argument("--status", default="unknown",
                       choices=["success", "failed", "unknown"],
                       help="会话状态")
    p_out.add_argument("--score", type=float, help="结果分数 [0, 1]")
    p_out.add_argument("--project", help="项目名")
    p_out.add_argument("--has-errors", type=int, default=0, help="是否有错误")
    p_out.add_argument("--user-corrections", type=int, default=0, help="用户纠正次数")

    # report: 有效性报告
    p_report = subparsers.add_parser("report", help="生成有效性报告")
    p_report.add_argument("--json", action="store_true", help="JSON 格式输出")

    # stale: 低效经验候选
    p_stale = subparsers.add_parser("stale", help="列出低效经验候选")
    p_stale.add_argument("--threshold", type=float, default=0.3,
                         help="低效阈值 (默认 0.3)")

    # register: 注册单条经验
    p_reg = subparsers.add_parser("register", help="注册经验")
    p_reg.add_argument("--experience-id", required=True, help="经验 ID")
    p_reg.add_argument("--source-file", required=True, help="来源文件")
    p_reg.add_argument("--hash", help="自定义 hash")

    # recalculate: 重新计算分数
    subparsers.add_parser("recalculate", help="重新计算所有经验分数")

    # scan: 扫描并注册所有经验
    subparsers.add_parser("scan", help="扫描 MEMORY.md 和 rules.md，注册所有经验")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "ref": cmd_ref,
        "outcome": cmd_outcome,
        "report": cmd_report,
        "stale": cmd_stale,
        "register": cmd_register,
        "recalculate": cmd_recalculate,
        "scan": cmd_scan_and_register,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
