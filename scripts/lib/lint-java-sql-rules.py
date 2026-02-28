#!/usr/bin/env python3
"""
Java DAO/Mapper SQL 避坑自动检测引擎

4 条规则:
- R1 [warn]:  LEFT JOIN + WHERE 过滤被 JOIN 表字段 (隐式转 INNER JOIN)
- R2 [warn]:  IN (SELECT ...) 子查询 (大表时导致全表扫描)
- R3 [block]: company_id 硬编码常量值 (绕过分表拦截器)
- R4 [warn]:  CREATE TABLE 缺少显式 COLLATE 指定

输入: 文件路径 (argv[1])
输出: JSON {"decision": "block"|"approve", "reason": "...", "warnings": [...]}

兼容 Python 3.9+ (macOS /usr/bin/python3)
"""
import json
import re
import sys
from typing import Dict, List, Tuple


def extract_sql_strings(content):
    # type: (str) -> List[Tuple[str, int]]
    """从 Java 注解中提取 SQL 字符串及其起始行号"""
    sqls = []

    # @Select("..."), @Insert("..."), @Update("..."), @Delete("...")
    single_pattern = re.compile(
        r'@(?:Select|Insert|Update|Delete)\s*\(\s*"((?:[^"\\]|\\"|\\\\)*)"',
        re.DOTALL,
    )
    for m in single_pattern.finditer(content):
        sql = m.group(1).replace('\\"', '"').replace('\\n', '\n')
        line_num = content[:m.start()].count('\n') + 1
        sqls.append((sql, line_num))

    # @Select({"...", "..."}) 多行格式
    multi_pattern = re.compile(
        r'@(?:Select|Insert|Update|Delete)\s*\(\s*\{([^}]*)\}',
        re.DOTALL,
    )
    for m in multi_pattern.finditer(content):
        block = m.group(1)
        parts = []
        for sm in re.finditer(r'"((?:[^"\\]|\\"|\\\\)*)"', block):
            parts.append(sm.group(1).replace('\\"', '"').replace('\\n', '\n'))
        if parts:
            combined_sql = ' '.join(parts)
            line_num = content[:m.start()].count('\n') + 1
            sqls.append((combined_sql, line_num))

    # 字符串拼接: "SELECT ... " + "FROM ..." 模式 (简化处理)
    concat_pattern = re.compile(
        r'(?:String\s+\w+\s*=\s*|return\s+)"((?:SELECT|INSERT|UPDATE|DELETE)\b[^"]*)"',
        re.IGNORECASE,
    )
    for m in concat_pattern.finditer(content):
        sql = m.group(1)
        line_num = content[:m.start()].count('\n') + 1
        sqls.append((sql, line_num))

    return sqls


def find_left_join_where_filter(sql, base_line):
    # type: (str, int) -> List[Dict]
    """R1: LEFT JOIN + WHERE 过滤被 JOIN 表字段

    WHERE 子句过滤被 LEFT JOIN 表的字段会隐式转为 INNER JOIN。
    应把过滤条件放到 ON 子句中。
    """
    issues = []

    # 提取所有 LEFT JOIN 的表别名
    join_pattern = re.compile(
        r'LEFT\s+(?:OUTER\s+)?JOIN\s+(\w+)\s+(?:AS\s+)?(\w+)',
        re.IGNORECASE,
    )
    aliases = []
    for m in join_pattern.finditer(sql):
        aliases.append(m.group(2))  # 别名

    if not aliases:
        return issues

    # 提取 WHERE 子句
    where_match = re.search(r'\bWHERE\b(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|$)',
                            sql, re.IGNORECASE | re.DOTALL)
    if not where_match:
        return issues

    where_clause = where_match.group(1)

    # 检查 WHERE 中是否引用了 LEFT JOIN 表的别名
    for alias in aliases:
        # 匹配 alias.column 但排除 IS NULL / IS NOT NULL 检查 (合法用法)
        ref_pattern = re.compile(
            r'\b' + re.escape(alias) + r'\.(\w+)\s*(?:=|!=|<>|>|<|>=|<=|LIKE|IN\b)',
            re.IGNORECASE,
        )
        for ref in ref_pattern.finditer(where_clause):
            col = ref.group(1)
            # delete_flag 在 ON 和 WHERE 中都常见，跳过
            if col.lower() in ('delete_flag', 'del_flag', 'is_deleted'):
                continue
            issues.append({
                'rule': 'R1',
                'severity': 'warn',
                'line': base_line,
                'message': (
                    "LEFT JOIN 表 {alias} 的字段 {alias}.{col} 出现在 WHERE 子句中，"
                    "会隐式转为 INNER JOIN。应将过滤条件移至 ON 子句。"
                ).format(alias=alias, col=col),
            })
            break  # 每个别名只报一次

    return issues


