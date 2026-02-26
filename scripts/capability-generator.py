#!/usr/bin/env python3
"""
capability-generator.py - 能力自动生成

从循环模式中自动生成 Claude Code Skill 或 Command。
移植 evolver skillDistiller.js 的门控逻辑，适配 Claude Code 环境。

用法:
    capability-generator.py                    # 自动生成 (带门控)
    capability-generator.py --dry-run          # 预览模式
    capability-generator.py --force            # 跳过门控强制生成
    capability-generator.py --pattern-id 3     # 针对特定模式生成

Python 3.9 兼容。
"""

import sqlite3
import os
import sys
import json
import hashlib
import argparse
import subprocess
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

MEMORY_DIR = os.path.expanduser("~/.claude/memory")
DB_PATH = os.path.join(MEMORY_DIR, ".search-index.db")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
COMMANDS_DIR = os.path.expanduser("~/.claude/commands")
RULES_DIR = os.path.expanduser("~/.claude/rules")
STATE_PATH = os.path.join(MEMORY_DIR, "evolution", "generator_state.json")
CAPABILITIES_LOG = os.path.join(MEMORY_DIR, "evolution", "capabilities.jsonl")
CAPABILITIES_INDEX = os.path.join(RULES_DIR, "auto-capabilities.md")

# 门控参数 (移植自 skillDistiller.js:383)
MIN_GENERATION_INTERVAL_HOURS = 24  # 最短生成间隔
MIN_QUALIFYING_PATTERNS = 2         # 至少 N 个 count>=2 的未处理循环模式
MIN_RECENT_SUCCESS_RATE = 0.6       # 最近 5 个会话的成功率下限
CAPABILITY_MODEL = os.environ.get("CAPABILITY_MODEL", "haiku")
CAPABILITY_GENERATION_ENABLED = os.environ.get("CAPABILITY_GENERATION", "true")


# ============================================================
# 状态管理
# ============================================================

def read_state() -> Dict[str, Any]:
    """读取生成器状态"""
    if os.path.isfile(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "last_generation_at": None,
        "generation_count": 0,
        "generated": [],
    }


