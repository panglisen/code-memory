#!/usr/bin/env python3
"""
signal-analyzer.py - 跨会话信号分析

移植自 evolver signals.js 的频率统计、循环检测、停滞检测算法。
自动发现跨会话重复问题，生成信号报告。

用法:
    signal-analyzer.py extract --session-id "abc" --summary-file "path/to/session.md"
    signal-analyzer.py analyze [--days 14]
    signal-analyzer.py recurring
    signal-analyzer.py resolve --pattern-id 3 --resolution "已修复"
    signal-analyzer.py backfill [--days 30]
    signal-analyzer.py auto-escalate

Python 3.9 兼容。
"""

import sqlite3
import os
import sys
import json
import re
import hashlib
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Set, Tuple
from collections import Counter

MEMORY_DIR = os.path.expanduser("~/.claude/memory")
DB_PATH = os.path.join(MEMORY_DIR, ".search-index.db")
SESSIONS_DIR = os.path.join(MEMORY_DIR, "sessions")
REPORT_PATH = os.path.join(MEMORY_DIR, "evolution", "signal_report_latest.md")

# 分析参数 (移植自 signals.js)
FREQUENCY_WINDOW = 8       # 频率计数窗口大小
SUPPRESSION_THRESHOLD = 3  # 信号在窗口内出现 >= 此值则抑制
ESCALATION_THRESHOLD = 3   # 循环模式出现 >= 此值自动升级为 high


# ============================================================
# SQLite Schema
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    signal TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(session_id, signal)
);

CREATE INDEX IF NOT EXISTS idx_session_signals_sid
    ON session_signals(session_id);
CREATE INDEX IF NOT EXISTS idx_session_signals_signal
    ON session_signals(signal);

CREATE TABLE IF NOT EXISTS recurring_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_hash TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    sessions TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    priority TEXT DEFAULT 'normal',
    auto_escalated INTEGER DEFAULT 0,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_recurring_status
    ON recurring_patterns(status);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """初始化信号分析表"""
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
# 信号提取 (移植自 signals.js:26-180)
# ============================================================

# 信号关键词映射 (中文 + 英文)
SIGNAL_PATTERNS = {
    "session_had_errors": [
        r"报错", r"失败", r"bug", r"error", r"exception", r"failed",
        r"编译错误", r"构建失败", r"build.fail", r"compile.error",
    ],
    "user_correction": [
        r"不要", r"不应该", r"禁止", r"纠正", r"改回来", r"回退",
        r"错了", r"不对", r"should not", r"don't", r"revert",
    ],
    "capability_gap": [
        r"不支持", r"缺少", r"缺失", r"无法", r"not supported",
        r"cannot", r"unsupported", r"missing",
    ],
    "recurring_issue": [
        r"重复", r"又遇到", r"同样的", r"再次", r"again", r"recurring",
        r"same issue", r"还是",
    ],
    "performance_issue": [
        r"慢", r"超时", r"timeout", r"slow", r"卡", r"hang",
        r"latency", r"oom", r"out of memory",
    ],
    "architecture_decision": [
        r"架构", r"设计", r"重构", r"refactor", r"migrate",
        r"architecture", r"design",
    ],
    "dependency_issue": [
        r"依赖", r"版本", r"冲突", r"兼容", r"dependency",
        r"version", r"conflict", r"incompatible",
    ],
    "environment_issue": [
        r"环境", r"配置", r"jdk", r"node", r"python",
        r"环境变量", r"config", r"env",
    ],
}

# 更具体的复合信号 (需要多个关键词同时出现)
COMPOUND_SIGNALS = {
    "import_path_error": [r"import", r"(路径|path|package|找不到|not found)"],
    "database_schema_issue": [r"(数据库|database|table|schema)", r"(改|修改|迁移|migration)"],
    "test_failure": [r"(测试|test)", r"(失败|fail|broken|red)"],
    "deployment_issue": [r"(部署|deploy)", r"(失败|error|fail)"],
}


