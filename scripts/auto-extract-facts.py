#!/usr/bin/env python3
"""
auto-extract-facts.py - 从 session 摘要自动提取原子事实写入 facts.json

核心流程:
  1. 扫描本周 sessions, 按项目分组
  2. 过滤 >= 2 个 session 的项目
  3. 对每个项目: 加载已有 facts → 组装 prompt → 调用 haiku → 解析 → 写入

配置:
  项目映射和忽略列表从 ~/.claude/memory/config.json 读取:
  {
    "project_name_map": {"repo-dir-name": "memory-project-name"},
    "ignored_projects": ["mac", "git-repo"]
  }

用法:
    python3 auto-extract-facts.py                     # 默认提取本周
    python3 auto-extract-facts.py --dry-run            # 预览模式
    python3 auto-extract-facts.py --project my-api --dry-run
    python3 auto-extract-facts.py --days 14            # 扫描最近 14 天
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path.home() / ".claude" / "memory"
SESSIONS_DIR = MEMORY_DIR / "sessions"
AREAS_DIR = MEMORY_DIR / "areas" / "projects"
CONFIG_PATH = MEMORY_DIR / "config.json"

# 从配置文件加载项目映射（支持用户自定义）
def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}

config = load_config()

# session 目录名 → areas/projects 目录名
PROJECT_NAME_MAP = config.get("project_name_map", {})

# 忽略的"项目"名 (非代码项目)
IGNORED_PROJECTS = set(config.get("ignored_projects", ["mac", "git-repo"]))

VALID_CATEGORIES = [
    "api",
    "architecture",
    "implementation",
    "configuration",
    "bug-pattern",
    "business-logic",
]

LLM_TIMEOUT = 180  # seconds (claude CLI startup + model response)


def resolve_project_name(raw_name: str) -> str | None:
    """将 session 中的项目名映射到 areas/projects 目录名"""
    raw_name = raw_name.strip()

    if raw_name in IGNORED_PROJECTS:
        return None

    # 先查映射表
    if raw_name in PROJECT_NAME_MAP:
        return PROJECT_NAME_MAP[raw_name]

    # 检查 areas/projects/{name}/ 是否存在
    if (AREAS_DIR / raw_name).is_dir():
        return raw_name

    return None


def scan_sessions(days: int = 7) -> dict[str, list[Path]]:
    """
    扫描最近 N 天的 session 摘要, 按项目分组.
    只包含结构化摘要 (含 '关键知识点' 或 '**主题**').

    Returns: {project_name: [session_path, ...]}
    """
    cutoff = datetime.now() - timedelta(days=days)
    grouped: dict[str, list[Path]] = {}

    if not SESSIONS_DIR.is_dir():
        return grouped

    for session_file in sorted(SESSIONS_DIR.glob("*.md")):
        # 按文件修改时间过滤
        mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
        if mtime < cutoff:
            continue

        content = session_file.read_text(encoding="utf-8", errors="ignore")

        # 只处理结构化摘要
        if "关键知识点" not in content and "**主题**" not in content:
            continue

        # 提取项目名
        match = re.search(r"^\- \*\*项目\*\*:\s*(.+)$", content, re.MULTILINE)
        if not match:
            continue

        raw_project = match.group(1).strip()
        project = resolve_project_name(raw_project)
        if not project:
            continue

        grouped.setdefault(project, []).append(session_file)

    return grouped


def extract_knowledge_points(session_path: Path) -> str:
    """从 session 摘要中提取关键知识点部分"""
    content = session_path.read_text(encoding="utf-8", errors="ignore")
    lines = content.split("\n")
    result_lines = []
    capturing = False

    for line in lines:
        # 检测知识点区块开始 (必须是标题行或加粗标签，忽略散文中的提及)
        if "关键知识点" in line and (
            line.lstrip().startswith("#")
            or line.lstrip().startswith("**")
            or line.lstrip().startswith("- **")
        ):
            capturing = True
            result_lines = [line]  # 重置，取最后匹配的区块
            continue

        if capturing:
            # 遇到下一个同级或更高级标题时停止
            if re.match(r"^#{1,3}\s", line) and "关键知识点" not in line:
                break
            # 遇到其他主要区块标记也停止
            if re.match(r"^(##\s|###\s|\*\*涉及文件|📁|⚡|✅|### 决策)", line):
                break
            result_lines.append(line)

    # 如果没有捕获到知识点区块，尝试提取主题和简要内容
    if not result_lines:
        for line in lines:
            if "**主题**" in line or line.startswith("## 主题"):
                result_lines.append(line)
            elif line.startswith("- **") and len(result_lines) < 8:
                result_lines.append(line)

    return "\n".join(result_lines).strip()


def load_existing_facts(project: str) -> dict:
    """读取项目现有的 facts.json"""
    facts_path = AREAS_DIR / project / "facts.json"
    if facts_path.is_file():
        try:
            return json.loads(facts_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"facts": []}
    return {"facts": []}


def get_next_fact_id(existing_facts: list) -> int:
    """从现有 facts 中获取最大 ID 编号"""
    max_id = 0
    for fact in existing_facts:
        fid = fact.get("id", "")
        match = re.match(r"fact-(\d+)", fid)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return max_id + 1


def build_prompt(project: str, knowledge_texts: list[str], existing_facts: list) -> tuple[str, str]:
    """组装 LLM prompt, 返回 (system_prompt, user_prompt)"""
    existing_fact_texts = []
    for f in existing_facts:
        if f.get("status") == "active":
            existing_fact_texts.append(f"- [{f.get('category', '')}] {f.get('fact', '')}")

    existing_section = ""
    if existing_fact_texts:
        existing_section = (
            "\n\n已有事实 (不要重复):\n" + "\n".join(existing_fact_texts)
        )

    system_prompt = (
        "你是一个知识提取助手。从给定的会话知识点中提取 1-3 条最有价值的原子事实。"
        "\n\n要求:"
        "\n1. 输出纯 JSON 数组，每个元素包含 fact (string, 50字以内) 和 category (string)"
        f"\n2. category 必须是以下之一: {', '.join(VALID_CATEGORIES)}"
        "\n3. 只提取对未来开发有参考价值的事实（架构决策、Bug 模式、关键配置等）"
        "\n4. 不要重复已有事实"
        "\n5. 不要输出任何 JSON 以外的内容"
    )

    knowledge_combined = "\n\n---\n\n".join(knowledge_texts)

    user_prompt = (
        f"项目: {project}\n"
        f"本周会话知识点 ({len(knowledge_texts)} 个会话):\n\n"
        f"{knowledge_combined}"
        f"{existing_section}"
    )

    return system_prompt, user_prompt


def call_haiku(system_prompt: str, user_prompt: str) -> str | None:
    """调用 claude CLI (haiku model), 返回原始输出"""
    # 将 system prompt 融入 user prompt，因为 claude CLI 会加载全局指令
    combined_prompt = (
        f"[指令] {system_prompt}\n\n"
        f"[输入]\n{user_prompt}\n\n"
        f"[输出] 只输出纯 JSON 数组，不要输出任何其他内容:"
    )
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model", "haiku",
                "--no-session-persistence",
            ],
            input=combined_prompt,
            capture_output=True,
            text=True,
            timeout=LLM_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"  [错误] claude 调用失败: {result.stderr.strip()}", file=sys.stderr)
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        print("  [错误] claude 命令未找到", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"  [超时] claude 调用超过 {LLM_TIMEOUT}s", file=sys.stderr)
        return None


def parse_response(raw: str) -> list[dict] | None:
    """
    解析 LLM 响应为 fact 列表。
    多层 fallback: json.loads → 正则提取 [...] → None
    """
    if not raw:
        return None

    # 尝试直接解析
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # 正则提取 JSON 数组
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    print(f"  [警告] 无法解析 LLM 响应: {raw[:200]}", file=sys.stderr)
    return None


def validate_facts(facts: list[dict]) -> list[dict]:
    """验证和清洗 facts"""
    valid = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        fact_text = f.get("fact", "").strip()
        category = f.get("category", "").strip()

        if not fact_text:
            continue
        if category not in VALID_CATEGORIES:
            category = "implementation"  # fallback

        valid.append({
            "fact": fact_text[:120],  # 截断过长
            "category": category,
        })

    return valid[:3]  # 最多 3 条


def write_facts(project: str, new_facts: list[dict], existing_data: dict, dry_run: bool) -> int:
    """追加新 facts 到 facts.json, 返回写入数量"""
    if not new_facts:
        return 0

    project_dir = AREAS_DIR / project
    facts_path = project_dir / "facts.json"

    existing_facts = existing_data.get("facts", [])
    next_id = get_next_fact_id(existing_facts)
    today = datetime.now().strftime("%Y-%m-%d")

    for i, nf in enumerate(new_facts):
        fact_entry = {
            "id": f"fact-{next_id + i:03d}",
            "fact": nf["fact"],
            "evidence": "auto-extract-facts.py 从本周 session 摘要提取",
            "timestamp": today,
            "status": "active",
            "category": nf["category"],
        }
        existing_facts.append(fact_entry)

    existing_data["facts"] = existing_facts

    if dry_run:
        print(f"  [预览] 将写入 {len(new_facts)} 条事实到 {facts_path}")
        for nf in new_facts:
            print(f"    - [{nf['category']}] {nf['fact']}")
        return len(new_facts)

    # 确保目录存在
    project_dir.mkdir(parents=True, exist_ok=True)

    facts_path.write_text(
        json.dumps(existing_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  [写入] {len(new_facts)} 条事实 → {facts_path}")
    return len(new_facts)


def process_project(
    project: str,
    sessions: list[Path],
    dry_run: bool,
) -> int:
    """处理单个项目, 返回写入事实数"""
    print(f"\n--- {project} ({len(sessions)} 个会话) ---")

    # 提取知识点
    knowledge_texts = []
    for sp in sessions:
        kp = extract_knowledge_points(sp)
        if kp:
            knowledge_texts.append(kp)

    if not knowledge_texts:
        print("  [跳过] 无可用知识点")
        return 0

    # 加载现有 facts
    existing_data = load_existing_facts(project)
    existing_facts = existing_data.get("facts", [])

    print(f"  知识点: {len(knowledge_texts)} 段, 已有事实: {len(existing_facts)} 条")

    # 构建 prompt
    system_prompt, user_prompt = build_prompt(project, knowledge_texts, existing_facts)

    if dry_run:
        print(f"  [预览] prompt 长度: system={len(system_prompt)}, user={len(user_prompt)}")
        # dry-run 时不调用 LLM，显示将要发送的信息
        print(f"  [预览] 将调用 claude haiku 提取事实")
        print(f"  [预览] 涉及知识点来源:")
        for sp in sessions:
            print(f"    - {sp.name}")
        return 0

    # 调用 LLM
    raw = call_haiku(system_prompt, user_prompt)
    if not raw:
        return 0

    # 解析响应
    parsed = parse_response(raw)
    if not parsed:
        return 0

    # 验证
    valid_facts = validate_facts(parsed)
    if not valid_facts:
        print("  [跳过] 提取的事实均无效")
        return 0

    # 写入
    return write_facts(project, valid_facts, existing_data, dry_run=False)


def main():
    parser = argparse.ArgumentParser(
        description="从 session 摘要自动提取原子事实"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式，不调用 LLM 也不写入文件",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="只处理指定项目 (areas/projects 下的目录名)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="扫描最近 N 天的 sessions (默认 7)",
    )
    args = parser.parse_args()

    print(f"自动事实提取 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"扫描范围: 最近 {args.days} 天")
    if args.dry_run:
        print("[预览模式]")

    # 扫描 sessions
    grouped = scan_sessions(days=args.days)

    if not grouped:
        print("\n未找到符合条件的 session 摘要")
        return

    print(f"\n发现 {len(grouped)} 个项目:")
    for proj, sessions in sorted(grouped.items()):
        print(f"  - {proj}: {len(sessions)} 个会话")

    # 过滤: 单项目模式或 >= 2 个 session
    total_written = 0

    for proj in sorted(grouped.keys()):
        sessions = grouped[proj]

        if args.project and proj != args.project:
            continue

        if not args.project and len(sessions) < 2:
            print(f"\n--- {proj} ({len(sessions)} 个会话) ---")
            print("  [跳过] 会话数 < 2")
            continue

        count = process_project(proj, sessions, dry_run=args.dry_run)
        total_written += count

    print(f"\n完成: 共写入 {total_written} 条新事实")


if __name__ == "__main__":
    main()