def find_in_subquery(sql, base_line):
    # type: (str, int) -> List[Dict]
    """R2: IN (SELECT ...) 子查询

    尤其是子查询涉及大表时，会导致全表扫描。
    应改为单表查询后在内存过滤或用主键 JOIN。
    """
    issues = []
    pattern = re.compile(r'\bIN\s*\(\s*SELECT\b', re.IGNORECASE)
    for m in pattern.finditer(sql):
        issues.append({
            'rule': 'R2',
            'severity': 'warn',
            'line': base_line,
            'message': (
                "IN (SELECT ...) 子查询: 当子查询涉及大表时会导致全表扫描。"
                "建议改为单表查询后在 Service 层内存过滤，或改用 JOIN。"
            ),
        })
    return issues


def find_company_id_hardcoded(sql, base_line):
    # type: (str, int) -> List[Dict]
    """R3: company_id 硬编码常量值

    在分表 DAO 的 SQL WHERE 中显式写 company_id 常量值，
    会绕过分表拦截器，导致跨租户数据泄露或路由错误。
    应使用 #{companyId} 参数化。
    """
    issues = []
    # 匹配 company_id = 'xxx' 或 company_id = 123 (不含 #{} 或 ${})
    patterns = [
        re.compile(r"company_id\s*=\s*'[^#${}]", re.IGNORECASE),
        re.compile(r'company_id\s*=\s*"[^#${}]', re.IGNORECASE),
        re.compile(r'company_id\s*=\s*\d+', re.IGNORECASE),
    ]
    for pat in patterns:
        for m in pat.finditer(sql):
            issues.append({
                'rule': 'R3',
                'severity': 'block',
                'line': base_line,
                'message': (
                    "company_id 使用硬编码常量值。"
                    "在分表 DAO 中会绕过分表拦截器，导致跨租户数据泄露。"
                    "应使用 #{companyId} 参数化传入。"
                ),
            })
            return issues  # 找到一个就够了
    return issues


def find_create_table_missing_collation(content, lines):
    # type: (str, list) -> List[Dict]
    """R4: CREATE TABLE 缺少显式 COLLATE 指定

    MySQL 8.0 默认 utf8mb4_0900_ai_ci 与历史表 utf8mb4_general_ci
    JOIN 时会报 collation 不匹配错误。
    """
    issues = []
    pattern = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?\w+`?',
        re.IGNORECASE,
    )
    for m in pattern.finditer(content):
        # 找到 CREATE TABLE 语句的结尾 (分号或文件结束)
        rest = content[m.start():]
        end_pos = rest.find(';')
        if end_pos == -1:
            stmt = rest
        else:
            stmt = rest[:end_pos + 1]

        # 检查是否包含 COLLATE
        if not re.search(r'\bCOLLATE\b', stmt, re.IGNORECASE):
            line_num = content[:m.start()].count('\n') + 1
            issues.append({
                'rule': 'R4',
                'severity': 'warn',
                'line': line_num,
                'message': (
                    "CREATE TABLE 缺少显式 COLLATE 指定。"
                    "MySQL 8.0 默认 utf8mb4_0900_ai_ci 与历史表 utf8mb4_general_ci "
                    "JOIN 时会报错。建议添加 COLLATE=utf8mb4_general_ci。"
                ),
            })
    return issues


def lint_file(filepath):
    # type: (str) -> Dict
    """对单个 Java DAO 文件运行所有 SQL 规则"""
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

    # 提取 SQL 字符串并检查 R1, R2, R3
    sqls = extract_sql_strings(content)
    for sql, base_line in sqls:
        all_issues.extend(find_left_join_where_filter(sql, base_line))
        all_issues.extend(find_in_subquery(sql, base_line))
        all_issues.extend(find_company_id_hardcoded(sql, base_line))

    # R4: CREATE TABLE (直接在文件内容中搜索)
    all_issues.extend(find_create_table_missing_collation(content, lines))

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

    warnings = [
        "[{rule}] 第{line}行: {msg}".format(
            rule=i['rule'], line=i['line'], msg=i['message'])
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
