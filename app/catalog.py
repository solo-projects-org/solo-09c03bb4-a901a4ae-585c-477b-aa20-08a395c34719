"""目录解析、静态校验与元数据构建。

目录采用 SQLite 方言的 SELECT 语句。为便于依赖分析，单个视图只允许一条
SELECT（不支持 CTE / 子查询 / 集合运算），视图之间可以按名称互相引用。
字段引用必须写成 ``别名.字段``；任何一处错误都会使整份目录被拒绝，调用方
不得提交，因此目录切换是「全有或全无」的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DIALECT = "sqlite"

# 字段出现在这些子句时，会影响视图的全部输出（而不仅是某个投影列）。
# 其中 JOIN / WHERE 是硬性要求；GROUP BY / HAVING / ORDER BY 同样会让
# 整个视图在字段停用后失效，因此一并按整体影响处理。
FANOUT_CLAUSES = frozenset({"JOIN", "WHERE", "GROUP BY", "HAVING", "ORDER BY"})
PROJECTION_CLAUSE = "SELECT"

_VIEW_NAME_RE = re.compile(r"^\S(?:.*\S)?$")


class CatalogError(Exception):
    """整份目录校验失败。errors 为逐条中文错误。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def norm(value: str) -> str:
    """SQLite 标识符大小写不敏感；统一 casefold 做匹配。"""
    return value.strip().casefold()


@dataclass(frozen=True)
class FieldRef:
    alias_key: str
    column_key: str
    column: str
    clause: str
    expression: str = ""
    projection_index: int | None = None


@dataclass
class Projection:
    index: int
    name: str
    sql: str
    refs: list[FieldRef] = field(default_factory=list)


@dataclass
class Source:
    alias_key: str
    alias: str
    kind: str          # "table" | "view"
    name: str          # 目录中的原始名称
    name_key: str


@dataclass
class ViewDef:
    name: str
    name_key: str
    sql: str
    canonical_sql: str
    sources: dict[str, Source]
    projections: list[Projection]
    output_names: list[str]
    filter_refs: list[FieldRef]
    dependencies: list[str]          # 直接依赖的视图名（原始大小写）
    dependency_keys: list[str]

    def fields_used_by_output(self, output_name: str) -> list[FieldRef]:
        key = norm(output_name)
        for projection in self.projections:
            if norm(projection.name) == key:
                return projection.refs
        return []


@dataclass
class Catalog:
    raw_json: str
    base_tables: dict[str, list[str]]                 # 原始名称 -> 字段
    base_index: dict[str, str]                        # casefold -> 原始名称
    views: dict[str, ViewDef]                         # 原始名称 -> 定义
    view_index: dict[str, str]                        # casefold -> 原始名称
    view_order: list[str]                             # 目录声明顺序
    topological_order: list[str]                      # 依赖优先
    dependents: dict[str, list[str]]                  # 视图 -> 直接下游视图

    @property
    def base_count(self) -> int:
        return len(self.base_tables)

    @property
    def view_count(self) -> int:
        return len(self.views)

    def get_view(self, name: str) -> ViewDef | None:
        return self.views.get(self.view_index.get(norm(name), ""))

    def summary(self) -> dict[str, Any]:
        return {
            "base_tables": {name: list(cols) for name, cols in self.base_tables.items()},
            "views": [
                {
                    "name": view.name,
                    "outputs": list(view.output_names),
                    "depends_on": list(view.dependencies),
                }
                for view in (self.views[name] for name in self.view_order)
            ],
        }


# ---------------------------------------------------------------------------
# JSON 目录结构解析
# ---------------------------------------------------------------------------


def _load_json(raw_json: str) -> Any:
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            [f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"]
        ) from exc


def _column_names(raw_columns: Any) -> list[str] | None:
    if not isinstance(raw_columns, list):
        return None
    result: list[str] = []
    for item in raw_columns:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            result.append(item["name"])
        else:
            return None
    return result