def extract_session_signals(summary_text: str) -> List[str]:
    """从会话摘要中提取结构化信号

    移植自 signals.js extractSignals，适配中文语境。

    Args:
        summary_text: 会话摘要文本

    Returns:
        去重后的信号列表
    """
    signals = []  # type: List[str]
    lower = summary_text.lower()

    # 基本信号检测
    for signal, patterns in SIGNAL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                signals.append(signal)
                break

    # 复合信号检测 (需要多个模式同时匹配)
    for signal, pattern_list in COMPOUND_SIGNALS.items():
        all_match = True
        for pattern in pattern_list:
            if not re.search(pattern, lower):
                all_match = False
                break
        if all_match:
            signals.append(signal)

    # 提取避坑经验信号 (如果摘要中提取了新的避坑经验)
    if re.search(r"避坑|avoidance|踩坑|陷阱", lower):
        signals.append("new_avoidance_extracted")

    # 提取规则信号
    if re.search(r"规则|rule|规范|convention", lower):
        signals.append("new_rule_extracted")

    return list(set(signals))


def extract_topic_keywords(summary_text: str) -> List[str]:
    """从摘要中提取主题关键词 (用于循环模式检测)"""
    keywords = []

    # 提取 **主题** 行
    topic_match = re.search(r"\*\*主题\*\*[：:]\s*(.+)", summary_text)
    if topic_match:
        keywords.append(topic_match.group(1).strip())

    # 提取关键知识点
    for match in re.finditer(r"^\s*[-*]\s+(.{10,80})", summary_text, re.MULTILINE):
        keywords.append(match.group(1).strip())

    return keywords[:10]  # 限制数量


# ============================================================
# 分析算法 (移植自 signals.js:180-361)
# ============================================================

def analyze_recent_history(
    sessions: List[Dict[str, Any]],
    window: int = FREQUENCY_WINDOW,
) -> Dict[str, Any]:
    """信号频率 + 循环检测 + 失败连续计数

    移植自 signals.js analyzeRecentHistory。

    Args:
        sessions: 会话列表 (按时间排序)
        window: 分析窗口大小

    Returns:
        分析结果字典
    """
    recent = sessions[-window:] if len(sessions) > window else sessions

    # 信号频率统计
    signal_freq = Counter()  # type: Counter
    for s in recent:
        signals_str = s.get("signals", "[]")
        try:
            sigs = json.loads(signals_str) if isinstance(signals_str, str) else (signals_str or [])
        except (json.JSONDecodeError, TypeError):
            sigs = []
        for sig in sigs:
            signal_freq[sig] += 1

    # 被抑制的信号 (频率 >= 阈值)
    suppressed = {k for k, v in signal_freq.items() if v >= SUPPRESSION_THRESHOLD}

    # 连续失败计数 (从末尾向后扫描)
    consecutive_failures = 0
    for s in reversed(recent):
        status = s.get("outcome_status", "unknown")
        if status == "failed":
            consecutive_failures += 1
        else:
            break

    # 最近失败率
    failure_count = sum(
        1 for s in recent if s.get("outcome_status") == "failed"
    )
    failure_ratio = failure_count / len(recent) if recent else 0.0

    return {
        "suppressed_signals": list(suppressed),
        "signal_freq": dict(signal_freq),
        "consecutive_failure_count": consecutive_failures,
        "recent_failure_count": failure_count,
        "recent_failure_ratio": round(failure_ratio, 2),
        "total_sessions_analyzed": len(recent),
    }


