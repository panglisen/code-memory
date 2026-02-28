#!/usr/bin/env python3
"""
antd v3 避坑自动检测引擎

5 条规则，分两个严重级别:
- block: 高置信度，直接拒绝编辑 (R1, R2)
- warn:  低置信度，stderr 提示 (R3, R4, R5)

输入: 文件路径 (argv[1])
输出: JSON {"decision": "block"|"approve", "reason": "...", "warnings": [...]}

兼容 Python 3.9+ (macOS /usr/bin/python3)
"""
import json
import re
import sys


def find_modal_static_calls_missing_prefix(content, lines):
    """R1: Modal.confirm/info/warning/error/success 缺少 prefixCls

    方法: 正则定位调用 → 大括号深度计数提取配置对象 → 检查 prefixCls
    """
    issues = []
    # 匹配 Modal.confirm({ ... }), Modal.info({ ... }) 等
    pattern = re.compile(
        r'Modal\.(confirm|info|warning|error|success)\s*\(',
        re.MULTILINE,
    )
    for match in pattern.finditer(content):
        call_start = match.start()
        # 找到左括号后的 { 开始
        paren_pos = content.index('(', call_start + len('Modal.'))
        rest = content[paren_pos:]

        # 提取括号内的配置对象文本 (大括号深度计数)
        config_text = _extract_brace_content(rest)
        if config_text is None:
            continue

        # 检查是否包含 prefixCls
        if 'prefixCls' not in config_text:
            line_num = content[:call_start].count('\n') + 1
            method = match.group(1)
            issues.append({
                'rule': 'R1',
                'severity': 'block',
                'line': line_num,
                'message': (
                    f"Modal.{method}() 缺少 prefixCls: 'bscrmCSS-modal'。"
                    f"静态方法创建的 DOM 在 React 树外，不继承 ConfigProvider 的 prefixCls。"
                ),
            })
    return issues


def _extract_brace_content(text):
    """从文本中提取第一对大括号 {...} 的内容 (支持嵌套)"""
    brace_start = text.find('{')
    if brace_start == -1:
        return None
    depth = 0
    i = brace_start
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[brace_start:i + 1]
        # 跳过字符串内容 (避免误计大括号)
        elif ch in ('"', "'", '`'):
            quote = ch
            i += 1
            while i < len(text) and text[i] != quote:
                if text[i] == '\\':
                    i += 1  # 跳过转义
                i += 1
        i += 1
    return None


def find_error_fields_v4_pattern(content, lines):
    """R2: errorFields (antd v4 模式) 用于 antd v3 项目

    antd v3 validateFields 的错误格式是 {fieldName: {errors: [...]}}, 没有 errorFields
    """
    issues = []
    pattern = re.compile(r'\.errorFields\b|hasOwnProperty\s*\(\s*[\'"]errorFields[\'"]\s*\)')
    for match in pattern.finditer(content):
        line_num = content[:match.start()].count('\n') + 1
        issues.append({
            'rule': 'R2',
            'severity': 'block',
            'line': line_num,
            'message': (
                "使用了 antd v4 的 errorFields 属性。"
                "antd v3 validateFields 错误格式是 {fieldName: {errors: [{message}]}}，"
                "没有 errorFields。请用 Object.values(err).some(v => v && v.errors) 检测。"
            ),
        })
    return issues


def find_set_timeout_set_fields_value(content, lines):
    """R3: setTimeout + setFieldsValue 组合 (500 字符范围内共现)

    用 setTimeout 等待 React 渲染后 setFieldsValue 不可靠，应用 initialValue 从 props 派生。
    """
    issues = []
    timeout_pattern = re.compile(r'setTimeout\s*\(')
    for match in timeout_pattern.finditer(content):
        # 在 setTimeout 调用后 500 字符范围内查找 setFieldsValue
        window_start = match.start()
        window_end = min(len(content), match.end() + 500)
        window = content[window_start:window_end]
        if 'setFieldsValue' in window:
            line_num = content[:match.start()].count('\n') + 1
            issues.append({
                'rule': 'R3',
                'severity': 'warn',
                'line': line_num,
                'message': (
                    "setTimeout + setFieldsValue 组合: 时序不可靠。"
                    "建议用 getFieldDecorator({initialValue: propValue}) 从 prop 派生。"
                ),
            })
    return issues


