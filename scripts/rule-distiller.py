#!/usr/bin/env python3
"""
rule-distiller.py - 避坑经验 → 检测规则 自动蒸馏

从 MEMORY.md 和项目级 rules.md 中提取避坑经验，
调用 LLM (Haiku) 判断是否可正则化，生成 auto-rules.json。

用法:
    rule-distiller.py                    # 自动扫描+生成
    rule-distiller.py --dry-run          # 预览模式
    rule-distiller.py --force            # 跳过去重强制重新分析
    rule-distiller.py --list             # 列出已生成的规则
    rule-distiller.py --remove auto-003  # 删除指定规则

兼容 Python 3.9+
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

MEMORY_DIR = os.path.expanduser("~/.claude/memory")
MEMORY_FILE = os.path.join(MEMORY_DIR, "MEMORY.md")
PROJECTS_DIR = os.path.join(MEMORY_DIR, "areas/projects")
RULES_FILE = os.path.join(MEMORY_DIR, "evolution/auto-rules.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 已有手写规则的关键词描述 (用于去重)
EXISTING_RULE_KEYWORDS = [
    "declare -A",
    "[[ -v",
    "match/case",
    "match-case",
    "str|None",
    "联合类型",
    "Modal.confirm",
    "prefixCls",
    "errorFields",
    "setTimeout",
    "setFieldsValue",
    "useState",
    "normalizeCondType",
    "normalize",
    "LEFT JOIN",
    "IN (SELECT",
    "子查询",
    "company_id",
    "companyId",
    "COLLATE",
    "utf8mb4",
]

LLM_TIMEOUT = 30
SUMMARY_MODEL = "haiku"


def short_hash(text):
    # type: (str) -> str
    """生成文本的 6 位短 hash"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def load_auto_rules():
    # type: () -> Dict
    """加载已有的 auto-rules.json"""
    if not os.path.exists(RULES_FILE):
        return {"version": 1, "generated_at": "", "rules": []}
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "generated_at": "", "rules": []}


def save_auto_rules(data):
    # type: (Dict) -> None
    """保存 auto-rules.json"""
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(RULES_FILE), exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def extract_avoidances_from_file(filepath):
    # type: (str) -> List[Tuple[str, str]]
    """从文件中提取避坑经验条目，返回 [(经验文本, 来源文件)]"""
    results = []
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, IsADirectoryError):
        return results

    in_avoidance_section = False
    in_relevant_section = False

    for line in lines:
        stripped = line.strip()

        # 检测 section header
        if stripped.startswith("## "):
            section_name = stripped[3:].strip()
            in_avoidance_section = section_name in ("避坑经验", "通用开发原则")
            in_relevant_section = section_name in (
                "禁止项", "编码规范", "架构分层", "缓存规范",
                "API 规范", "数据库规范", "注意事项", "规范",
            )
            continue

        # 提取列表项
        if not stripped.startswith("- "):
            continue

        if not (in_avoidance_section or in_relevant_section):
            continue

        # 提取经验文本
        text = stripped[2:].strip()

        # 去掉 **加粗** 标记中的文本作为经验内容
        bold_match = re.search(r"\*\*(.*?)\*\*", text)
        if bold_match:
            text = bold_match.group(1)
        else:
            # 纯文本: 取冒号前部分
            text = re.split(r"[：:]", text)[0].strip()

        # 过滤太短的条目
        if len(text) < 8:
            continue

        # 去掉末尾的日期标记
        text = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}.*?\)\s*$", "", text)

        results.append((text, filepath))

    return results


def collect_avoidances():
    # type: () -> List[Tuple[str, str]]
    """收集所有避坑经验来源"""
    all_avoidances = []

    # 1. MEMORY.md
    if os.path.exists(MEMORY_FILE):
        all_avoidances.extend(extract_avoidances_from_file(MEMORY_FILE))

    # 2. 项目级 rules.md
    if os.path.isdir(PROJECTS_DIR):
        for project_name in os.listdir(PROJECTS_DIR):
            rules_path = os.path.join(PROJECTS_DIR, project_name, "rules.md")
            if os.path.isfile(rules_path):
                all_avoidances.extend(extract_avoidances_from_file(rules_path))

    return all_avoidances


def is_covered_by_existing_rules(text):
    # type: (str) -> bool
    """检查经验是否已被手写规则覆盖"""
    text_lower = text.lower()
    for keyword in EXISTING_RULE_KEYWORDS:
        # 使用纯字符串包含检查，避免正则特殊字符问题
        if keyword.lower() in text_lower:
            return True
    return False


def filter_new_avoidances(avoidances, existing_rules, force=False):
    # type: (List[Tuple[str, str]], Dict, bool) -> List[Tuple[str, str]]
    """过滤出未处理的新经验"""
    if force:
        return avoidances

    # 已处理的 hash 集合
    processed_hashes = set()
    for rule in existing_rules.get("rules", []):
        h = rule.get("source_hash", "")
        if h:
            processed_hashes.add(h)

    new_avoidances = []
    for text, source in avoidances:
        h = short_hash(text)
        if h in processed_hashes:
            continue
        if is_covered_by_existing_rules(text):
            continue
        new_avoidances.append((text, source))

    return new_avoidances


