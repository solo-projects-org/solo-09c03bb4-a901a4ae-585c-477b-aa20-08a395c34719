"""并发自测：导入与分析交错时结果必须对应某一版已提交目录。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.catalog import CatalogError  # noqa: E402
from app.impact import analyze_impact  # noqa: E402
from app.storage import CatalogStore  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8010")


def main() -> int:
    failures: list[str] = []

    # 场景一：同一进程内并发导入与分析，确认每个结果都属于完整版本。
    db_path = os.path.join(tempfile.mkdtemp(), "race.db")
    store = CatalogStore(db_path)
    versions_expected: dict[int, int] = {}
    lock = threading.Lock()

    def make_payload(count: int) -> str:
        views = [
            {
                "name": f"vw_{index}",
                "sql": f"SELECT o.order_id AS order_id, o.amount AS amount FROM orders o WHERE o.status = 's{index}'",
            }
            for index in range(count)
        ]
        if count > 1:
            views.append(
                {
                    "name": "vw_chain",
                    "sql": "SELECT v0.order_id AS order_id, v0.amount AS amount FROM vw_0 v0",
                }
            )
        return json.dumps({"base_tables": {"orders": ["order_id", "amount", "status"]}, "views": views})

    def importer() -> None:
        for count in range(2, 9):
            info = store.import_catalog(make_payload(count))
            with lock:
                versions_expected[info["version"]] = len(info["summary"]["views"])

    def analyzer() -> None:
        for _ in range(60):
            loaded = store.load_active_catalog()
            if loaded is None:
                continue
            catalog, row = loaded
            version = int(row["version"])
            result = analyze_impact(catalog, "orders", "status")
            # 该字段全部用于 WHERE，因此该版本所有视图都应整视图失效
            affected = {item["view"] for item in result["affected_views"]}
            expected_outputs = sum(
                len(catalog.views[name].output_names)
                for name in catalog.view_order
            )
            if affected != set(catalog.view_order):
                failures.append(f"v{version}: 受影响视图集合与目录不一致")
            if result["summary"]["affected_output_count"] != expected_outputs:
                failures.append(f"v{version}: 受影响列数与目录不一致")
            with lock:
                expected = versions_expected.get(version)
            if expected is not None and len(catalog.view_order) != expected:
                failures.append(
                    f"v{version}: 读到视图数 {len(catalog.view_order)} 与提交时 {expected} 不一致"
                )

    threads = [threading.Thread(target=importer), threading.Thread(target=analyzer),
               threading.Thread(target=analyzer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 场景二：非法导入不得改变生效版本
    before = store.active_version()
    try:
        store.import_catalog('{"base_tables": {"orders": ["order_id"]}, "views": [{"name": "bad", "sql": "SELECT x.a AS a FROM nope x"}]}')
        failures.append("非法目录未被拒绝")
    except CatalogError:
        pass
    if store.active_version() != before:
        failures.append("非法导入改变了生效版本")

    if failures:
        print("FAIL")
        for item in failures:
            print(" -", item)
        return 1
    print(f"OK 并发导入/分析一致性（最终版本 v{store.active_version()}，"
          f"{len(versions_expected)} 个版本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