def _normalise_base_tables(value: Any, errors: list[str]) -> dict[str, list[str]]:
    """接受对象或数组两种写法：
    {"orders": ["id", ...]} / {"orders": {"columns": [...]}} /
    [{"name": "orders", "columns": [...]}]
    """
    base_tables: dict[str, list[str]] = {}
    if value is None:
        value = {}
    elif not isinstance(value, (dict, list)):
        errors.append("base_tables 必须是对象（表名 -> 字段数组）或数组")
        return base_tables

    items: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        items = list(value.items())
    else:
        for idx, entry in enumerate(value):
            if not isinstance(entry, dict):
                errors.append(f"base_tables[{idx}] 必须是对象")
                continue
            name = entry.get("name", entry.get("table"))
            cols = entry.get("columns", entry.get("fields", []))
            items.append((name, cols))

    seen: set[str] = set()
    for raw_name, raw_columns in items:
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors.append(f"基础表名非法：{raw_name!r}")
            continue
        name = raw_name.strip()
        key = norm(name)
        if key in seen:
            errors.append(f"基础表重名：{name}")
            continue
        seen.add(key)

        if isinstance(raw_columns, dict):
            raw_columns = raw_columns.get("columns", raw_columns.get("fields"))
        columns = _column_names(raw_columns)
        if columns is None:
            errors.append(f"基础表 {name} 的 columns 必须是字段名数组")
            continue

        cleaned: list[str] = []
        col_seen: set[str] = set()
        for raw_col in columns:
            col = raw_col.strip()
            if not col:
                errors.append(f"基础表 {name} 存在空字段名")
                continue
            col_key = norm(col)
            if col_key in col_seen:
                errors.append(f"基础表 {name} 字段重名：{col}")
                continue
            col_seen.add(col_key)
            cleaned.append(col)
        if not cleaned:
            errors.append(f"基础表 {name} 至少需要一个字段")
            continue
        base_tables[name] = cleaned
    return base_tables


def _normalise_view_entries(value: Any, errors: list[str]) -> list[tuple[str, str]]:
    """接受 [{"name": ..., "sql": ...}] 或 {"name": "SQL"} 等写法。"""
    if value is None:
        value = []
    entries: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        for name, body in value.items():
            if isinstance(body, dict):
                body = body.get("sql", body.get("select", body.get("query", body.get("definition"))))
            entries.append((name, body))
    elif isinstance(value, list):
        for idx, entry in enumerate(value):
            if not isinstance(entry, dict):
                errors.append(f"views[{idx}] 必须是对象")
                continue
            name = entry.get("name", entry.get("view"))
            sql = entry.get("sql", entry.get("select", entry.get("query", entry.get("definition"))))
            entries.append((name, sql))
    else:
        errors.append("views 必须是数组或对象（视图名 -> SELECT 定义）")
        return []

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_name, raw_sql in entries:
        if not isinstance(raw_name, str) or not _VIEW_NAME_RE.match(raw_name.strip()):
            errors.append(f"视图名非法：{raw_name!r}")
            continue
        name = raw_name.strip()
        key = norm(name)
        if key in seen:
            errors.append(f"视图重名：{name}")
            continue
        if not isinstance(raw_sql, str) or not raw_sql.strip():
            errors.append(f"视图 {name} 缺少 SELECT 定义")
            continue
        seen.add(key)
        result.append((name, raw_sql))
    return result


# ---------------------------------------------------------------------------
# SQL 结构与字段引用校验
# ---------------------------------------------------------------------------


def _parse_view_sql(view_name: str, sql: str, errors: list[str]) -> exp.Select | None:
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except ParseError as exc:
        errors.append(f"视图 {view_name} SQL 无法解析：{exc}")
        return None
    statements = [stmt for stmt in statements if stmt is not None]
    if len(statements) != 1:
        errors.append(f"视图 {view_name} 必须且只能包含一条 SELECT 语句")
        return None
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        errors.append(
            f"视图 {view_name} 必须是单条 SELECT 查询（不支持 "
            f"{type(tree).__name__} / 集合运算）"
        )
        return None
    if tree.args.get("with_") is not None:
        errors.append(f"视图 {view_name} 不支持 CTE（WITH），请拆分为独立视图")
        return None
    if next(tree.find_all(exp.Subquery), None) is not None:
        errors.append(f"视图 {view_name} 不支持子查询，请拆分为独立视图")
        return None
    if next(tree.find_all(exp.SetOperation), None) is not None:
        errors.append(f"视图 {view_name} 不支持集合运算（UNION 等）")
        return None
    return tree