def find_use_state_async_props(content, lines):
    """R4: useState(props.xxx) 异步 props 初始化

    useState 初始值只在组件首次挂载时取值一次，异步加载的 props 变化后 state 不会更新。
    """
    issues = []
    # 匹配 useState(props.xxx) 或 useState(someObj.xxx) 但排除简单字面量
    pattern = re.compile(
        r'useState\s*\(\s*(?:props|this\.props)\s*\.\s*\w+',
    )
    for match in pattern.finditer(content):
        line_num = content[:match.start()].count('\n') + 1
        issues.append({
            'rule': 'R4',
            'severity': 'warn',
            'line': line_num,
            'message': (
                "useState(props.xxx): props 变化后 state 不会自动更新。"
                "如果 props 是异步加载的，应直接从 props 派生或用 useEffect 同步。"
            ),
        })
    return issues


def find_normalize_leak(content, lines):
    """R5: normalize 函数泄漏 — 定义了 normalize 但有地方用了原始值比较

    检测: 找到 normalizeCondType (或类似 normalize 函数) 的定义/导入，
    然后检查是否存在未包装的原始 condType/cond.type 值用于 === 比较。
    """
    issues = []

    # 检测是否存在 normalize 函数 (定义、导入、赋值)
    normalize_names = set()
    # 匹配: import { normalizeCondType }, const normalizeX = ..., function normalizeX
    patterns = [
        re.compile(r'import\s+\{[^}]*\b(\w*[Nn]ormalize\w*)\b[^}]*\}'),
        re.compile(r'(?:const|let|var|function)\s+(\w*[Nn]ormalize\w*)'),
    ]
    for pat in patterns:
        for m in pat.finditer(content):
            normalize_names.add(m.group(1))

    if not normalize_names:
        return issues

    # 查找未经 normalize 的原始值比较
    # 匹配: cond.type === 'xxx' 或 condType === 'xxx' (未被 normalize 包装)
    raw_compare_pattern = re.compile(
        r'(?<!\w)(?:cond\.type|condType|condition\.type)\s*===?\s*[\'"]',
    )
    for match in raw_compare_pattern.finditer(content):
        # 检查此比较是否在 normalize 调用内
        # 向前看 50 个字符是否有 normalize 调用
        lookback_start = max(0, match.start() - 50)
        lookback = content[lookback_start:match.start()]
        is_wrapped = any(name in lookback for name in normalize_names)
        if not is_wrapped:
            line_num = content[:match.start()].count('\n') + 1
            issues.append({
                'rule': 'R5',
                'severity': 'warn',
                'line': line_num,
                'message': (
                    f"原始值比较未经 normalize 处理 (已定义 {list(normalize_names)})。"
                    f"应使用 normalize 函数包装后再比较，否则大小写不一致会导致逻辑分支不匹配。"
                ),
            })
    return issues


def lint_file(filepath):
    """对单个文件运行所有规则，返回结果 dict"""
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

    lines = content.split('\n')

    all_issues = []
    all_issues.extend(find_modal_static_calls_missing_prefix(content, lines))
    all_issues.extend(find_error_fields_v4_pattern(content, lines))
    all_issues.extend(find_set_timeout_set_fields_value(content, lines))
    all_issues.extend(find_use_state_async_props(content, lines))
    all_issues.extend(find_normalize_leak(content, lines))

    block_issues = [i for i in all_issues if i['severity'] == 'block']
    warn_issues = [i for i in all_issues if i['severity'] == 'warn']

    if block_issues:
        # 合并所有 block 原因
        reasons = []
        for issue in block_issues:
            reasons.append(f"[{issue['rule']}] 第{issue['line']}行: {issue['message']}")
        return {
            'decision': 'block',
            'reason': '\n'.join(reasons),
            'warnings': [
                f"[{i['rule']}] 第{i['line']}行: {i['message']}"
                for i in warn_issues
            ],
        }

    warnings = [
        f"[{i['rule']}] 第{i['line']}行: {i['message']}"
        for i in warn_issues
    ]
    return {
        'decision': 'approve',
        'reason': '',
        'warnings': warnings,
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
