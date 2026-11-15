"""字段停用影响分析：沿视图血缘计算受影响视图与输出列。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import (
    FANOUT_CLAUSES,
    PROJECTION_CLAUSE,
    Catalog,
    FieldRef,
    Projection,
    ViewDef,
    norm,
)


def _field_node(table: str, column: str) -> dict[str, Any]:
    return {
        "kind": "field",
        "label": f"{table}.{column}",
        "table": table,
        "column": column,
    }


def _view_node(
    view: ViewDef,
    output: str,
    clause: str,
    expression: str,
    full: bool,
) -> dict[str, Any]:
    return {
        "kind": "view",
        "label": f"{view.name}.{output}",
        "view": view.name,
        "output": output,
        "clause": clause,
        "expression": expression,
        "full": full,
    }


def _cause(
    kind: str,
    clause: str,
    expression: str,
    via_view: str | None = None,
    via_output: str | None = None,
) -> dict[str, Any]:
    return {
        "type": kind,
        "clause": clause,
        "expression": expression,
        "via_view": via_view,
        "via_output": via_output,
    }


def _cause_key(cause: dict[str, Any]) -> tuple[Any, ...]:
    return (
        cause["type"],
        cause["clause"],
        cause["expression"],
        cause["via_view"],
        cause["via_output"],
    )


def _path_rank(path: list[dict[str, Any]], full: bool) -> tuple[int, int, int]:
    """路径更短优先；整视图影响时优先展示 JOIN/WHERE 等扩散节点。"""
    terminal = path[-1] if path else {}
    full_penalty = 0 if bool(terminal.get("full")) == full else 1
    clause_penalty = 0 if terminal.get("clause") != PROJECTION_CLAUSE else 1
    return (len(path), full_penalty, clause_penalty)


@dataclass
class _OutputCandidates:
    name: str
    candidates: list[tuple[list[dict[str, Any]], dict[str, Any]]] = field(default_factory=list)

    def add(self, path: list[dict[str, Any]], cause: dict[str, Any]) -> None:
        key = _cause_key(cause)
        for existing_path, existing in self.candidates:
            if _cause_key(existing) == key and existing_path == path:
                return
        self.candidates.append((path, cause))


@dataclass
class _FullCause:
    prefix: list[dict[str, Any]]
    clause: str
    expression: str
    cause: dict[str, Any]


@dataclass
class _ViewImpact:
    view: ViewDef
    full: bool = False
    outputs: dict[str, _OutputCandidates] = field(default_factory=dict)
    full_causes: list[_FullCause] = field(default_factory=list)

    def ensure_output(self, output: str) -> _OutputCandidates:
        key = norm(output)
        if key not in self.outputs:
            self.outputs[key] = _OutputCandidates(name=output)
        return self.outputs[key]

    def add_full(self, prefix: list[dict[str, Any]], clause: str,
                 expression: str, cause: dict[str, Any]) -> None:
        key = (_cause_key(cause), tuple(node.get("label") for node in prefix), clause, expression)
        for existing in self.full_causes:
            existing_key = (
                _cause_key(existing.cause),
                tuple(node.get("label") for node in existing.prefix),
                existing.clause,
                existing.expression,
            )
            if existing_key == key:
                return
        self.full = True
        self.full_causes.append(
            _FullCause(prefix=prefix, clause=clause, expression=expression, cause=cause)
        )


def _projection_refs(projection: Projection, alias_key: str, column_key: str) -> list[FieldRef]:
    seen: set[tuple[Any, ...]] = set()
    result: list[FieldRef] = []
    for ref in projection.refs:
        if ref.alias_key != alias_key or ref.column_key != column_key:
            continue
        key = (ref.alias_key, ref.column_key, ref.clause, ref.expression)
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def _filter_refs(view: ViewDef, alias_key: str, column_keys: set[str]) -> list[FieldRef]:
    seen: set[tuple[Any, ...]] = set()
    result: list[FieldRef] = []
    for ref in view.filter_refs:
        if ref.alias_key != alias_key or ref.column_key not in column_keys:
            continue
        if ref.clause not in FANOUT_CLAUSES:
            continue
        key = (ref.alias_key, ref.column_key, ref.clause, ref.expression)
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def analyze_impact(catalog: Catalog, table: str, column: str) -> dict[str, Any]:
    """计算停用某基础表字段后的受影响视图。

    规则：
    * 字段出现在视图的 SELECT 投影中 -> 仅对应输出列受影响，并沿下游血缘传播；
    * 字段出现在 JOIN / WHERE / GROUP BY / HAVING / ORDER BY -> 该视图全部输出
      受影响；任何引用该「整视图受影响」视图的下游视图也整视图受影响；
    * 下游视图中受影响的列若用于 JOIN/WHERE 等谓词，则下游视图全部输出受影响。
    """
    base_name = catalog.base_index.get(norm(table))
    if base_name is None:
        raise KeyError(f"基础表 {table!r} 不存在")
    column_name = next(
        (col for col in catalog.base_tables[base_name] if norm(col) == norm(column)),
        None,
    )
    if column_name is None:
        raise KeyError(f"基础表 {base_name} 不包含字段 {column!r}")
    field_key = norm(column_name)
    base_key = norm(base_name)

    impacts: dict[str, _ViewImpact] = {}
    view_by_name = catalog.views

    for view_name in catalog.topological_order:
        view = view_by_name[view_name]
        impact = _ViewImpact(view=view)
        impacts[view_name] = impact

        # 1) 直接引用基础表字段
        for alias_key, source in view.sources.items():
            if source.kind != "table" or source.name_key != base_key:
                continue
            for ref in _filter_refs(view, alias_key, {field_key}):
                impact.add_full(
                    prefix=[_field_node(base_name, column_name)],
                    clause=ref.clause,
                    expression=ref.expression,
                    cause=_cause("direct", ref.clause, ref.expression),
                )
            for projection in view.projections:
                refs = _projection_refs(projection, alias_key, field_key)
                if not refs:
                    continue
                ref = refs[0]
                output = impact.ensure_output(projection.name)
                output.add(
                    path=[
                        _field_node(base_name, column_name),
                        _view_node(view, projection.name, PROJECTION_CLAUSE, projection.sql, False),
                    ],
                    cause=_cause("direct", PROJECTION_CLAUSE, projection.sql),
                )

        # 2) 引用上游视图的受影响输出
        for alias_key, source in view.sources.items():
            if source.kind != "view":
                continue
            upstream = view_by_name.get(source.name)
            upstream_impact = impacts.get(source.name) if upstream else None
            if upstream is None or upstream_impact is None:
                continue

            if upstream_impact.full:
                # 上游整视图失效时，下游视图无法依赖，整体标记受影响。
                best_path = _best_upstream_path(upstream_impact)
                impact.add_full(
                    prefix=best_path,
                    clause="UPSTREAM",
                    expression=f"引用视图 {upstream.name}（全部输出受影响）",
                    cause=_cause(
                        "downstream",
                        "UPSTREAM",
                        f"引用视图 {upstream.name}（全部输出受影响）",
                        via_view=upstream.name,
                    ),
                )
                continue

            affected_keys = set(upstream_impact.outputs)
            for ref in _filter_refs(view, alias_key, affected_keys):
                upstream_output = _output_for_key(upstream, ref.column_key)
                upstream_entry = upstream_impact.outputs.get(ref.column_key)
                prefix = _pick_path(upstream_entry, full=False) if upstream_entry else []
                impact.add_full(
                    prefix=prefix,
                    clause=ref.clause,
                    expression=ref.expression,
                    cause=_cause(
                        "downstream",
                        ref.clause,
                        ref.expression,
                        via_view=upstream.name,
                        via_output=upstream_output,
                    ),
                )
            for projection in view.projections:
                for ref in projection.refs:
                    if ref.alias_key != alias_key:
                        continue
                    if ref.column_key not in affected_keys:
                        continue
                    upstream_output = _output_for_key(upstream, ref.column_key)
                    upstream_entry = upstream_impact.outputs.get(ref.column_key)
                    prefix = _pick_path(upstream_entry, full=False) if upstream_entry else []
                    output = impact.ensure_output(projection.name)
                    output.add(
                        path=prefix
                        + [
                            _view_node(
                                view,
                                projection.name,
                                PROJECTION_CLAUSE,
                                projection.sql,
                                False,
                            )
                        ],
                        cause=_cause(
                            "downstream",
                            PROJECTION_CLAUSE,
                            projection.sql,
                            via_view=upstream.name,
                            via_output=upstream_output,
                        ),
                    )

        # 整视图影响：补全每个输出列的路径
        if impact.full and impact.full_causes:
            for output_name in view.output_names:
                bucket = impact.ensure_output(output_name)
                for full_cause in impact.full_causes:
                    bucket.add(
                        path=full_cause.prefix
                        + [
                            _view_node(
                                view,
                                output_name,
                                full_cause.clause,
                                full_cause.expression,
                                True,
                            )
                        ],
                        cause=full_cause.cause,
                    )

    affected_views: list[dict[str, Any]] = []
    for view_name in catalog.topological_order:
        impact = impacts[view_name]
        if not impact.outputs and not impact.full:
            continue
        affected_entries: list[dict[str, Any]] = []
        affected_keys: set[str] = set()
        for output_name in impact.view.output_names:
            key = norm(output_name)
            bucket = impact.outputs.get(key)
            if bucket is None or not bucket.candidates:
                continue
            affected_keys.add(key)
            path, _cause_value = min(
                bucket.candidates,
                key=lambda item: _path_rank(item[0], impact.full),
            )
            reasons = []
            seen_reasons: set[tuple[Any, ...]] = set()
            for candidate_path, candidate_cause in bucket.candidates:
                cause_key = _cause_key(candidate_cause)
                if cause_key in seen_reasons:
                    continue
                seen_reasons.add(cause_key)
                reasons.append(candidate_cause)
            affected_entries.append(
                {
                    "name": output_name,
                    "full_impact": impact.full,
                    "reasons": reasons,
                    "path": path,
                    "path_text": _path_text(path),
                }
            )
        if not affected_entries:
            continue
        unaffected = [
            name for name in impact.view.output_names if norm(name) not in affected_keys
        ]
        full_causes = []
        seen_full: set[tuple[Any, ...]] = set()
        for full_cause in impact.full_causes:
            cause_key = _cause_key(full_cause.cause)
            if cause_key in seen_full:
                continue
            seen_full.add(cause_key)
            full_causes.append(full_cause.cause)
        affected_views.append(
            {
                "view": impact.view.name,
                "sql": impact.view.sql,
                "depends_on": list(impact.view.dependencies),
                "full_impact": impact.full,
                "full_causes": full_causes,
                "all_outputs": list(impact.view.output_names),
                "affected_outputs": affected_entries,
                "unaffected_outputs": unaffected,
            }
        )

    affected_names = {item["view"] for item in affected_views}
    unaffected_views = [
        name for name in catalog.view_order if name not in affected_names
    ]
    affected_output_count = sum(
        len(item["affected_outputs"]) for item in affected_views
    )
    return {
        "field": {"table": base_name, "column": column_name},
        "affected_views": affected_views,
        "unaffected_views": unaffected_views,
        "summary": {
            "affected_view_count": len(affected_views),
            "total_view_count": len(catalog.view_order),
            "affected_output_count": affected_output_count,
            "full_impact_views": [
                item["view"] for item in affected_views if item["full_impact"]
            ],
        },
    }


def _output_for_key(view: ViewDef, column_key: str) -> str:
    for name in view.output_names:
        if norm(name) == column_key:
            return name
    return column_key


def _best_upstream_path(upstream_impact: _ViewImpact) -> list[dict[str, Any]]:
    best: list[dict[str, Any]] = []
    for bucket in upstream_impact.outputs.values():
        if not bucket.candidates:
            continue
        path, _cause_value = min(
            bucket.candidates, key=lambda item: _path_rank(item[0], upstream_impact.full)
        )
        if not best or len(path) < len(best):
            best = path
    return best


def _pick_path(bucket: _OutputCandidates | None, full: bool) -> list[dict[str, Any]]:
    if bucket is None or not bucket.candidates:
        return []
    path, _cause_value = min(
        bucket.candidates, key=lambda item: _path_rank(item[0], full)
    )
    return path


def _path_text(path: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for index, node in enumerate(path):
        if index == 0:
            parts.append(node.get("label", ""))
            continue
        clause = node.get("clause") or ""
        label = node.get("label", "")
        if node.get("full"):
            parts.append(f"--[{clause}]--> {label}（整视图）")
        else:
            parts.append(f"--[{clause}]--> {label}")
    return " ".join(parts)