def _source_nodes(tree: exp.Select) -> list[exp.Expression]:
    from_clause = tree.args.get("from_")
    nodes: list[exp.Expression] = []
    if from_clause is not None:
        # sqlglot 的 From.this 保存第一个来源，From.expressions 保存其余来源。
        if from_clause.this is not None:
            nodes.append(from_clause.this)
        nodes.extend(from_clause.expressions or [])
    for join in tree.args.get("joins") or []:
        nodes.append(join.this)
    return nodes


def _source_entries(
    view_name: str, tree: exp.Select, errors: list[str]
) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    for node in _source_nodes(tree):
        if isinstance(node, exp.Lateral):
            errors.append(f"视图 {view_name} 不支持 LATERAL 来源")
            continue
        if not isinstance(node, exp.Table):
            errors.append(
                f"视图 {view_name} 的来源必须是物理表或视图（不支持 "
                f"{type(node).__name__}，请拆分为独立视图）"
            )
            continue
        if node.args.get("db") is not None or node.args.get("catalog") is not None:
            errors.append(
                f"视图 {view_name} 的表 {node.sql()} 不能带库名，请直接使用目录中的表名"
            )
            continue
        table_name = node.name
        alias = node.alias or table_name
        alias_key = norm(alias)
        if not alias:
            errors.append(f"视图 {view_name} 存在无名称来源")
            continue
        if alias_key in sources:
            errors.append(
                f"视图 {view_name} 来源别名重复：{alias}（JOIN 同一对象请使用不同别名）"
            )
            continue
        sources[alias_key] = Source(
            alias_key=alias_key,
            alias=alias,
            kind="table",          # 实际种类在依赖解析阶段确定
            name=table_name,
            name_key=norm(table_name),
        )
    return sources


def _clause_info(column: exp.Column, root: exp.Select) -> tuple[str | None, str]:
    """返回 (子句, 该子句的 SQL 片段)。

    只有挂在该 SELECT 根节点上的 WHERE / JOIN / GROUP BY / HAVING / ORDER BY
    才算谓词子句；窗口函数内部的 PARTITION BY / ORDER BY 属于投影表达式的一
    部分，停用其字段只会让该输出列失效，不会扩散到整个视图。
    """
    node: exp.Expression | None = column
    while node is not None and node is not root:
        parent = node.parent
        attached_to_root = parent is root
        if attached_to_root and isinstance(node, exp.Join):
            on = node.args.get("on")
            return "JOIN", on.sql(dialect=DIALECT) if on is not None else node.sql(dialect=DIALECT)
        if attached_to_root and isinstance(node, exp.Where):
            return "WHERE", node.this.sql(dialect=DIALECT)
        if attached_to_root and isinstance(node, exp.Group):
            return "GROUP BY", node.sql(dialect=DIALECT)
        if attached_to_root and isinstance(node, exp.Having):
            return "HAVING", node.this.sql(dialect=DIALECT)
        if attached_to_root and isinstance(node, (exp.Order, exp.Sort)):
            return "ORDER BY", node.sql(dialect=DIALECT)
        if isinstance(node, (exp.Select, exp.Subquery, exp.SetOperation)) and node is not root:
            return None, ""
        node = parent
    return PROJECTION_CLAUSE, ""


def _projection_name(projection: exp.Expression) -> str | None:
    if isinstance(projection, exp.Alias):
        name = projection.alias
        return name if name else None
    if isinstance(projection, exp.Column):
        return projection.name
    # 无别名的表达式沿用 SQL 文本作为稳定输出名（SQLite 行为）。
    return projection.sql(dialect=DIALECT)


def _projection_owner(column: exp.Column, tree: exp.Select) -> int | None:
    owner: exp.Expression | None = column
    while owner is not None and owner is not tree:
        if owner.parent is tree:
            try:
                return tree.expressions.index(owner)
            except ValueError:
                return None
        owner = owner.parent
    return None


