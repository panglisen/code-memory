#!/usr/bin/env python3
"""
Python 脚本避坑自动检测引擎

2 条规则 (Python 3.9 兼容):
- R1 [block]: match/case 语法 (Python 3.10+)
- R2 [warn]:  str|None 联合类型语法 (Python 3.10+)

输入: 文件路径 (argv[1])
输出: JSON {"decision": "block"|"approve", "reason": "...", "warnings": [...]}

兼容 Python 3.9+
"""
import json
import re
import sys
from typing import Dict, List


def find_match_case_syntax(content):
    # type: (str) -> List[Dict]
    """R1: match/case 语法 (Python 3.10+)

    macOS /usr/bin/python3 可能是 3.9，不支持 match/case。
    应使用 if/elif 条件链替代。
    """
    issues = []

    # match 语句: `match expr:` 作为语句开头 (不在字符串/注释中)
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        # 跳过注释和字符串
        if stripped.startswith('#'):
            continue
        if stripped.startswith(('"""', "'''")):
            continue

        # match expr:
        if re.match(r'match\s+\w.*:\s*$', stripped):
            issues.append({
                'rule': 'R1',
                'severity': 'block',
                'line': i + 1,
                'message': (
                    "match/case 语法需要 Python 3.10+。"
                    "/usr/bin/python3 可能是 3.9，应使用 if/elif 条件链替代。"
                ),
            })
        # case value:
        elif re.match(r'case\s+(?!.*\bclass\b)\S.*:\s*$', stripped):
            # 排除 case class (不是 Python 语法)
            # 只有当附近有 match 时才报告
            pass  # match 已经报告过了

    return issues


def find_union_type_syntax(content):
    # type: (str) -> List[Dict]
    """R2: str|None 联合类型语法 (Python 3.10+)

    Python 3.9 不支持运行时类型联合 X | Y。
    应使用 Optional[X] 或 Union[X, Y] (from typing)。
    """
    issues = []

    lines = content.split('\n')
    in_docstring = False
    docstring_char = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # 简单的 docstring 跟踪
        if not in_docstring:
            if stripped.startswith('"""') or stripped.startswith("'''"):
                docstring_char = stripped[:3]
                if stripped.count(docstring_char) == 1:
                    in_docstring = True
                continue
        else:
            if docstring_char and docstring_char in stripped:
                in_docstring = False
            continue

        # 跳过注释
        if stripped.startswith('#'):
            continue

        # 跳过字符串赋值 (简化: 跳过含引号的行)
        if '|' not in stripped:
            continue

        # 检测类型注解中的 X | Y 模式
        # 匹配: def foo(x: str | None), x: int | str, -> str | None
        type_union_pattern = re.compile(
            r'(?::\s*|->)\s*[\w\[\].]+\s*\|\s*(?:None|[\w\[\].]+)',
        )
        for m in type_union_pattern.finditer(stripped):
            # 排除 __future__ annotations (从字符串评估, 合法)
            if 'from __future__ import annotations' in content:
                break
            issues.append({
                'rule': 'R2',
                'severity': 'warn',
                'line': i + 1,
                'message': (
                    "X | Y 联合类型语法需要 Python 3.10+ 运行时。"
                    "应使用 Optional[X] 或 Union[X, Y] (from typing)。"
                    "或添加 'from __future__ import annotations' 使类型注解延迟评估。"
                ),
            })
            break  # 每行只报一次

    return issues


def lint_file(filepath):
    # type: (str) -> Dict
    """对单个 Python 文件运行所有规则"""
    try:
        with open(filepath, encoding='utf-8', errors='replace') as f:
            content = f.read()
    except (OSError, IOError) as e:
        return {
            'decision': 'approve',
            'reason': '',
            'warnings': [],
            'error': str(e),
        }

    all_issues = []
    all_issues.extend(find_match_case_syntax(content))
    all_issues.extend(find_union_type_syntax(content))

    block_issues = [i for i in all_issues if i['severity'] == 'block']
    warn_issues = [i for i in all_issues if i['severity'] == 'warn']

    if block_issues:
        reasons = []
        for issue in block_issues:
            reasons.append("[{rule}] 第{line}行: {msg}".format(
                rule=issue['rule'], line=issue['line'], msg=issue['message']))
        return {
            'decision': 'block',
            'reason': '\n'.join(reasons),
            'warnings': [
                "[{rule}] 第{line}行: {msg}".format(
                    rule=i['rule'], line=i['line'], msg=i['message'])
                for i in warn_issues
            ],
        }

    return {
        'decision': 'approve',
        'reason': '',
        'warnings': [
            "[{rule}] 第{line}行: {msg}".format(
                rule=i['rule'], line=i['line'], msg=i['message'])
            for i in warn_issues
        ],
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'decision': 'approve', 'reason': '', 'warnings': []}))
        sys.exit(0)

    filepath = sys.argv[1]
    result = lint_file(filepath)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