def detect_recurring_patterns(
    conn: sqlite3.Connection,
    sessions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """检测跨会话重复出现的问题模式

    通过信号组合和主题关键词匹配，识别重复出现的问题。

    Args:
        conn: 数据库连接
        sessions: 会话列表

    Returns:
        新发现的循环模式列表
    """
    # 收集所有会话的信号
    session_signals = {}  # type: Dict[str, List[str]]
    for s in sessions:
        sid = s.get("session_id", "")
        signals_str = s.get("signals", "[]")
        try:
            sigs = json.loads(signals_str) if isinstance(signals_str, str) else (signals_str or [])
        except (json.JSONDecodeError, TypeError):
            sigs = []
        if sigs:
            session_signals[sid] = sigs

    # 按信号组合分组
    signal_combo_sessions = {}  # type: Dict[str, List[str]]
    for sid, sigs in session_signals.items():
        # 对信号排序并组合为 key
        key = "|".join(sorted(set(sigs)))
        if key not in signal_combo_sessions:
            signal_combo_sessions[key] = []
        signal_combo_sessions[key].append(sid)

    new_patterns = []

    for combo_key, sids in signal_combo_sessions.items():
        if len(sids) < 2:
            continue

        # 生成模式 hash
        pattern_hash = hashlib.md5(combo_key.encode()).hexdigest()[:16]

        # 检查是否已存在
        existing = conn.execute(
            "SELECT * FROM recurring_patterns WHERE pattern_hash = ?",
            (pattern_hash,),
        ).fetchone()

        now = datetime.now().isoformat()

        if existing:
            # 更新已有模式
            old_sessions = json.loads(existing["sessions"])
            new_sids = [s for s in sids if s not in old_sessions]
            if new_sids:
                all_sessions = old_sessions + new_sids
                conn.execute(
                    """UPDATE recurring_patterns
                       SET occurrence_count = ?,
                           last_seen = ?,
                           sessions = ?
                       WHERE pattern_hash = ?""",
                    (len(all_sessions), now,
                     json.dumps(all_sessions, ensure_ascii=False),
                     pattern_hash),
                )
        else:
            # 创建新模式
            signals = combo_key.split("|")
            description = "信号组合: %s (出现在 %d 个会话)" % (
                ", ".join(signals), len(sids))

            conn.execute(
                """INSERT INTO recurring_patterns
                   (pattern_hash, description, first_seen, last_seen,
                    occurrence_count, sessions, status, priority)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', 'normal')""",
                (pattern_hash, description, now, now, len(sids),
                 json.dumps(sids, ensure_ascii=False)),
            )
            new_patterns.append({
                "pattern_hash": pattern_hash,
                "description": description,
                "count": len(sids),
                "signals": signals,
            })

    return new_patterns


# ============================================================
# CLI 命令
# ============================================================

def cmd_extract(args: argparse.Namespace) -> None:
    """从会话摘要中提取信号"""
    summary_file = args.summary_file
    session_id = args.session_id

    if not os.path.isfile(summary_file):
        print("summary file not found: %s" % summary_file, file=sys.stderr)
        return

    with open(summary_file, "r", encoding="utf-8") as f:
        text = f.read()

    signals = extract_session_signals(text)

    if not signals:
        print("no signals detected in session %s" % session_id)
        return

    conn = get_conn()
    now = datetime.now().isoformat()

    inserted = 0
    for sig in signals:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO session_signals
                   (session_id, signal, detected_at)
                   VALUES (?, ?, ?)""",
                (session_id, sig, now),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass

    # 更新 session_outcomes 的 signals 字段 (如果存在)
    conn.execute(
        """UPDATE session_outcomes
           SET signals = ?
           WHERE session_id = ?""",
        (json.dumps(signals, ensure_ascii=False), session_id),
    )

    conn.commit()
    print("extracted %d signals from session %s: %s" % (
        inserted, session_id, ", ".join(signals)))
    conn.close()


def cmd_analyze(args: argparse.Namespace) -> None:
    """分析近 N 天的会话信号"""
    days = int(getattr(args, "days", 14) or 14)
    conn = get_conn()

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # 获取有信号数据的会话
    sessions = conn.execute(
        """SELECT so.session_id, so.outcome_status, so.outcome_score,
                  so.signals, so.has_errors, so.user_corrections
           FROM session_outcomes so
           WHERE so.started_at >= ?
           ORDER BY so.started_at ASC""",
        (cutoff,),
    ).fetchall()

    sessions_list = [dict(s) for s in sessions]

    # 补充从 session_signals 表获取的信号
    for s in sessions_list:
        if not s.get("signals") or s["signals"] == "null":
            sigs = conn.execute(
                "SELECT signal FROM session_signals WHERE session_id = ?",
                (s["session_id"],),
            ).fetchall()
            if sigs:
                s["signals"] = json.dumps([r["signal"] for r in sigs])

    if not sessions_list:
        print("no session data found in the last %d days" % days)
        conn.close()
        return

    # 分析
    history = analyze_recent_history(sessions_list)
    new_patterns = detect_recurring_patterns(conn, sessions_list)
    conn.commit()

    # 生成报告
    report_lines = [
        "# 信号分析报告",
        "",
        "生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "分析范围: 最近 %d 天" % days,
        "",
        "## 概览",
        "",
        "- 分析会话数: %d" % history["total_sessions_analyzed"],
        "- 连续失败数: %d" % history["consecutive_failure_count"],
        "- 近期失败率: %.0f%%" % (history["recent_failure_ratio"] * 100),
        "",
    ]

    if history["signal_freq"]:
        report_lines.append("## 信号频率")
        report_lines.append("")
        sorted_freq = sorted(history["signal_freq"].items(),
                             key=lambda x: x[1], reverse=True)
        for sig, count in sorted_freq:
            suppressed = " [SUPPRESSED]" if sig in history["suppressed_signals"] else ""
            report_lines.append("- %s: %d 次%s" % (sig, count, suppressed))
        report_lines.append("")

    if history["suppressed_signals"]:
        report_lines.append("## 抑制的信号 (频率过高)")
        report_lines.append("")
        for sig in history["suppressed_signals"]:
            report_lines.append("- %s" % sig)
        report_lines.append("")

    if new_patterns:
        report_lines.append("## 新发现的循环模式")
        report_lines.append("")
        for p in new_patterns:
            report_lines.append("- **%s** (出现 %d 次)" % (
                ", ".join(p["signals"]), p["count"]))
        report_lines.append("")

    # 获取所有活跃的循环模式
    active_patterns = conn.execute(
        """SELECT * FROM recurring_patterns
           WHERE status = 'active'
           ORDER BY occurrence_count DESC"""
    ).fetchall()

    if active_patterns:
        report_lines.append("## 活跃循环模式")
        report_lines.append("")
        for p in active_patterns:
            priority_tag = " [%s]" % p["priority"].upper() if p["priority"] != "normal" else ""
            report_lines.append("- [#%d]%s %s (出现 %d 次, 最后: %s)" % (
                p["id"], priority_tag, p["description"],
                p["occurrence_count"], p["last_seen"][:10]))
        report_lines.append("")

    # 警告
    if history["consecutive_failure_count"] >= 3:
        report_lines.append("## !!warning!! 连续失败警告")
        report_lines.append("")
        report_lines.append("最近 %d 个会话连续失败，建议检查环境或最近的变更。" % (
            history["consecutive_failure_count"]))
        report_lines.append("")

    if history["recent_failure_ratio"] >= 0.75:
        report_lines.append("## !!warning!! 高失败率警告")
        report_lines.append("")
        report_lines.append("近期失败率 %.0f%%，建议暂停开发，先排查根因。" % (
            history["recent_failure_ratio"] * 100))
        report_lines.append("")

    report_text = "\n".join(report_lines)

    # 保存报告
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(report_text)
    print("\n报告已保存: %s" % REPORT_PATH)
    conn.close()


def cmd_recurring(args: argparse.Namespace) -> None:
    """显示循环问题"""
    conn = get_conn()

    patterns = conn.execute(
        """SELECT * FROM recurring_patterns
           WHERE status = 'active'
           ORDER BY occurrence_count DESC"""
    ).fetchall()

    if not patterns:
        print("no active recurring patterns")
        conn.close()
        return

    print("活跃循环模式 (%d 个):" % len(patterns))
    print("")
    for p in patterns:
        priority_tag = " [%s]" % p["priority"].upper() if p["priority"] != "normal" else ""
        escalated = " (auto-escalated)" if p["auto_escalated"] else ""
        print("  #%d%s%s" % (p["id"], priority_tag, escalated))
        print("    %s" % p["description"])
        print("    出现 %d 次 | 首次: %s | 最近: %s" % (
            p["occurrence_count"], p["first_seen"][:10], p["last_seen"][:10]))
        sessions = json.loads(p["sessions"])
        print("    会话: %s" % ", ".join(sessions[:5]))
        if len(sessions) > 5:
            print("    ... 及其他 %d 个" % (len(sessions) - 5))
        print("")

    conn.close()


def cmd_resolve(args: argparse.Namespace) -> None:
    """标记循环模式为已解决"""
    conn = get_conn()

    pattern_id = args.pattern_id
    resolution = getattr(args, "resolution", None) or "resolved"

    result = conn.execute(
        """UPDATE recurring_patterns
           SET status = 'addressed', resolution = ?
           WHERE id = ?""",
        (resolution, pattern_id),
    )

    if result.rowcount == 0:
        print("pattern #%s not found" % pattern_id)
    else:
        print("pattern #%s marked as addressed: %s" % (pattern_id, resolution))

    conn.commit()
    conn.close()


def cmd_auto_escalate(args: argparse.Namespace) -> None:
    """自动升级 count>=3 的模式为 high 优先级"""
    conn = get_conn()

    patterns = conn.execute(
        """SELECT * FROM recurring_patterns
           WHERE status = 'active'
             AND priority != 'high'
             AND priority != 'critical'
             AND occurrence_count >= ?""",
        (ESCALATION_THRESHOLD,),
    ).fetchall()

    escalated = 0
    for p in patterns:
        conn.execute(
            """UPDATE recurring_patterns
               SET priority = 'high', auto_escalated = 1
               WHERE id = ?""",
            (p["id"],),
        )
        escalated += 1
        print("escalated pattern #%d to HIGH: %s (count=%d)" % (
            p["id"], p["description"][:60], p["occurrence_count"]))

    # 升级 count>=5 为 critical
    critical_patterns = conn.execute(
        """SELECT * FROM recurring_patterns
           WHERE status = 'active'
             AND priority != 'critical'
             AND occurrence_count >= 5""",
    ).fetchall()

    for p in critical_patterns:
        conn.execute(
            """UPDATE recurring_patterns
               SET priority = 'critical', auto_escalated = 1
               WHERE id = ?""",
            (p["id"],),
        )
        print("escalated pattern #%d to CRITICAL: %s (count=%d)" % (
            p["id"], p["description"][:60], p["occurrence_count"]))

    conn.commit()

    if escalated == 0 and not critical_patterns:
        print("no patterns to escalate")

    conn.close()


def cmd_backfill(args: argparse.Namespace) -> None:
    """从现有 sessions/ 回填信号数据"""
    days = int(getattr(args, "days", 30) or 30)
    conn = get_conn()

    if not os.path.isdir(SESSIONS_DIR):
        print("sessions directory not found: %s" % SESSIONS_DIR)
        conn.close()
        return

    cutoff = datetime.now() - timedelta(days=days)
    total_processed = 0
    total_signals = 0

    for filename in sorted(os.listdir(SESSIONS_DIR)):
        if not filename.endswith(".md"):
            continue

        filepath = os.path.join(SESSIONS_DIR, filename)
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            if mtime < cutoff:
                continue
        except OSError:
            continue

        session_id = filename[:-3]  # 去掉 .md

        # 检查是否已处理
        existing = conn.execute(
            "SELECT COUNT(*) as cnt FROM session_signals WHERE session_id = ?",
            (session_id,),
        ).fetchone()["cnt"]

        if existing > 0:
            continue

        # 读取并提取信号
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue

        signals = extract_session_signals(text)
        now = datetime.now().isoformat()

        for sig in signals:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO session_signals
                       (session_id, signal, detected_at)
                       VALUES (?, ?, ?)""",
                    (session_id, sig, now),
                )
                total_signals += 1
            except sqlite3.IntegrityError:
                pass

        # 如果 session_outcomes 中没有该会话，创建一条
        existing_outcome = conn.execute(
            "SELECT session_id FROM session_outcomes WHERE session_id = ?",
            (session_id,),
        ).fetchone()

        if not existing_outcome:
            # 从摘要中推断项目
            project_match = re.search(r"\*\*项目\*\*:\s*(\S+)", text)
            project = project_match.group(1) if project_match else None

            # 推断状态
            has_errors = 1 if any(
                re.search(p, text.lower())
                for p in [r"报错", r"失败", r"error", r"bug"]
            ) else 0

            conn.execute(
                """INSERT OR IGNORE INTO session_outcomes
                   (session_id, project, started_at, ended_at,
                    has_errors, user_corrections, outcome_status,
                    outcome_score, signals)
                   VALUES (?, ?, ?, ?, ?, 0, 'unknown', 0.5, ?)""",
                (session_id, project, mtime.isoformat(), mtime.isoformat(),
                 has_errors, json.dumps(signals, ensure_ascii=False)),
            )

        total_processed += 1

    conn.commit()
    print("backfill complete: %d sessions processed, %d signals extracted" % (
        total_processed, total_signals))
    conn.close()