def write_state(state: Dict[str, Any]) -> None:
    """写入生成器状态"""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def capabilities_log(event_type: str, data: Dict[str, Any]) -> None:
    """写入能力生成审计日志"""
    os.makedirs(os.path.dirname(CAPABILITIES_LOG), exist_ok=True)
    entry = {
        "type": event_type,
        "ts": datetime.now().isoformat(),
        **data,
    }
    try:
        with open(CAPABILITIES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ============================================================
# 门控检查 (移植自 skillDistiller.js:383)
# ============================================================

def get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


def should_generate(force: bool = False) -> Tuple[bool, str]:
    """门控检查: 是否应该生成新能力

    移植自 skillDistiller.js shouldDistill 逻辑:
    1. 功能未禁用
    2. 距上次生成 >= 24h
    3. 至少 2 个 count>=2 的未处理循环模式
    4. 最近 5 个会话 >= 60% 成功率

    Returns:
        (should_proceed, reason)
    """
    if force:
        return True, "forced generation"

    # 1. 功能开关
    if CAPABILITY_GENERATION_ENABLED.lower() == "false":
        return False, "CAPABILITY_GENERATION=false"

    # 2. 冷却期
    state = read_state()
    if state["last_generation_at"]:
        try:
            last = datetime.fromisoformat(state["last_generation_at"])
            elapsed = (datetime.now() - last).total_seconds() / 3600
            if elapsed < MIN_GENERATION_INTERVAL_HOURS:
                return False, "cooldown: %.1fh since last generation (need %dh)" % (
                    elapsed, MIN_GENERATION_INTERVAL_HOURS)
        except (ValueError, TypeError):
            pass

    # 3. 足够的循环模式
    conn = get_conn()
    qualifying_patterns = conn.execute(
        """SELECT COUNT(*) as cnt FROM recurring_patterns
           WHERE status = 'active' AND occurrence_count >= 2"""
    ).fetchone()["cnt"]

    if qualifying_patterns < MIN_QUALIFYING_PATTERNS:
        conn.close()
        return False, "insufficient patterns: %d active (need >= %d with count>=2)" % (
            qualifying_patterns, MIN_QUALIFYING_PATTERNS)

    # 4. 稳定期检查 (最近 5 个会话成功率)
    recent = conn.execute(
        """SELECT outcome_status FROM session_outcomes
           ORDER BY started_at DESC LIMIT 5"""
    ).fetchall()

    conn.close()

    if len(recent) < 3:
        return False, "insufficient session data: %d (need >= 3)" % len(recent)

    success_count = sum(1 for r in recent if r["outcome_status"] == "success")
    success_rate = success_count / len(recent)

    if success_rate < MIN_RECENT_SUCCESS_RATE:
        return False, "unstable period: %.0f%% success rate (need >= %.0f%%)" % (
            success_rate * 100, MIN_RECENT_SUCCESS_RATE * 100)

    return True, "all gates passed (%d patterns, %.0f%% success)" % (
        qualifying_patterns, success_rate * 100)


# ============================================================
# 候选收集
# ============================================================

def collect_candidates() -> List[Dict[str, Any]]:
    """扫描 recurring_patterns + session 主题，识别候选"""
    conn = get_conn()

    # 获取活跃的 count>=2 的循环模式
    patterns = conn.execute(
        """SELECT * FROM recurring_patterns
           WHERE status = 'active' AND occurrence_count >= 2
           ORDER BY occurrence_count DESC"""
    ).fetchall()

    candidates = []

    # 已有的 commands 和 skills
    existing_commands = set()
    if os.path.isdir(COMMANDS_DIR):
        for f in os.listdir(COMMANDS_DIR):
            if f.endswith(".md"):
                existing_commands.add(f[:-3])

    existing_skills = set()
    if os.path.isdir(SKILLS_DIR):
        for d in os.listdir(SKILLS_DIR):
            if os.path.isdir(os.path.join(SKILLS_DIR, d)):
                existing_skills.add(d)

    for p in patterns:
        pattern_dict = dict(p)
        sessions = json.loads(pattern_dict["sessions"])

        # 检查是否已有覆盖
        pattern_name = _pattern_to_name(pattern_dict["description"])
        auto_name = "auto-%s" % pattern_name

        if auto_name in existing_commands or auto_name in existing_skills:
            continue

        # 获取相关会话的详细信息
        session_details = []
        for sid in sessions[:5]:  # 最多取 5 个
            detail = conn.execute(
                "SELECT * FROM session_outcomes WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if detail:
                session_details.append(dict(detail))

        candidates.append({
            "pattern_id": pattern_dict["id"],
            "pattern_hash": pattern_dict["pattern_hash"],
            "description": pattern_dict["description"],
            "occurrence_count": pattern_dict["occurrence_count"],
            "priority": pattern_dict["priority"],
            "sessions": sessions,
            "session_details": session_details,
            "suggested_name": auto_name,
        })

    conn.close()
    return candidates


def _pattern_to_name(description: str) -> str:
    """从模式描述生成短名称"""
    # 提取信号名称
    signals_match = re.search(r"信号组合:\s*(.+?)(?:\s*\(|$)", description)
    if signals_match:
        signals = signals_match.group(1).strip()
        # 取第一个信号名作为基础
        first_signal = signals.split(",")[0].strip()
        return re.sub(r"[^a-zA-Z0-9_-]", "-", first_signal).strip("-")[:30]

    # 降级: 用描述的 hash
    return hashlib.md5(description.encode()).hexdigest()[:8]


# ============================================================
# Skill/Command 生成
# ============================================================

def synthesize_skill(candidate: Dict[str, Any], dry_run: bool = False) -> Optional[str]:
    """用 LLM 生成 Skill 模板

    优先生成 Skill 而非 Command，确保 Claude 能自动发现和调用。

    Args:
        candidate: 候选模式
        dry_run: 预览模式

    Returns:
        生成的 Skill 文件路径，或 None
    """
    if dry_run:
        print("  [dry-run] would generate skill for: %s" % candidate["description"])
        return None

    prompt = """基于以下循环模式，生成一个 Claude Code Skill 文件。

循环模式: %s
出现 %d 次
涉及会话: %s
优先级: %s

要求:
1. 输出完整的 SKILL.md 文件内容
2. 以 YAML frontmatter 开头，格式如下:
   ---
   name: %s
   description: 描述这个 Skill 的触发条件和功能。当用户遇到 XX 时自动触发。
   ---
3. YAML 之后是 Markdown 格式的 Skill 执行指南
4. 包含: 触发条件、执行步骤、输出格式
5. 如果需要调用 Python 脚本，用 Bash 工具调用 python3
6. 使用中文
7. 直接输出文件内容，不要任何额外解释

限制:
- 不要包含 ```markdown 或 ``` 代码块标记
- 只输出 SKILL.md 的内容""" % (
        candidate["description"],
        candidate["occurrence_count"],
        ", ".join(candidate["sessions"][:5]),
        candidate["priority"],
        candidate["suggested_name"],
    )

    # 调用 claude CLI 生成
    try:
        result = subprocess.run(
            ["claude", "-p",
             "--model", CAPABILITY_MODEL,
             "--no-session-persistence",
             "--system-prompt", "你是一个 Claude Code Skill 生成器。只输出 SKILL.md 文件内容。"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0 or not result.stdout.strip():
            print("  LLM generation failed: %s" % (result.stderr[:200] if result.stderr else "empty output"))
            return None

        skill_content = result.stdout.strip()

        # 验证基本格式
        if not skill_content.startswith("---"):
            print("  generated content missing YAML frontmatter, skipping")
            return None

        # 写入文件
        skill_dir = os.path.join(SKILLS_DIR, candidate["suggested_name"])
        os.makedirs(skill_dir, exist_ok=True)
        skill_path = os.path.join(skill_dir, "SKILL.md")

        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(skill_content)

        return skill_path

    except subprocess.TimeoutExpired:
        print("  LLM generation timed out (120s)")
        return None
    except FileNotFoundError:
        print("  claude CLI not found")
        return None
    except Exception as e:
        print("  LLM generation error: %s" % e)
        return None


def update_capability_index(
    name: str,
    cap_type: str,
    trigger: str,
    date: str,
    output_path: str,
) -> None:
    """更新 ~/.claude/rules/auto-capabilities.md 能力索引表"""
    os.makedirs(os.path.dirname(CAPABILITIES_INDEX), exist_ok=True)

    header = """# 自动生成的能力

> 由 capability-generator.py 自动生成。每次会话自动加载。
> 当此表有内容时，Claude 可根据触发条件自动调用对应的 Skill。

| 能力名 | 类型 | 触发条件 | 生成日期 | 路径 |
|--------|------|---------|---------|------|
"""

    existing_content = ""
    if os.path.isfile(CAPABILITIES_INDEX):
        with open(CAPABILITIES_INDEX, "r", encoding="utf-8") as f:
            existing_content = f.read()

    if not existing_content.strip():
        existing_content = header

    # 检查是否已有该能力
    if name in existing_content:
        return

    # 追加新行
    new_row = "| %s | %s | %s | %s | `%s` |\n" % (
        name, cap_type, trigger[:40], date, output_path)

    # 在表格末尾追加
    if existing_content.rstrip().endswith("|"):
        existing_content = existing_content.rstrip() + "\n" + new_row
    else:
        existing_content = existing_content.rstrip() + "\n" + new_row

    with open(CAPABILITIES_INDEX, "w", encoding="utf-8") as f:
        f.write(existing_content)


# ============================================================
# 主流程
# ============================================================

def run_generation(
    dry_run: bool = False,
    force: bool = False,
    pattern_id: Optional[int] = None,
) -> None:
    """执行能力生成主流程"""

    print("=" * 50)
    print("  能力自动生成")
    print("=" * 50)
    print("")

    # 1. 门控检查
    if pattern_id:
        should, reason = True, "specific pattern requested"
    else:
        should, reason = should_generate(force=force)

    print("门控检查: %s (%s)" % ("PASS" if should else "BLOCKED", reason))
    print("")

    if not should:
        return

    # 2. 收集候选
    candidates = collect_candidates()

    if pattern_id:
        candidates = [c for c in candidates if c["pattern_id"] == pattern_id]

    if not candidates:
        print("no candidates found")
        return

    print("候选模式 (%d 个):" % len(candidates))
    for c in candidates:
        priority_tag = " [%s]" % c["priority"].upper() if c["priority"] != "normal" else ""
        print("  #%d%s %s (出现 %d 次) -> %s" % (
            c["pattern_id"], priority_tag, c["description"][:50],
            c["occurrence_count"], c["suggested_name"]))
    print("")

    if dry_run:
        print("[dry-run] 预览模式，不实际生成文件")
        print("")
        for c in candidates:
            synthesize_skill(c, dry_run=True)
        return

    # 3. 生成 (每次最多生成 2 个，避免过度生成)
    generated = []
    for c in candidates[:2]:
        print("生成 Skill: %s ..." % c["suggested_name"])
        path = synthesize_skill(c)

        if path:
            print("  -> 已生成: %s" % path)
            generated.append({
                "name": c["suggested_name"],
                "pattern_id": c["pattern_id"],
                "path": path,
            })

            # 更新能力索引
            update_capability_index(
                name=c["suggested_name"],
                cap_type="Skill",
                trigger=c["description"][:40],
                date=datetime.now().strftime("%Y-%m-%d"),
                output_path=path,
            )

            # 审计日志
            capabilities_log("CapabilityGenerated", {
                "id": "cap_%03d" % c["pattern_id"],
                "pattern_id": c["pattern_id"],
                "output_path": path,
                "name": c["suggested_name"],
                "status": "generated",
            })

            # 更新循环模式状态
            conn = get_conn()
            conn.execute(
                """UPDATE recurring_patterns
                   SET status = 'addressed',
                       resolution = ?
                   WHERE id = ?""",
                ("auto-generated skill: %s" % c["suggested_name"], c["pattern_id"]),
            )
            conn.commit()
            conn.close()
        else:
            print("  -> 生成失败")
            capabilities_log("CapabilityFailed", {
                "id": "cap_%03d" % c["pattern_id"],
                "pattern_id": c["pattern_id"],
                "name": c["suggested_name"],
                "status": "failed",
            })

    # 4. 更新状态
    state = read_state()
    state["last_generation_at"] = datetime.now().isoformat()
    state["generation_count"] += len(generated)
    state["generated"].extend([{
        "name": g["name"],
        "path": g["path"],
        "generated_at": datetime.now().isoformat(),
    } for g in generated])
    write_state(state)

    print("")
    print("完成: 生成 %d 个能力" % len(generated))


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="能力自动生成 (从循环模式生成 Skill/Command)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式，不实际生成文件")
    parser.add_argument("--force", action="store_true",
                        help="跳过门控检查强制生成")
    parser.add_argument("--pattern-id", type=int,
                        help="针对特定模式 ID 生成")
    parser.add_argument("--status", action="store_true",
                        help="查看生成器状态")

    args = parser.parse_args()

    if args.status:
        state = read_state()
        print("生成器状态:")
        print("  上次生成: %s" % (state["last_generation_at"] or "从未"))
        print("  累计生成: %d" % state["generation_count"])
        if state["generated"]:
            print("  已生成:")
            for g in state["generated"][-5:]:
                print("    - %s (%s)" % (g["name"], g["generated_at"][:10]))
        return

    try:
        run_generation(
            dry_run=args.dry_run,
            force=args.force,
            pattern_id=args.pattern_id,
        )
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
