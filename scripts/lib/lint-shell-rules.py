#!/usr/bin/env python3
"""
Shell 脚本避坑自动检测引擎

2 条规则 (macOS bash 3.2 兼容):
- R1 [block]: declare -A (关联数组, bash 4.0+)
- R2 [warn]:  [[ -v varname ]] (变量存在检查, bash 4.2+)

输入: 文件路径 (argv[1])
输出: JSON {"decision": "block"|"approve", "reason": "...", "warnings": [...]}

兼容 Python 3.9+
"""
import json
import re
import sys
from typing import Dict, List


def find_declare_associative(content):
    # type: (str) -> List[Dict]
    """R1: declare -A (关联数组)

    macOS 自带 bash 3.2 不支持 declare -A。
    应使用 case 语句或其他兼容方案替代。
    """
    issues = []
    pattern = re.compile(r'^\s*declare\s+-A\b', re.MULTILINE)
    for m in pattern.finditer(content):
        line_num = content[:m.start()].count('\n') + 1
        issues.append({
            'rule': 'R1',
            'severity': 'block',
            'line': line_num,
            'message': (
                "declare -A (关联数组) 在 macOS bash 3.2 中不可用。"
                "应使用 case 语句或 POSIX 兼容方案替代。"
            ),
        })
    return issues


def find_test_v_flag(content):
    # type: (str) -> List[Dict]
    """R2: [[ -v varname ]] (变量存在检查)

    bash 4.2+ 特性，macOS bash 3.2 不支持。
    应使用 [ -n "${var+x}" ] 或 [ -z "${var-}" ] 替代。
    """
    issues = []
    pattern = re.compile(r'\[\[\s+-v\s+\w+', re.MULTILINE)
    for m in pattern.finditer(content):
        line_num = content[:m.start()].count('\n') + 1
        issues.append({
            'rule': 'R2',
            'severity': 'warn',
            'line': line_num,
            'message': (
                "[[ -v var ]] 在 macOS bash 3.2 中不可用 (需要 bash 4.2+)。"
                "应使用 [ -n \"${var+x}\" ] 检查变量是否已设置。"
            ),
        })
    return issues


def is_bash_script(content):
    # type: (str) -> bool
    """检查是否为 bash 脚本 (shebang 或无 shebang)"""
    first_line = content.split('\n', 1)[0].strip()
    # 如果有 shebang 且不是 bash，跳过 (如 #!/usr/bin/env zsh)
    if first_line.startswith('#!'):
        if 'bash' in first_line:
            return True
        if 'sh' in first_line and 'zsh' not in first_line and 'fish' not in first_line:
            return True  # #!/bin/sh 也检查 (POSIX 兼容更严格)
        return False
    # 无 shebang，假设是 bash
    return True


def lint_file(filepath):
    # type: (str) -> Dict
    """对单个 Shell 脚本运行所有规则"""
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

    if not is_bash_script(content):
        return {'decision': 'approve', 'reason': '', 'warnings': []}

    all_issues = []
    all_issues.extend(find_declare_associative(content))
    all_issues.extend(find_test_v_flag(content))

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