def cmd_stats(args: argparse.Namespace) -> None:
    """显示信号分析统计"""
    conn = get_conn()

    signal_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM session_signals"
    ).fetchone()["cnt"]

    session_count = conn.execute(
        "SELECT COUNT(DISTINCT session_id) as cnt FROM session_signals"
    ).fetchone()["cnt"]

    pattern_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM recurring_patterns WHERE status = 'active'"
    ).fetchone()["cnt"]

    high_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM recurring_patterns WHERE priority IN ('high', 'critical')"
    ).fetchone()["cnt"]

    print("信号分析统计:")
    print("  信号记录数: %d" % signal_count)
    print("  有信号的会话数: %d" % session_count)
    print("  活跃循环模式: %d" % pattern_count)
    print("  高优先级模式: %d" % high_count)

    # 信号 Top 10
    top_signals = conn.execute(
        """SELECT signal, COUNT(*) as cnt
           FROM session_signals
           GROUP BY signal
           ORDER BY cnt DESC
           LIMIT 10"""
    ).fetchall()

    if top_signals:
        print("\n  信号 Top 10:")
        for row in top_signals:
            print("    %s: %d" % (row["signal"], row["cnt"]))

    conn.close()


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="跨会话信号分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # extract
    p_extract = subparsers.add_parser("extract", help="从会话摘要提取信号")
    p_extract.add_argument("--session-id", required=True, help="会话 ID")
    p_extract.add_argument("--summary-file", required=True, help="摘要文件路径")

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="分析近 N 天的信号")
    p_analyze.add_argument("--days", type=int, default=14, help="分析天数")

    # recurring
    subparsers.add_parser("recurring", help="显示循环问题")

    # resolve
    p_resolve = subparsers.add_parser("resolve", help="标记循环模式为已解决")
    p_resolve.add_argument("--pattern-id", required=True, type=int, help="模式 ID")
    p_resolve.add_argument("--resolution", help="解决方案描述")

    # auto-escalate
    subparsers.add_parser("auto-escalate", help="自动升级高频模式")

    # backfill
    p_backfill = subparsers.add_parser("backfill", help="从现有会话回填信号")
    p_backfill.add_argument("--days", type=int, default=30, help="回填天数")

    # stats
    subparsers.add_parser("stats", help="显示统计信息")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "extract": cmd_extract,
        "analyze": cmd_analyze,
        "recurring": cmd_recurring,
        "resolve": cmd_resolve,
        "auto-escalate": cmd_auto_escalate,
        "backfill": cmd_backfill,
        "stats": cmd_stats,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