def _collect_view(
    view_name: str,
    tree: exp.Select,
    raw_sql: str,
    base_index: dict[str, str],
    view_index: dict[str, str],
    errors: list[str],
) -> ViewDef | None:
    before = len(errors)
    sources = _source_entries(view_name, tree, errors)

    for join in tree.args.get("joins") or []:
        if join.args.get("using") is not None:
            errors.append(f"视图 {view_name} 的 JOIN 不支持 USING，请改为 ON 并带表别名")
        if join.args.get("method") == "NATURAL":
            errors.append(f"视图 {view_name} 不支持 NATURAL JOIN，请改为 ON 并带表别名")

    projections: list[Projection] = []
    output_seen: dict[str, str] = {}
    for index, item in enumerate(tree.expressions):
        if isinstance(item, exp.Star) or (
            isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
        ):
            errors.append(f"视图 {view_name} 必须显式列出输出字段，不支持 * 通配投影")
            continue
        name = _projection_name(item)
        if name is None or not str(name).strip():
            errors.append(f"视图 {view_name} 第 {index + 1} 个投影缺少输出列名")
            continue
        name = str(name).strip()
        if norm(name) in output_seen:
            errors.append(
                f"视图 {view_name} 输出列重名：{name} 与 {output_seen[norm(name)]}"
            )
            continue
        output_seen[norm(name)] = name
        projections.append(Projection(index=index, name=name, sql=item.sql(dialect=DIALECT)))

    if len(projections) != len(tree.expressions):
        return None

    filter_refs: list[FieldRef] = []
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        if column.args.get("catalog") is not None:
            errors.append(
                f"视图 {view_name} 的字段引用 {column.sql()} 只能使用表别名限定"
            )
            continue
        clause, context_sql = _clause_info(column, tree)
        if clause is None:
            errors.append(f"视图 {view_name} 的字段引用 {column.sql()} 不在支持的子句中")
            continue

        alias = column.table
        if not alias:
            # GROUP BY / HAVING / ORDER BY 允许直接引用投影输出别名。
            if clause in ("GROUP BY", "HAVING", "ORDER BY") and norm(column.name) in output_seen:
                continue
            errors.append(f"视图 {view_name} 的字段引用 {column.sql()} 缺少表别名（{clause}）")
            continue

        alias_key = norm(alias)
        if alias_key not in sources:
            errors.append(
                f"视图 {view_name} 引用了未知表别名 {alias!r}（字段 {column.sql()}）"
            )
            continue
        if column.args.get("db") is not None:
            errors.append(
                f"视图 {view_name} 的字段引用 {column.sql()} 只能使用表别名限定，不能带库名"
            )
            continue

        if clause == PROJECTION_CLAUSE:
            projection_index = _projection_owner(column, tree)
            projection = None
            if projection_index is not None and projection_index < len(projections):
                candidate = projections[projection_index]
                if candidate.index == projection_index:
                    projection = candidate
            if projection is None:
                errors.append(
                    f"视图 {view_name} 无法归属字段引用 {column.sql()} 的投影项"
                )
                continue
            ref = FieldRef(
                alias_key=alias_key,
                column_key=norm(column.name),
                column=column.name,
                clause=clause,
                expression=projection.sql,
                projection_index=projection.index,
            )
            projection.refs.append(ref)
        else:
            filter_refs.append(
                FieldRef(
                    alias_key=alias_key,
                    column_key=norm(column.name),
                    column=column.name,
                    clause=clause,
                    expression=context_sql,
                )
            )

    # 依赖解析（基础表 / 视图 / 不存在）
    dependency_keys: list[str] = []
    dependency_names: list[str] = []
    for source in sources.values():
        if source.name_key in base_index and source.name_key in view_index:
            errors.append(f"视图 {view_name} 的来源 {source.name} 与基础表/视图名称冲突")
            continue
        if source.name_key in base_index:
            source.kind = "table"
            source.name = base_index[source.name_key]
        elif source.name_key in view_index:
            source.kind = "view"
            source.name = view_index[source.name_key]
            if source.name_key not in dependency_keys:
                dependency_keys.append(source.name_key)
                dependency_names.append(source.name)
        else:
            errors.append(f"视图 {view_name} 引用了不存在的表或视图 {source.name!r}")

    if len(errors) != before:
        return None

    return ViewDef(
        name=view_name,
        name_key=norm(view_name),
        sql=raw_sql,
        canonical_sql=tree.sql(dialect=DIALECT),
        sources=sources,
        projections=projections,
        output_names=[projection.name for projection in projections],
        filter_refs=filter_refs,
        dependencies=dependency_names,
        dependency_keys=dependency_keys,
    )