def call_llm(avoidances):
    # type: (List[Tuple[str, str]]) -> Optional[List[Dict]]
    """批量调用 LLM 判断经验是否可正则化"""
    if not avoidances:
        return []

    # 构造输入
    items = []
    for i, (text, source) in enumerate(avoidances):
        items.append("{}. {}".format(i + 1, text))
    items_text = "\n".join(items)

    system_prompt = """你是代码检测规则生成器。判断每条避坑经验能否转化为正则检测规则。

对每条经验，输出 JSON 数组中的一个对象：
{
  "index": 1,
  "detectable": true/false,
  "file_globs": ["*.sh"],
  "pattern": "^\\\\s*declare\\\\s+-A\\\\b",
  "pattern_flags": ["MULTILINE"],
  "message": "简短的检测提示消息",
  "fix_hint": "修复建议",
  "confidence": 0.9
}

注意:
- "先查后用"、"环境先验"、"编译验证" 这类方法论建议 → detectable: false
- "不要用 declare -A"、"company_id 不要硬编码" → detectable: true
- 只有高置信度的简单模式才输出 detectable: true，复杂语义判断输出 false
- pattern 必须是合法的 Python re 模块正则表达式
- pattern_flags 可选值: MULTILINE, IGNORECASE, DOTALL
- severity 始终为 "warn" (自动规则不 block)
- confidence 范围 0.0-1.0，低于 0.7 的不要输出 detectable: true
- file_globs 示例: ["*.sh"], ["*.py"], ["*.java"], ["*.js", "*.jsx", "*.ts", "*.tsx"]
- detectable: false 时其他字段可省略

直接输出 JSON 数组，不要任何解释文字。以 [ 开头，以 ] 结尾。"""

    user_prompt = "以下是需要分析的避坑经验列表：\n\n{}".format(items_text)

    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", SUMMARY_MODEL,
                "--no-session-persistence",
                "--system-prompt", system_prompt,
            ],
            input=user_prompt,
            capture_output=True,
            text=True,
            timeout=LLM_TIMEOUT,
        )
        if result.returncode != 0:
            print("[rule-distiller] LLM 调用失败: {}".format(
                result.stderr[:200]), file=sys.stderr)
            return None

        output = result.stdout.strip()
        # 提取 JSON 数组
        start = output.find("[")
        end = output.rfind("]")
        if start == -1 or end == -1:
            print("[rule-distiller] LLM 输出无法解析为 JSON 数组",
                  file=sys.stderr)
            return None

        return json.loads(output[start:end + 1])

    except subprocess.TimeoutExpired:
        print("[rule-distiller] LLM 调用超时 ({}s)".format(LLM_TIMEOUT),
              file=sys.stderr)
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("[rule-distiller] 解析错误: {}".format(e), file=sys.stderr)
        return None


def validate_regex(pattern, flags_list):
    # type: (str, List[str]) -> bool
    """验证正则表达式是否合法"""
    flag_map = {
        "MULTILINE": re.MULTILINE,
        "IGNORECASE": re.IGNORECASE,
        "DOTALL": re.DOTALL,
    }
    flags = 0
    for f in flags_list:
        flags |= flag_map.get(f, 0)

    try:
        compiled = re.compile(pattern, flags)
        # 简单测试: 对空字符串运行不报错
        compiled.search("")
        return True
    except re.error:
        return False


def generate_rule_id(existing_rules):
    # type: (Dict) -> str
    """生成下一个规则 ID"""
    max_num = 0
    for rule in existing_rules.get("rules", []):
        rule_id = rule.get("id", "")
        m = re.match(r"auto-(\d+)", rule_id)
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return "auto-{:03d}".format(max_num + 1)


