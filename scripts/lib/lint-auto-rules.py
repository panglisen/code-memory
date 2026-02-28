#!/usr/bin/env python3
"""
lint-auto-rules.py - 通用规则引擎

读取 auto-rules.json 配置文件，对目标文件进行正则检测。
纯正则匹配，无 LLM 调用，延迟 ~1ms。

输入: 文件路径 (argv[1])
输出: JSON {"decision": "block"|"approve", "reason": "...", "warnings": [...]}

兼容 Python 3.9+
"""

import fnmatch
import json
import os
import re
import sys
from typing import Dict, List

RULES_FILE = os.path.expanduser(
    "~/.claude/memory/evolution/auto-rules.json")

# 正则 flag 映射
FLAG_MAP = {
    "MULTILINE": re.MULTILINE,
    "IGNORECASE": re.IGNORECASE,
    "DOTALL": re.DOTALL,
}


def load_auto_rules():
    # type: () -> List[Dict]
    """加载 auto-rules.json 中的活跃规则"""
    if not os.path.exists(RULES_FILE):
        return []
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    # 只返回非 skipped 的规则
    return [r for r in data.get("rules", []) if not r.get("skipped")]


def matches_glob(filepath, file_globs):
    # type: (str, List[str]) -> bool
    """检查文件路径是否匹配 glob 模式列表"""
    basename = os.path.basename(filepath)
    for pattern in file_globs:
        if fnmatch.fnmatch(basename, pattern):
            return True
        # 也尝试对完整路径匹配
        if fnmatch.fnmatch(filepath, pattern):
            return True
    return False


def build_flags(flag_names):
    # type: (List[str]) -> int
    """构建正则 flag"""
    flags = 0
    for name in flag_names:
        flags |= FLAG_MAP.get(name, 0)
    return flags


def lint_file(filepath):
    # type: (str) -> Dict
    """对文件运行所有 auto-rules"""
    rules = load_auto_rules()
    if not rules:
        return {"decision": "approve", "reason": "", "warnings": []}

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, IOError) as e:
        return {
            "decision": "approve",
            "reason": "",
            "warnings": [],
            "error": str(e),
        }

    all_issues = []

    for rule in rules:
        file_globs = rule.get("file_globs", ["*"])
        if not matches_glob(filepath, file_globs):
            continue

        pattern = rule.get("pattern", "")
        if not pattern:
            continue

        flags = build_flags(rule.get("pattern_flags", []))

        try:
            compiled = re.compile(pattern, flags)
        except re.error:
            continue

        for match in compiled.finditer(content):
            line_num = content[:match.start()].count("\n") + 1
            all_issues.append({
                "rule": rule.get("id", "auto-???"),
                "severity": rule.get("severity", "warn"),
                "line": line_num,
                "message": rule.get("message", ""),
                "fix_hint": rule.get("fix_hint", ""),
            })

    block_issues = [i for i in all_issues if i["severity"] == "block"]
    warn_issues = [i for i in all_issues if i["severity"] == "warn"]

    if block_issues:
        reasons = []
        for issue in block_issues:
            reasons.append("[{rule}] 第{line}行: {msg}".format(
                rule=issue["rule"], line=issue["line"], msg=issue["message"]))
        return {
            "decision": "block",
            "reason": "\n".join(reasons),
            "warnings": [
                "[{rule}] 第{line}行: {msg}".format(
                    rule=i["rule"], line=i["line"], msg=i["message"])
                for i in warn_issues
            ],
        }

    return {
        "decision": "approve",
        "reason": "",
        "warnings": [
            "[{rule}] 第{line}行: {msg}".format(
                rule=i["rule"], line=i["line"], msg=i["message"])
            for i in warn_issues
        ],
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"decision": "approve", "reason": "", "warnings": []}))
        sys.exit(0)

    filepath = sys.argv[1]
    result = lint_file(filepath)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