def _topological_sort(
    view_order: list[str],
    views: dict[str, ViewDef],
    errors: list[str],
) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    order: list[str] = []
    reported: set[tuple[str, ...]] = set()
    by_key = {view.name_key: view.name for view in views.values()}

    def visit(name: str) -> None:
        state[name] = 1
        stack.append(name)
        for dep_key in views[name].dependency_keys:
            # dependency_keys 为 casefold 键，需映射回原始视图名。
            dep_name = by_key.get(dep_key)
            if dep_name is None or dep_name not in views:
                continue
            dep_state = state.get(dep_name, 0)
            if dep_state == 0:
                visit(dep_name)
            elif dep_state == 1:
                start = stack.index(dep_name)
                cycle = tuple(stack[start:] + [dep_name])
                if cycle not in reported:
                    reported.add(cycle)
                    errors.append("视图存在循环引用：" + " → ".join(cycle))
        stack.pop()
        state[name] = 2
        if name not in order:
            order.append(name)

    for name in view_order:
        if state.get(name, 0) == 0:
            visit(name)
    return order


def _validate_columns(
    views: dict[str, ViewDef],
    topo_keys: list[str],
    base_tables: dict[str, list[str]],
    errors: list[str],
) -> None:
    base_columns = {
        norm(name): {norm(col): col for col in cols}
        for name, cols in base_tables.items()
    }
    view_by_key = {view.name_key: view for view in views.values()}
    for key in topo_keys:
        view = view_by_key[key]
        available_by_alias: dict[str, dict[str, str]] = {}
        for alias_key, source in view.sources.items():
            if source.kind == "table":
                available_by_alias[alias_key] = base_columns.get(source.name_key, {})
            else:
                upstream = view_by_key.get(source.name_key)
                available_by_alias[alias_key] = (
                    {norm(name): name for name in upstream.output_names} if upstream else {}
                )
        refs: list[FieldRef] = list(view.filter_refs)
        for projection in view.projections:
            refs.extend(projection.refs)
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            pair = (ref.alias_key, ref.column_key)
            if pair in seen:
                continue
            seen.add(pair)
            source = view.sources.get(ref.alias_key)
            if source is None:
                continue  # 已在结构校验中报告
            if ref.column_key not in available_by_alias.get(ref.alias_key, {}):
                kind = "基础表" if source.kind == "table" else "视图"
                errors.append(
                    f"视图 {view.name} 引用的字段 {source.name}.{ref.column} 不存在"
                    f"（来自{kind}，别名 {source.alias}）"
                )


def compile_catalog(raw_json: str) -> Catalog:
    """解析并校验整份目录。失败抛出 CatalogError（不会返回部分结果）。"""
    payload = _load_json(raw_json)
    if not isinstance(payload, dict):
        raise CatalogError(["目录根节点必须是 JSON 对象"])

    errors: list[str] = []
    base_value = payload.get("base_tables")
    if base_value is None:
        base_value = payload.get("tables", payload.get("baseTables"))
    base_tables = _normalise_base_tables(base_value, errors)
    view_entries = _normalise_view_entries(payload.get("views"), errors)
    if errors:
        raise CatalogError(errors)

    if not base_tables and not view_entries:
        raise CatalogError(["目录为空：至少需要一个基础表或视图"])

    base_index = {norm(name): name for name in base_tables}
    view_index = {norm(name): name for name, _ in view_entries}
    for key in base_index:
        if key in view_index:
            errors.append(f"基础表与视图重名：{base_index[key]}")
    if errors:
        raise CatalogError(errors)

    parsed: dict[str, tuple[exp.Select, str]] = {}
    for name, sql in view_entries:
        tree = _parse_view_sql(name, sql, errors)
        if tree is not None:
            parsed[name] = (tree, sql)
    if errors:
        raise CatalogError(errors)

    views: dict[str, ViewDef] = {}
    for name, sql in view_entries:
        tree, raw = parsed[name]
        view = _collect_view(name, tree, raw, base_index, view_index, errors)
        if view is not None:
            views[name] = view
    if errors:
        raise CatalogError(errors)

    view_order = [name for name, _ in view_entries]
    topo = _topological_sort(view_order, views, errors)
    if errors:
        raise CatalogError(errors)

    _validate_columns(views, [norm(name) for name in topo], base_tables, errors)
    if errors:
        raise CatalogError(errors)

    dependents: dict[str, list[str]] = {name: [] for name in view_order}
    for name in view_order:
        for dep in views[name].dependencies:
            dependents.setdefault(dep, []).append(name)

    return Catalog(
        raw_json=raw_json,
        base_tables=base_tables,
        base_index=base_index,
        views=views,
        view_index=view_index,
        view_order=view_order,
        topological_order=topo,
        dependents=dependents,
    )