def distill(dry_run=False, force=False):
    # type: (bool, bool) -> int
    """主蒸馏流程，返回新增规则数"""
    print("[rule-distiller] 收集避坑经验...", file=sys.stderr)
    avoidances = collect_avoidances()
    print("[rule-distiller] 发现 {} 条经验".format(len(avoidances)),
          file=sys.stderr)

    existing_rules = load_auto_rules()
    new_avoidances = filter_new_avoidances(avoidances, existing_rules, force)
    print("[rule-distiller] 其中 {} 条为新经验 (未处理/未覆盖)".format(
        len(new_avoidances)), file=sys.stderr)

    if not new_avoidances:
        print("[rule-distiller] 无新经验需要处理", file=sys.stderr)
        return 0

    # 批量限制: 每次最多 20 条
    batch = new_avoidances[:20]
    print("[rule-distiller] 调用 LLM 分析 {} 条...".format(len(batch)),
          file=sys.stderr)

    llm_results = call_llm(batch)
    if llm_results is None:
        print("[rule-distiller] LLM 调用失败，跳过", file=sys.stderr)
        return 0

    new_rules_count = 0
    for item in llm_results:
        idx = item.get("index", 0) - 1
        if idx < 0 or idx >= len(batch):
            continue

        text, source = batch[idx]
        source_hash = short_hash(text)

        if not item.get("detectable", False):
            # 不可正则化，但仍记录 hash 避免重复处理
            # 用一个特殊的 skipped 标记
            existing_rules["rules"].append({
                "id": "skip-{}".format(source_hash),
                "source": text,
                "source_hash": source_hash,
                "skipped": True,
                "reason": "not_detectable",
                "generated_at": datetime.now().strftime("%Y-%m-%d"),
            })
            continue

        confidence = item.get("confidence", 0.0)
        if confidence < 0.7:
            print("[rule-distiller] 跳过低置信度 ({:.1f}): {}".format(
                confidence, text[:50]), file=sys.stderr)
            existing_rules["rules"].append({
                "id": "skip-{}".format(source_hash),
                "source": text,
                "source_hash": source_hash,
                "skipped": True,
                "reason": "low_confidence",
                "generated_at": datetime.now().strftime("%Y-%m-%d"),
            })
            continue

        pattern = item.get("pattern", "")
        pattern_flags = item.get("pattern_flags", [])

        if not pattern:
            continue

        # 验证正则
        if not validate_regex(pattern, pattern_flags):
            print("[rule-distiller] 正则验证失败: {}".format(pattern),
                  file=sys.stderr)
            continue

        rule_id = generate_rule_id(existing_rules)
        new_rule = {
            "id": rule_id,
            "source": text,
            "source_hash": source_hash,
            "file_globs": item.get("file_globs", ["*"]),
            "pattern": pattern,
            "pattern_flags": pattern_flags,
            "severity": "warn",  # 自动规则一律 warn
            "message": item.get("message", text),
            "fix_hint": item.get("fix_hint", ""),
            "confidence": confidence,
            "generated_at": datetime.now().strftime("%Y-%m-%d"),
        }

        if dry_run:
            print("\n[预览] 将生成规则:")
            print(json.dumps(new_rule, ensure_ascii=False, indent=2))
        else:
            existing_rules["rules"].append(new_rule)

        new_rules_count += 1

    if not dry_run and new_rules_count > 0:
        save_auto_rules(existing_rules)
        print("[rule-distiller] 已写入 {} 条新规则到 {}".format(
            new_rules_count, RULES_FILE), file=sys.stderr)
    elif not dry_run:
        # 即使没有新规则，也保存 skipped 记录
        save_auto_rules(existing_rules)

    return new_rules_count


def list_rules():
    # type: () -> None
    """列出已生成的规则"""
    data = load_auto_rules()
    rules = [r for r in data.get("rules", []) if not r.get("skipped")]
    skipped = [r for r in data.get("rules", []) if r.get("skipped")]

    if not rules and not skipped:
        print("暂无自动生成的规则")
        return

    if rules:
        print("=== 活跃规则 ({} 条) ===\n".format(len(rules)))
        for r in rules:
            print("  [{id}] (confidence: {conf:.1f}, severity: {sev})".format(
                id=r["id"], conf=r.get("confidence", 0), sev=r.get("severity", "warn")))
            print("    来源: {}".format(r.get("source", "")[:60]))
            print("    模式: {}".format(r.get("pattern", "")))
            print("    文件: {}".format(", ".join(r.get("file_globs", []))))
            print("    消息: {}".format(r.get("message", "")))
            print()

    if skipped:
        print("=== 已跳过 ({} 条) ===\n".format(len(skipped)))
        for r in skipped:
            print("  [{}] {} (原因: {})".format(
                r.get("id", "?"),
                r.get("source", "")[:50],
                r.get("reason", "unknown"),
            ))


def remove_rule(rule_id):
    # type: (str) -> bool
    """删除指定规则"""
    data = load_auto_rules()
    original_count = len(data.get("rules", []))
    data["rules"] = [r for r in data.get("rules", []) if r.get("id") != rule_id]

    if len(data["rules"]) == original_count:
        print("规则 {} 不存在".format(rule_id), file=sys.stderr)
        return False

    save_auto_rules(data)
    print("已删除规则: {}".format(rule_id))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="从避坑经验中蒸馏检测规则")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式，不写入文件")
    parser.add_argument("--force", action="store_true",
                        help="跳过去重，强制重新分析所有经验")
    parser.add_argument("--list", action="store_true",
                        help="列出已生成的规则")
    parser.add_argument("--remove", metavar="RULE_ID",
                        help="删除指定规则")

    args = parser.parse_args()

    if args.list:
        list_rules()
        return

    if args.remove:
        success = remove_rule(args.remove)
        sys.exit(0 if success else 1)

    count = distill(dry_run=args.dry_run, force=args.force)
    if args.dry_run:
        print("\n[预览模式] 共 {} 条规则待生成 (未写入)".format(count))
    else:
        print("[rule-distiller] 完成，新增 {} 条规则".format(count),
              file=sys.stderr)


if __name__ == "__main__":
    main()
