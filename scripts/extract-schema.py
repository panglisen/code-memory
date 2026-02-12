#!/usr/bin/env python3
"""
extract-schema.py v2 - 统一数据库 Schema 管理

从 MySQL INFORMATION_SCHEMA 获取表注释+列信息，与代码提取的 JOIN/DAO 数据合并，
按表名前缀分组，生成全局唯一一份 schema。

配置:
    业务域提示和特殊前缀从 ~/.claude/memory/config.json 读取:
    {
      "domain_hints": {"user": {"label": "用户", "keywords": ["用户表", "会员"]}},
      "special_prefixes": ["e_wechat", "t_sys"],
      "source_labels": {"/path/to/project-a": "project-a"}
    }

依赖:
    brew install ast-grep
    pip3 install pymysql sqlglot

用法:
    python3 extract-schema.py \
      --db-host 127.0.0.1 --db-port 3306 \
      --db-user root --db-password xxx --db-name my_database \
      --sources /path/to/project-a,/path/to/project-b \
      --source-labels project-a,project-b \
      --project shared-db \
      [--dry-run]
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    import pymysql
except ImportError:
    print("错误: 需要安装 pymysql: pip3 install pymysql", file=sys.stderr)
    sys.exit(1)

try:
    from sqlglot import parse as sql_parse, exp
    from sqlglot.errors import ErrorLevel
except ImportError:
    print("错误: 需要安装 sqlglot: pip3 install sqlglot", file=sys.stderr)
    sys.exit(1)

logging.getLogger("sqlglot").setLevel(logging.ERROR)

MEMORY_BASE = Path.home() / ".claude" / "memory" / "areas" / "projects"
CONFIG_PATH = Path.home() / ".claude" / "memory" / "config.json"
ANNOTATIONS = ["Select", "Insert", "Update", "Delete"]

# 从配置文件加载
def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}

config = load_config()

# 特殊前缀：这些包含下划线但应视为整体（从配置读取）
SPECIAL_PREFIXES = config.get("special_prefixes", [])

# 前缀分组阈值
MIN_GROUP_SIZE = 3
MAX_GROUP_SIZE = 40

# 源码路径 → 项目标签映射（从配置或 CLI 参数读取）
SOURCE_LABELS: dict[str, str] = config.get("source_labels", {})


# ──────────────────────────────────────────────
# MySQL 数据获取
# ──────────────────────────────────────────────

def fetch_db_tables(conn) -> dict:
    """从 INFORMATION_SCHEMA 获取所有表信息

    返回: {table_name: {"comment": str, "columns": [...]}}
    """
    tables = {}

    with conn.cursor() as cur:
        cur.execute("""
            SELECT TABLE_NAME, TABLE_COMMENT
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """)
        for row in cur.fetchall():
            tables[row["TABLE_NAME"].lower()] = {
                "comment": row["TABLE_COMMENT"] or "",
                "columns": [],
            }

    with conn.cursor() as cur:
        cur.execute("""
            SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY,
                   COLUMN_COMMENT, IS_NULLABLE, EXTRA
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """)
        for row in cur.fetchall():
            tname = row["TABLE_NAME"].lower()
            if tname in tables:
                tables[tname]["columns"].append({
                    "name": row["COLUMN_NAME"],
                    "type": row["COLUMN_TYPE"],
                    "key": row["COLUMN_KEY"],
                    "comment": row["COLUMN_COMMENT"] or "",
                    "nullable": row["IS_NULLABLE"],
                    "extra": row["EXTRA"] or "",
                })

    return tables


def select_key_columns(columns: list, max_count: int = 6) -> list[str]:
    """选择最重要的列（主键 + 有注释的 + 外键），最多 max_count 个"""
    selected = []
    remaining = []

    for col in columns:
        if col["key"] == "PRI":
            selected.append(col["name"])
        elif col["key"] in ("MUL", "UNI") and col["comment"]:
            selected.append(col["name"])
        elif col["comment"]:
            remaining.append(col["name"])
        elif col["key"] in ("MUL", "UNI"):
            remaining.append(col["name"])

    result = selected + remaining
    return result[:max_count]


# ──────────────────────────────────────────────
# 分表折叠
# ──────────────────────────────────────────────

def collapse_sharded_tables(db_tables: dict) -> tuple[dict, dict]:
    """识别并折叠分表（如 user_main_1402 → user_main）

    分表识别规则：
    - 表名以 _数字 结尾（如 _1402, _50003）
    - 同一个逻辑表名下有 >= 3 个分片
    - 多数派策略：>= 50% 的分片有相同列名列表即可折叠

    返回:
        logical_tables: {逻辑表名: db_tables 中的原始信息（取自多数派分片）}
        shard_info: {逻辑表名: {"count": N, "shards": [分片表名列表]}}
    """
    candidates = defaultdict(list)
    standalone = []

    for tname in sorted(db_tables.keys()):
        match = re.match(r'^(.+?)_(\d+)$', tname)
        if match:
            logical_name = match.group(1)
            candidates[logical_name].append(tname)
        else:
            standalone.append(tname)

    logical_tables = {}
    shard_info = {}

    for logical_name, shards in candidates.items():
        if len(shards) >= 10:
            # 10+ 个 _数字后缀表 → 高置信度分表，无需检查列结构
            representative = shards[0]
            logical_tables[logical_name] = {
                "comment": db_tables[representative]["comment"],
                "columns": db_tables[representative]["columns"],
            }
            shard_info[logical_name] = {
                "count": len(shards),
                "shards": shards,
            }
        elif len(shards) >= 3:
            # 多数派策略：找出现次数最多的列签名
            sig_counts = defaultdict(list)
            for s in shards:
                cols = tuple(c["name"] for c in db_tables[s]["columns"])
                sig_counts[cols].append(s)

            # 取最大的那组
            majority_sig, majority_shards = max(
                sig_counts.items(), key=lambda x: len(x[1])
            )

            # 只要多数派 >= 50% 就折叠全部
            if len(majority_shards) >= len(shards) * 0.5:
                representative = majority_shards[0]
                logical_tables[logical_name] = {
                    "comment": db_tables[representative]["comment"],
                    "columns": db_tables[representative]["columns"],
                }
                shard_info[logical_name] = {
                    "count": len(shards),
                    "shards": shards,
                }
            else:
                # 不满足多数派，保留为独立表
                for s in shards:
                    logical_tables[s] = db_tables[s]
        elif len(shards) == 2:
            # 2 个分片：如果结构一致就折叠
            cols_a = tuple(c["name"] for c in db_tables[shards[0]]["columns"])
            cols_b = tuple(c["name"] for c in db_tables[shards[1]]["columns"])
            if cols_a == cols_b:
                logical_tables[logical_name] = {
                    "comment": db_tables[shards[0]]["comment"],
                    "columns": db_tables[shards[0]]["columns"],
                }
                shard_info[logical_name] = {
                    "count": 2,
                    "shards": shards,
                }
            else:
                for s in shards:
                    logical_tables[s] = db_tables[s]
        else:
            # 单个 _数字 表
            shard = shards[0]
            logical_tables[shard] = db_tables[shard]

    for tname in standalone:
        logical_tables[tname] = db_tables[tname]

    return logical_tables, shard_info


# ──────────────────────────────────────────────
# 表名前缀分组算法
# ──────────────────────────────────────────────

def compute_prefix(table_name: str) -> str:
    """计算表名前缀

    算法:
    1. 检查特殊前缀（如 e_wechat）
    2. 按 _ 分割，尝试 3段/2段/1段
    3. 返回最长可用前缀
    """
    for sp in SPECIAL_PREFIXES:
        if table_name.startswith(sp + "_"):
            return sp

    parts = table_name.split("_")
    if len(parts) >= 4:
        return "_".join(parts[:3])
    if len(parts) >= 3:
        return "_".join(parts[:2])
    if len(parts) >= 2:
        return parts[0]
    return table_name


def group_tables_by_prefix(table_names: list[str]) -> dict:
    """按前缀分组表名

    返回: {prefix: [table_names]}
    """
    # 第一轮：用 compute_prefix 初步分组
    prefix_groups = defaultdict(list)
    for name in sorted(table_names):
        prefix = compute_prefix(name)
        prefix_groups[prefix].append(name)

    # 第二轮：处理过大的组（> MAX_GROUP_SIZE），尝试更长前缀
    final_groups = {}
    for prefix, tables in prefix_groups.items():
        if len(tables) > MAX_GROUP_SIZE:
            sub_groups = _split_large_group(prefix, tables)
            final_groups.update(sub_groups)
        else:
            final_groups[prefix] = tables

    # 第三轮：合并过小的组（< MIN_GROUP_SIZE）到 misc
    misc_tables = []
    result = {}
    for prefix, tables in sorted(final_groups.items()):
        if len(tables) < MIN_GROUP_SIZE:
            misc_tables.extend(tables)
        else:
            result[prefix] = tables

    if misc_tables:
        result["misc"] = misc_tables

    return result


def _split_large_group(prefix: str, tables: list[str]) -> dict:
    """将过大的前缀组用更长前缀拆分"""
    sub_groups = defaultdict(list)
    prefix_len = len(prefix.split("_"))

    for name in tables:
        parts = name.split("_")
        # 检查特殊前缀
        is_special = False
        for sp in SPECIAL_PREFIXES:
            if name.startswith(sp + "_"):
                sp_parts = sp.split("_")
                longer = "_".join(parts[:len(sp_parts) + 1]) if len(parts) > len(sp_parts) else sp
                sub_groups[longer].append(name)
                is_special = True
                break
        if is_special:
            continue

        if len(parts) > prefix_len + 1:
            longer = "_".join(parts[:prefix_len + 1])
        else:
            longer = prefix
        sub_groups[longer].append(name)

    return dict(sub_groups)


# ──────────────────────────────────────────────
# 代码分析（ast-grep + sqlglot）
# ──────────────────────────────────────────────

def run_ast_grep(source_path: str, annotation: str) -> list[dict]:
    """用 ast-grep 提取指定注解的匹配结果"""
    pattern = f"@{annotation}($$$)"
    cmd = [
        "ast-grep",
        "--pattern", pattern,
        "--lang", "java",
        "--json",
        source_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout) if result.stdout.strip() else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def clean_sql(raw_text: str) -> str:
    """清理 Java/MyBatis 语法噪音，提取纯 SQL"""
    text = raw_text
    text = re.sub(r'^@\w+\(\s*\{?\s*', '', text)
    text = re.sub(r'\s*\}?\s*\)\s*$', '', text)
    text = text.replace('"', ' ').replace('+', ' ')
    text = re.sub(r'^\s*,\s*', ' ', text, flags=re.MULTILINE)
    text = re.sub(r'\s*,\s*$', ' ', text, flags=re.MULTILINE)

    # MyBatis XML 标签
    for tag in ['script', 'if', 'choose', 'when', 'otherwise', 'where', 'set', 'trim']:
        text = re.sub(rf'</?{tag}[^>]*>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<!\[CDATA\[', ' ', text)
    text = re.sub(r'\]\]>', ' ', text)
    text = re.sub(
        r'<foreach\s+[^>]*>.*?</foreach>',
        " ('placeholder') ",
        text, flags=re.DOTALL | re.IGNORECASE,
    )

    text = re.sub(r'#\{[^}]*\}', "'placeholder'", text)
    text = re.sub(r'\$\{[^}]*\}', "placeholder", text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_tables_sqlglot(sql: str) -> list[str]:
    """用 sqlglot 从 SQL 中提取表名"""
    try:
        parsed = sql_parse(sql, error_level=ErrorLevel.IGNORE)
        tables = set()
        for statement in parsed:
            if statement is None:
                continue
            for table in statement.find_all(exp.Table):
                name = table.name
                if name and name.lower() not in ("dual", "placeholder", "information_schema"):
                    tables.add(name.lower())
        return sorted(tables)
    except Exception:
        return _extract_tables_regex(sql)


def _extract_tables_regex(sql: str) -> list[str]:
    """降级方案：正则提取表名"""
    pattern = r'(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)'
    matches = re.findall(pattern, sql, re.IGNORECASE)
    skip = {
        "select", "where", "set", "values", "and", "or", "on", "as",
        "left", "right", "inner", "outer", "cross", "full", "group",
        "order", "having", "limit", "offset", "union", "placeholder", "dual",
    }
    return sorted({m.lower() for m in matches if m.lower() not in skip})


def extract_joins_sqlglot(sql: str) -> list[dict]:
    """用 sqlglot 提取 JOIN 关系"""
    joins = []
    try:
        parsed = sql_parse(sql, error_level=ErrorLevel.IGNORE)
        for statement in parsed:
            if statement is None:
                continue
            for join_node in statement.find_all(exp.Join):
                join_table = join_node.find(exp.Table)
                on_cond = join_node.find(exp.EQ)
                if join_table and on_cond:
                    joins.append({
                        "table": join_table.name.lower(),
                        "condition": on_cond.sql(),
                    })
    except Exception:
        pass
    return joins


def _extract_joins_regex(sql: str) -> list[dict]:
    """降级方案：正则提取 JOIN"""
    joins = []
    pattern = r'JOIN\s+(\w+)\s+\w*\s*ON\s+(\w+\.\w+)\s*=\s*(\w+\.\w+)'
    for match in re.finditer(pattern, sql, re.IGNORECASE):
        joins.append({
            "table": match.group(1).lower(),
            "condition": f"{match.group(2)} = {match.group(3)}",
        })
    return joins


def infer_dao_name(file_path: str) -> str:
    """从文件路径提取 DAO 类名"""
    return Path(file_path).stem


def infer_project_label(source_path: str) -> str:
    """从源码路径推断项目标签（查 SOURCE_LABELS 映射表）"""
    abs_path = os.path.abspath(source_path)
    if abs_path in SOURCE_LABELS:
        return SOURCE_LABELS[abs_path]
    return Path(source_path).name


def scan_sources(source_paths: list[str], shard_info: dict | None = None) -> dict:
    """扫描多个项目源码，提取代码信息

    shard_info: 分表信息，用于将代码中的分表名映射回逻辑表名

    返回:
    {
        table_name: {
            "daos": {dao_name: set(projects)},
            "joins": [{"from": t, "to": t, "condition": c, "projects": set()}],
        }
    }
    """
    # 构建分片名→逻辑名的反向映射
    shard_to_logical = {}
    if shard_info:
        for logical_name, info in shard_info.items():
            for shard_name in info["shards"]:
                shard_to_logical[shard_name] = logical_name

    def resolve_table(name: str) -> str:
        """将物理表名解析为逻辑表名"""
        return shard_to_logical.get(name, name)

    table_info = defaultdict(lambda: {
        "daos": defaultdict(set),
        "joins": [],
    })

    for source_path in source_paths:
        source_path = os.path.abspath(source_path)
        if not os.path.isdir(source_path):
            print(f"  警告: 跳过不存在的路径 {source_path}", file=sys.stderr)
            continue

        project_label = infer_project_label(source_path)
        print(f"\n扫描项目: {project_label} ({source_path})")

        for annotation in ANNOTATIONS:
            print(f"  @{annotation}...", end=" ", flush=True)
            matches = run_ast_grep(source_path, annotation)
            print(f"{len(matches)} 条")

            for match in matches:
                file_path = match.get("file", "")
                raw_text = match.get("text", "")
                dao_name = infer_dao_name(file_path)

                sql = clean_sql(raw_text)
                if not sql:
                    continue

                tables = extract_tables_sqlglot(sql)
                if not tables:
                    tables = _extract_tables_regex(sql)
                if not tables:
                    continue

                for t in tables:
                    lt = resolve_table(t)
                    table_info[lt]["daos"][dao_name].add(project_label)

                joins = extract_joins_sqlglot(sql)
                if not joins:
                    joins = _extract_joins_regex(sql)
                for j in joins:
                    main_tables = [t for t in tables if t != j["table"]]
                    from_table = resolve_table(main_tables[0] if main_tables else tables[0])
                    to_table = resolve_table(j["table"])
                    table_info[to_table]["joins"].append({
                        "from": from_table,
                        "to": to_table,
                        "condition": j["condition"],
                        "project": project_label,
                    })
                    table_info[from_table]["joins"].append({
                        "from": from_table,
                        "to": to_table,
                        "condition": j["condition"],
                        "project": project_label,
                    })

    return dict(table_info)


# ──────────────────────────────────────────────
# 业务域推断
# ──────────────────────────────────────────────

# 中文搜索关键词映射（从配置读取，默认空）
# 配置格式: {"user": {"label": "用户", "keywords": ["用户表", "会员"]}}
_raw_domain_hints = config.get("domain_hints", {})

# 转换为内部格式: {prefix: (label, [keywords])}
DOMAIN_HINTS: dict[str, tuple[str, list[str]]] = {}
for prefix, hint in _raw_domain_hints.items():
    if isinstance(hint, dict):
        DOMAIN_HINTS[prefix] = (hint.get("label", prefix), hint.get("keywords", []))
    elif isinstance(hint, (list, tuple)) and len(hint) >= 2:
        DOMAIN_HINTS[prefix] = (hint[0], hint[1])


def infer_domain(prefix: str, db_tables: dict, tables_in_group: list[str]) -> str:
    """推断业务域名称

    优先使用 DOMAIN_HINTS 预设，其次从表注释中提取
    """
    if prefix in DOMAIN_HINTS:
        return DOMAIN_HINTS[prefix][0]

    # 从表注释中提取公共关键词
    comments = []
    for t in tables_in_group:
        if t in db_tables and db_tables[t]["comment"]:
            comments.append(db_tables[t]["comment"])

    if comments:
        # 取第一个非空注释作为参考
        first = comments[0]
        if len(first) <= 20:
            return first
        return first[:20] + "..."

    return prefix.replace("_", " ")


def build_search_hints(prefix_groups: dict, db_tables: dict) -> list[str]:
    """自动生成中文搜索提示"""
    hints = []
    for prefix in sorted(prefix_groups.keys()):
        if prefix == "misc":
            continue

        if prefix in DOMAIN_HINTS:
            synonyms = DOMAIN_HINTS[prefix][1]
            hint_text = "/".join(f'"{s}"' for s in synonyms)
            hints.append(f'- {hint_text} → {prefix}')
        else:
            # 尝试从表注释推断
            tables = prefix_groups[prefix]
            sample_comments = []
            for t in tables[:3]:
                if t in db_tables and db_tables[t]["comment"]:
                    sample_comments.append(db_tables[t]["comment"])
            if sample_comments:
                hint = "/".join(f'"{c}"' for c in sample_comments[:2])
                hints.append(f'- {hint} → {prefix}')

    return hints


# ──────────────────────────────────────────────
# 输出生成
# ──────────────────────────────────────────────

def generate_schema_index(
    prefix_groups: dict,
    db_tables: dict,
    db_name: str,
    source_projects: list[str],
) -> str:
    """生成 schema-index.md"""
    project_labels = ", ".join(infer_project_label(s) for s in source_projects)
    lines = [
        "# 共享数据库 Schema 索引",
        "",
        f"> 数据库: {db_name} | 自动生成: {date.today().isoformat()}",
        f"> 共享项目: {project_labels}",
        "",
        "| 前缀 | 表数 | 业务域 | 关键表说明 |",
        "|------|------|--------|-----------|",
    ]

    for prefix in sorted(prefix_groups.keys()):
        tables = prefix_groups[prefix]
        domain = infer_domain(prefix, db_tables, tables)

        # 关键表说明：取有注释的前 3 张表
        key_descs = []
        for t in tables:
            if t in db_tables and db_tables[t]["comment"]:
                key_descs.append(db_tables[t]["comment"])
            if len(key_descs) >= 3:
                break
        key_desc_str = "、".join(key_descs) if key_descs else "-"

        lines.append(f"| {prefix} | {len(tables)} | {domain} | {key_desc_str} |")

    # 搜索提示
    hints = build_search_hints(prefix_groups, db_tables)
    if hints:
        lines.extend(["", "## 搜索提示", ""])
        lines.extend(hints)

    lines.append("")
    return "\n".join(lines)


def generate_prefix_map(prefix_groups: dict) -> dict:
    """生成 prefix-map.json: {table_name: prefix_file}"""
    mapping = {}
    for prefix, tables in prefix_groups.items():
        for t in tables:
            mapping[t] = prefix
    return mapping


def generate_prefix_schema(
    prefix: str,
    tables_in_group: list[str],
    db_tables: dict,
    code_info: dict,
    all_prefix_groups: dict,
    shard_info: dict | None = None,
) -> str:
    """生成 schema/{prefix}.md"""
    domain = infer_domain(prefix, db_tables, tables_in_group)
    lines = [
        f"# {prefix} 系列表 ({domain})",
        "",
        f"> 共 {len(tables_in_group)} 张逻辑表 | 自动生成: {date.today().isoformat()}",
        "",
        "## 表清单",
        "",
        "| 表名 | 注释 | 分表 | 关键列 |",
        "|------|------|------|--------|",
    ]

    shard_info = shard_info or {}

    for t in sorted(tables_in_group):
        comment = ""
        key_cols_str = ""
        if t in db_tables:
            comment = db_tables[t]["comment"]
            key_cols = select_key_columns(db_tables[t]["columns"])
            key_cols_str = ", ".join(key_cols)

        shard_note = ""
        if t in shard_info:
            shard_note = f"x{shard_info[t]['count']}"

        lines.append(f"| {t} | {comment} | {shard_note} | {key_cols_str} |")

    # JOIN 关系
    # 构建表到前缀的映射
    table_to_prefix = {}
    for p, tlist in all_prefix_groups.items():
        for tname in tlist:
            table_to_prefix[tname] = p

    current_tables = set(tables_in_group)
    joins = []
    seen_joins = set()

    for t in tables_in_group:
        if t not in code_info:
            continue
        for j in code_info[t].get("joins", []):
            key = tuple(sorted([j["from"], j["to"]])) + (j["condition"],)
            if key in seen_joins:
                continue
            seen_joins.add(key)

            # 只显示至少一端在当前组的 JOIN
            if j["from"] in current_tables or j["to"] in current_tables:
                joins.append(j)

    if joins:
        lines.extend(["", "## JOIN 关系", ""])
        lines.append("| 源表 | 目标表 | 关联条件 | 涉及项目 |")
        lines.append("|------|--------|---------|----------|")

        for j in joins:
            from_t = j["from"]
            to_t = j["to"]
            cond = j["condition"]
            project = j.get("project", "")

            cross_note = ""
            if to_t not in current_tables:
                target_prefix = table_to_prefix.get(to_t, "?")
                cross_note = f" [跨前缀 → {target_prefix}]"
            elif from_t not in current_tables:
                target_prefix = table_to_prefix.get(from_t, "?")
                cross_note = f" [跨前缀 → {target_prefix}]"

            lines.append(f"| {from_t} | {to_t}{cross_note} | {cond} | {project} |")

    # DAO 映射
    dao_entries = []
    for t in tables_in_group:
        if t not in code_info:
            continue
        for dao_name, projects in code_info[t]["daos"].items():
            dao_entries.append({
                "dao": dao_name,
                "projects": sorted(projects),
                "table": t,
            })

    if dao_entries:
        # 按 DAO 合并操作表
        dao_map = defaultdict(lambda: {"projects": set(), "tables": set()})
        for entry in dao_entries:
            dao_map[entry["dao"]]["projects"].update(entry["projects"])
            dao_map[entry["dao"]]["tables"].add(entry["table"])

        lines.extend(["", "## DAO 映射", ""])
        lines.append("| DAO | 来源项目 | 操作表 |")
        lines.append("|-----|---------|--------|")

        for dao_name in sorted(dao_map.keys()):
            info = dao_map[dao_name]
            proj_str = ", ".join(sorted(info["projects"]))
            tables_str = ", ".join(sorted(info["tables"]))
            lines.append(f"| {dao_name} | {proj_str} | {tables_str} |")

    lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 文件写入
# ──────────────────────────────────────────────

def write_output(
    prefix_groups: dict,
    db_tables: dict,
    code_info: dict,
    db_name: str,
    source_projects: list[str],
    project_name: str,
    dry_run: bool,
    shard_info: dict | None = None,
):
    """生成并写入所有文件"""
    project_dir = MEMORY_BASE / project_name
    schema_dir = project_dir / "schema"

    # 1. schema-index.md
    index_content = generate_schema_index(
        prefix_groups, db_tables, db_name, source_projects
    )

    # 2. prefix-map.json
    prefix_map = generate_prefix_map(prefix_groups)

    # 3. 各前缀的 schema 文件
    prefix_files = {}
    for prefix in sorted(prefix_groups.keys()):
        content = generate_prefix_schema(
            prefix, prefix_groups[prefix], db_tables, code_info, prefix_groups,
            shard_info=shard_info,
        )
        prefix_files[prefix] = content

    if dry_run:
        print(f"\n{'=' * 60}")
        print("schema-index.md 预览:")
        print("=" * 60)
        print(index_content)

        print(f"\n{'=' * 60}")
        print(f"prefix-map.json: {len(prefix_map)} 张表映射")
        print("=" * 60)

        for prefix, content in prefix_files.items():
            line_count = content.count('\n')
            print(f"\n{'=' * 60}")
            print(f"schema/{prefix}.md ({line_count} 行) 预览:")
            print("=" * 60)
            # dry-run 只显示前 30 行
            preview_lines = content.split('\n')[:30]
            print('\n'.join(preview_lines))
            if line_count > 30:
                print(f"  ... 还有 {line_count - 30} 行 ...")

        print(f"\n提示: 去掉 --dry-run 可写入文件到 {project_dir}/")
    else:
        project_dir.mkdir(parents=True, exist_ok=True)
        schema_dir.mkdir(parents=True, exist_ok=True)

        # 清理旧的 schema 文件
        for old_file in schema_dir.glob("*.md"):
            old_file.unlink()
            print(f"  删除旧文件: {old_file.name}")

        index_path = project_dir / "schema-index.md"
        index_path.write_text(index_content, encoding="utf-8")
        print(f"  写入: {index_path}")

        map_path = project_dir / "prefix-map.json"
        map_path.write_text(
            json.dumps(prefix_map, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  写入: {map_path}")

        for prefix, content in prefix_files.items():
            fpath = schema_dir / f"{prefix}.md"
            fpath.write_text(content, encoding="utf-8")
            line_count = content.count('\n')
            print(f"  写入: schema/{prefix}.md ({line_count} 行)")


def print_summary(
    prefix_groups: dict,
    db_tables: dict,
    code_info: dict,
    total_physical_tables: int = 0,
    shard_info: dict | None = None,
):
    """打印统计摘要"""
    total_logical_tables = len(db_tables)
    total_grouped = sum(len(tables) for tables in prefix_groups.values())
    total_prefixes = len(prefix_groups)
    misc_count = len(prefix_groups.get("misc", []))
    total_sharded = len(shard_info) if shard_info else 0

    total_joins = 0
    total_daos = set()
    for t, info in code_info.items():
        total_joins += len(info.get("joins", []))
        total_daos.update(info["daos"].keys())

    print(f"\n{'=' * 60}")
    print("统计摘要:")
    if total_physical_tables:
        print(f"  物理表总数:    {total_physical_tables}")
    print(f"  逻辑表总数:    {total_logical_tables}")
    if total_sharded:
        print(f"  分表数:        {total_sharded} 张（已折叠为逻辑表）")
    print(f"  分组后表数:    {total_grouped}")
    print(f"  前缀组数:      {total_prefixes}")
    print(f"  misc 组表数:   {misc_count}")
    print(f"  代码 JOIN 关系: {total_joins}")
    print(f"  代码 DAO 文件:  {len(total_daos)}")

    if total_logical_tables != total_grouped:
        diff = total_logical_tables - total_grouped
        print(f"  ⚠ 差异: DB 比代码多 {diff} 张表（正常，代码未覆盖所有表）")

    # 文件大小预估
    large_groups = [
        (p, len(ts)) for p, ts in prefix_groups.items() if len(ts) > 40
    ]
    if large_groups:
        print(f"\n  ⚠ 以下前缀组偏大（> 40 张表）:")
        for p, count in large_groups:
            print(f"    {p}: {count} 张表")

    print("=" * 60)


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="统一数据库 Schema 管理 v2"
    )
    parser.add_argument("--db-host", default="127.0.0.1", help="MySQL 主机")
    parser.add_argument("--db-port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--db-user", default="root", help="MySQL 用户")
    parser.add_argument("--db-password", default="", help="MySQL 密码")
    parser.add_argument("--db-name", required=True, help="数据库名")
    parser.add_argument(
        "--sources", required=True,
        help="项目源码路径，逗号分隔（如 /path/a,/path/b,/path/c）",
    )
    parser.add_argument(
        "--source-labels",
        default=None,
        help="源码项目标签，与 --sources 一一对应，逗号分隔（如 api,worker,agent）",
    )
    parser.add_argument(
        "--project", default="shared-db",
        help="记忆系统中的项目名（默认 shared-db）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只预览输出，不写文件",
    )
    args = parser.parse_args()

    source_paths = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not source_paths:
        print("错误: --sources 不能为空", file=sys.stderr)
        sys.exit(1)

    # 处理 --source-labels CLI 参数（优先于 config.json）
    if args.source_labels:
        labels = [l.strip() for l in args.source_labels.split(",") if l.strip()]
        if len(labels) == len(source_paths):
            for path, label in zip(source_paths, labels):
                SOURCE_LABELS[os.path.abspath(path)] = label
        else:
            print(f"警告: --source-labels 数量 ({len(labels)}) 与 --sources ({len(source_paths)}) 不匹配，忽略", file=sys.stderr)

    # 检查 ast-grep
    try:
        subprocess.run(
            ["ast-grep", "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        print("错误: 未安装 ast-grep: brew install ast-grep", file=sys.stderr)
        sys.exit(1)

    print(f"数据库: {args.db_name}@{args.db_host}:{args.db_port}")
    print(f"项目: {args.project}")
    print(f"源码: {', '.join(source_paths)}")
    print(f"模式: {'预览' if args.dry_run else '写入'}")

    # 1. 连接 MySQL，获取表结构
    print(f"\n连接 MySQL...")
    try:
        conn = pymysql.connect(
            host=args.db_host,
            port=args.db_port,
            user=args.db_user,
            password=args.db_password,
            database=args.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.Error as e:
        print(f"错误: MySQL 连接失败: {e}", file=sys.stderr)
        sys.exit(1)

    print("  获取表结构...")
    db_tables_raw = fetch_db_tables(conn)
    total_physical = len(db_tables_raw)
    print(f"  获取到 {total_physical} 张物理表")
    conn.close()

    # 2. 分表折叠
    print(f"\n折叠分表...")
    db_tables, shard_info = collapse_sharded_tables(db_tables_raw)
    print(f"  折叠后 {len(db_tables)} 张逻辑表（{len(shard_info)} 张分表被合并）")

    # 3. 按前缀分组
    print(f"\n按前缀分组...")
    prefix_groups = group_tables_by_prefix(list(db_tables.keys()))
    for prefix in sorted(prefix_groups.keys()):
        print(f"  {prefix}: {len(prefix_groups[prefix])} 张表")

    # 4. 扫描代码提取 JOIN/DAO 信息
    print(f"\n扫描代码...")
    code_info = scan_sources(source_paths, shard_info=shard_info)
    print(f"\n  代码中涉及 {len(code_info)} 张表")

    # 5. 统计摘要
    print_summary(
        prefix_groups, db_tables, code_info,
        total_physical_tables=total_physical,
        shard_info=shard_info,
    )

    # 6. 生成输出
    write_output(
        prefix_groups, db_tables, code_info,
        args.db_name, source_paths, args.project, args.dry_run,
        shard_info=shard_info,
    )


if __name__ == "__main__":
    main()
