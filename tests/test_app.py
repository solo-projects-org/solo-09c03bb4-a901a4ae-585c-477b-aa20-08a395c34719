import json
import os

import pytest
from fastapi.testclient import TestClient

from app.catalog import CatalogError, compile_catalog
from app.impact import analyze_impact
from app.main import create_app

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "..", "samples", "catalog.json")


def sample_payload() -> dict:
    with open(SAMPLE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def compile_sample():
    return compile_catalog(json.dumps(sample_payload()))


def affected_map(result: dict) -> dict[str, list[str]]:
    return {
        item["view"]: [output["name"] for output in item["affected_outputs"]]
        for item in result["affected_views"]
    }


# ---------------------------------------------------------------------------
# 目录校验
# ---------------------------------------------------------------------------


def test_sample_catalog_compiles():
    catalog = compile_sample()
    assert catalog.view_count == 10
    assert catalog.base_count == 4
    # 依赖优先的拓扑顺序
    assert catalog.topological_order.index("vw_settlement") > catalog.topological_order.index(
        "vw_order_net"
    )
    assert catalog.topological_order.index("vw_paid_orders") < catalog.topological_order.index(
        "vw_paid_settlement"
    )


def test_view_metadata_shape():
    catalog = compile_sample()
    settlement = catalog.views["vw_settlement"]
    assert settlement.output_names == [
        "order_id",
        "customer_id",
        "currency",
        "open_amount",
        "refund_amount",
        "created_at",
    ]
    assert set(settlement.dependencies) == {"vw_order_net", "vw_order_payments"}
    open_amount = settlement.fields_used_by_output("open_amount")
    assert {(ref.alias_key, ref.column) for ref in open_amount} == {
        ("n", "net_amount"),
        ("p", "paid_total"),
    }
    # JOIN 上的 g.order_id 出现在 filter_refs
    assert any(
        ref.clause == "JOIN" and ref.column == "order_id" for ref in settlement.filter_refs
    )


def test_reject_missing_table():
    payload = {
        "base_tables": {"orders": ["order_id"]},
        "views": [
            {"name": "v1", "sql": "SELECT o.order_id AS id FROM missing o"},
        ],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "不存在的表或视图" in str(exc.value)


def test_reject_missing_column():
    payload = {
        "base_tables": {"orders": ["order_id", "amount"]},
        "views": [{"name": "v1", "sql": "SELECT o.nope AS nope FROM orders o"}],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "字段 orders.nope 不存在" in str(exc.value)


def test_reject_duplicate_output_columns():
    payload = {
        "base_tables": {"orders": ["order_id", "amount"]},
        "views": [
            {"name": "v1", "sql": "SELECT o.order_id AS x, o.amount AS X FROM orders o"}
        ],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "输出列重名" in str(exc.value)


def test_reject_unqualified_column():
    payload = {
        "base_tables": {"orders": ["order_id"]},
        "views": [{"name": "v1", "sql": "SELECT order_id FROM orders o"}],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "缺少表别名" in str(exc.value)


def test_reject_unqualified_column_in_join_and_where():
    payload = {
        "base_tables": {"orders": ["order_id"], "payments": ["order_id"]},
        "views": [
            {
                "name": "v_join",
                "sql": "SELECT o.order_id AS id FROM orders o JOIN payments p ON order_id = p.order_id",
            }
        ],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "缺少表别名（JOIN）" in str(exc.value)

    payload["views"] = [
        {"name": "v_where", "sql": "SELECT o.order_id AS id FROM orders o WHERE order_id > 0"}
    ]
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "缺少表别名（WHERE）" in str(exc.value)


def test_reject_cycle():
    payload = {
        "base_tables": {"orders": ["order_id"]},
        "views": [
            {"name": "a", "sql": "SELECT b.id AS id FROM b b"},
            {"name": "b", "sql": "SELECT c.id AS id FROM c c"},
            {"name": "c", "sql": "SELECT a.id AS id FROM a a"},
        ],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "循环引用" in str(exc.value)
    assert "a" in str(exc.value) and "c" in str(exc.value)


def test_reject_star_projection_and_using_join():
    payload = {
        "base_tables": {"orders": ["order_id"], "payments": ["order_id"]},
        "views": [{"name": "v1", "sql": "SELECT o.* FROM orders o"}],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "不支持 * 通配投影" in str(exc.value)

    payload["views"] = [
        {
            "name": "v1",
            "sql": "SELECT o.order_id AS id FROM orders o JOIN payments p USING (order_id)",
        }
    ]
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "USING" in str(exc.value)


def test_reject_subquery_cte_union():
    payload = {
        "base_tables": {"orders": ["order_id"]},
        "views": [
            {"name": "v1", "sql": "SELECT o.order_id AS id FROM (SELECT x.order_id FROM orders x) o"},
        ],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "子查询" in str(exc.value)

    payload["views"] = [
        {"name": "v1", "sql": "WITH c AS (SELECT o.order_id FROM orders o) SELECT c.order_id AS id FROM c c"}
    ]
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "CTE" in str(exc.value)

    payload["views"] = [
        {
            "name": "v1",
            "sql": "SELECT o.order_id AS id FROM orders o UNION ALL SELECT p.order_id AS id FROM orders p",
        }
    ]
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "集合运算" in str(exc.value)


def test_reject_bad_json():
    with pytest.raises(CatalogError) as exc:
        compile_catalog("{not json")
    assert "JSON 解析失败" in str(exc.value)


def test_unknown_alias_rejected():
    payload = {
        "base_tables": {"orders": ["order_id"]},
        "views": [{"name": "v1", "sql": "SELECT x.order_id AS id FROM orders o"}],
    }
    with pytest.raises(CatalogError) as exc:
        compile_catalog(json.dumps(payload))
    assert "未知表别名" in str(exc.value)


def test_alias_and_column_case_insensitive_resolution():
    payload = {
        "base_tables": {"Orders": ["Order_ID", "Amount"]},
        "views": [{"name": "v1", "sql": "SELECT O.order_id AS id FROM orders O"}],
    }
    catalog = compile_catalog(json.dumps(payload))
    assert catalog.views["v1"].output_names == ["id"]


def test_expression_projection_without_alias_uses_sql_text():
    payload = {
        "base_tables": {"orders": ["amount"]},
        "views": [{"name": "v1", "sql": "SELECT o.amount * 2 FROM orders o"}],
    }
    catalog = compile_catalog(json.dumps(payload))
    assert catalog.views["v1"].output_names == ["o.amount * 2"]


def test_output_alias_allowed_in_group_and_order_by():
    payload = {
        "base_tables": {"orders": ["amount"]},
        "views": [
            {
                "name": "v1",
                "sql": "SELECT o.amount AS amt FROM orders o GROUP BY amt HAVING amt > 0 ORDER BY amt",
            }
        ],
    }
    catalog = compile_catalog(json.dumps(payload))
    assert catalog.views["v1"].filter_refs == []


# ---------------------------------------------------------------------------
# 影响分析
# ---------------------------------------------------------------------------


def test_projection_field_only_affects_single_output():
    result = analyze_impact(compile_sample(), "orders", "discount_amount")
    affected = affected_map(result)
    assert affected["vw_paid_orders"] == ["discount_amount"]
    assert affected["vw_order_net"] == ["net_amount"]
    assert affected["vw_settlement"] == ["open_amount"]
    assert "order_id" not in affected["vw_settlement"]
    # 下游通过 open_amount 传播，但 GROUP BY 未涉及，只影响合计列
    assert affected["vw_settlement_by_region"] == ["total_open_amount"]


def test_join_field_fans_out_to_all_outputs_and_downstream():
    result = analyze_impact(compile_sample(), "orders", "order_id")
    summary = result["summary"]
    assert "vw_paid_orders" in summary["full_impact_views"]
    assert "vw_settlement" in summary["full_impact_views"]
    # vw_order_payments 只来自 payments，不应受影响
    assert "vw_order_payments" not in {v["view"] for v in result["affected_views"]}
    settlement = next(v for v in result["affected_views"] if v["view"] == "vw_settlement")
    assert settlement["full_impact"] is True
    assert settlement["unaffected_outputs"] == []
    assert {o["name"] for o in settlement["affected_outputs"]} == set(
        settlement["all_outputs"]
    )
    # 下游所有视图都整视图失效
    by_region = next(
        v for v in result["affected_views"] if v["view"] == "vw_settlement_by_region"
    )
    assert by_region["full_impact"] is True


def test_where_field_marks_entire_view_and_downstream():
    result = analyze_impact(compile_sample(), "orders", "status")
    affected = affected_map(result)
    assert set(affected["vw_paid_orders"]) == {
        "order_id",
        "customer_id",
        "amount",
        "discount_amount",
        "currency",
        "created_at",
    }
    assert set(affected["vw_paid_settlement"]) == {
        "order_id",
        "amount",
        "paid_total",
        "open_amount",
    }
    paid = next(v for v in result["affected_views"] if v["view"] == "vw_paid_orders")
    assert paid["full_causes"][0]["clause"] == "WHERE"


def test_path_contains_each_hop():
    result = analyze_impact(compile_sample(), "payments", "refund_amount")
    settlement = next(v for v in result["affected_views"] if v["view"] == "vw_settlement")
    output = next(o for o in settlement["affected_outputs"] if o["name"] == "refund_amount")
    labels = [node["label"] for node in output["path"]]
    assert labels == [
        "payments.refund_amount",
        "vw_order_payments.refund_total",
        "vw_settlement.refund_amount",
    ]
    assert [node["clause"] for node in output["path"][1:]] == ["SELECT", "SELECT"]


def test_unused_field_reports_no_impact():
    result = analyze_impact(compile_sample(), "orders", "region_code")
    assert result["affected_views"] == []
    assert result["summary"]["affected_view_count"] == 0
    assert len(result["unaffected_views"]) == 10


def test_payments_channel_unused_but_refund_used():
    catalog = compile_sample()
    assert analyze_impact(catalog, "payments", "channel")["affected_views"] == []
    assert analyze_impact(catalog, "payments", "refund_amount")["affected_views"]


def test_unknown_field_raises():
    catalog = compile_sample()
    with pytest.raises(KeyError):
        analyze_impact(catalog, "orders", "nope")
    with pytest.raises(KeyError):
        analyze_impact(catalog, "nope", "order_id")


def test_currency_join_marks_fx_view_full():
    result = analyze_impact(compile_sample(), "orders", "currency")
    fx = next(v for v in result["affected_views"] if v["view"] == "vw_settlement_fx")
    assert fx["full_impact"] is True
    # 只用 currency 做 JOIN 条件，但视图整视图失效
    assert {o["name"] for o in fx["affected_outputs"]} == {
        "order_id",
        "fx_rate",
        "open_amount_cny",
    }


# ---------------------------------------------------------------------------
# API / 版本一致性
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path=db_path)
    with TestClient(app) as test_client:
        yield test_client


def import_payload(client, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post(
        "/api/catalog/import",
        files={"file": ("catalog.json", body, "application/json")},
    )


def test_import_and_active_version(client):
    resp = import_payload(client, sample_payload())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 1
    assert len(data["summary"]["views"]) == 10

    catalog = client.get("/api/catalog").json()
    assert catalog["exists"] is True
    assert catalog["version"] == 1


def test_rejected_import_keeps_previous_version(client):
    import_payload(client, sample_payload())
    bad = sample_payload()
    bad["views"].append(
        {"name": "vw_broken", "sql": "SELECT o.nope AS nope FROM orders o"}
    )
    resp = import_payload(client, bad)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["message"] == "目录校验失败，已保留原版目录"
    assert any("vw_broken" in error for error in detail["errors"])

    catalog = client.get("/api/catalog").json()
    assert catalog["version"] == 1
    versions = client.get("/api/versions").json()["versions"]
    assert len(versions) == 1


def test_cyclic_import_rejected(client):
    import_payload(client, sample_payload())
    cyclic = {
        "base_tables": {"orders": ["order_id"]},
        "views": [
            {"name": "x", "sql": "SELECT y.id AS id FROM y y"},
            {"name": "y", "sql": "SELECT x.id AS id FROM x x"},
        ],
    }
    resp = import_payload(client, cyclic)
    assert resp.status_code == 422
    assert client.get("/api/catalog").json()["version"] == 1


def test_successive_imports_increment_versions(client):
    import_payload(client, sample_payload())
    second = sample_payload()
    second["views"].append(
        {
            "name": "vw_extra",
            "sql": "SELECT o.order_id AS order_id, o.amount AS amount FROM orders o",
        }
    )
    resp = import_payload(client, second)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2
    versions = client.get("/api/versions").json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["active"] is True
    # 旧版本仍可查询且内容不变
    old = client.get("/api/versions/1").json()
    assert len(old["summary"]["views"]) == 10
    new = client.get("/api/versions/2").json()
    assert len(new["summary"]["views"]) == 11


def test_impact_pins_version(client):
    import_payload(client, sample_payload())
    first = client.post(
        "/api/impact", json={"table": "orders", "column": "status"}
    ).json()
    assert first["catalog"]["version"] == 1

    second_payload = sample_payload()
    second_payload["views"].append(
        {
            "name": "vw_status_only",
            "sql": "SELECT o.order_id AS order_id FROM orders o WHERE o.status = 'paid'",
        }
    )
    import_payload(client, second_payload)

    pinned = client.post(
        "/api/impact", json={"table": "orders", "column": "status", "version": 1}
    ).json()
    assert pinned["catalog"]["version"] == 1
    assert pinned["catalog"]["is_active"] is False
    assert "vw_status_only" not in {v["view"] for v in pinned["affected_views"]}

    latest = client.post("/api/impact", json={"table": "orders", "column": "status"}).json()
    assert latest["catalog"]["version"] == 2
    assert latest["catalog"]["is_active"] is True
    assert "vw_status_only" in {v["view"] for v in latest["affected_views"]}


def test_impact_before_import_is_conflict(client):
    resp = client.post("/api/impact", json={"table": "orders", "column": "status"})
    assert resp.status_code == 409


def test_impact_unknown_field_and_version(client):
    import_payload(client, sample_payload())
    missing = client.post("/api/impact", json={"table": "orders", "column": "nope"})
    assert missing.status_code == 404
    no_version = client.post(
        "/api/impact", json={"table": "orders", "column": "status", "version": 99}
    )
    assert no_version.status_code == 404


def test_impact_validation(client):
    import_payload(client, sample_payload())
    assert client.post("/api/impact", json={"table": "", "column": "x"}).status_code == 422
    assert client.post("/api/impact", json={"table": "orders"}).status_code == 422
    assert (
        client.post(
            "/api/impact",
            json={"table": "orders", "column": "status", "version": "1"},
        ).status_code
        == 422
    )


def test_import_rejects_non_json_upload(client):
    resp = client.post(
        "/api/catalog/import",
        files={"file": ("catalog.json", b"\xff\xfe\x00", "application/json")},
    )
    assert resp.status_code == 400


def test_versions_survive_reopen(tmp_path):
    db_path = str(tmp_path / "persist.db")
    app1 = create_app(db_path=db_path)
    with TestClient(app1) as c1:
        import_payload(c1, sample_payload())
    app2 = create_app(db_path=db_path)
    with TestClient(app2) as c2:
        catalog = c2.get("/api/catalog").json()
        assert catalog["version"] == 1
        result = c2.post(
            "/api/impact", json={"table": "orders", "column": "discount_amount"}
        ).json()
        assert result["catalog"]["version"] == 1


def test_index_and_static_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
