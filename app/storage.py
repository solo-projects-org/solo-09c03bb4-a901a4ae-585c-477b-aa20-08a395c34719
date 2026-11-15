"""SQLite 持久化：目录版本快照与「当前生效版本」指针。

每次导入要么完整提交为一个新版本（版本号单调递增），要么整份拒绝，绝不
产生半成品版本。分析结果总是绑定到某个不可变版本，避免导入/分析交错时混用
两个目录。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .catalog import Catalog, compile_catalog

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_versions (
    version      INTEGER PRIMARY KEY,
    payload      TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    summary      TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    active_version INTEGER NOT NULL REFERENCES catalog_versions(version)
);
"""


class CatalogStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._cache_lock = threading.Lock()
        self._cache: dict[int, Catalog] = {}
        self._cache_order: list[int] = []
        self._cache_limit = 8
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # 数据卷目录可能被外部清理；缺失时重新创建，避免 500。
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            # 数据卷被重建时自动补建表结构（PRAGMA 读取开销极小）。
            if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA user_version = 1")
                conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def import_catalog(self, raw_json: str) -> dict[str, Any]:
        """校验并整体替换目录。

        校验失败抛出 CatalogError，数据库与当前生效版本保持不变。
        成功返回新版本信息。
        """
        catalog = compile_catalog(raw_json)  # 纯校验，不触碰数据库
        canonical = _canonical_catalog(raw_json)
        summary = catalog.summary()
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS max_version FROM catalog_versions"
                ).fetchone()
                version = int(row["max_version"]) + 1
                conn.execute(
                    """
                    INSERT INTO catalog_versions
                        (version, payload, canonical, summary, sha256, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        raw_json,
                        canonical,
                        json.dumps(summary, ensure_ascii=False),
                        digest,
                        created_at,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO state (id, active_version) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET active_version = excluded.active_version
                    """,
                    (version,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self._cache_put(version, catalog)
        return {
            "version": version,
            "sha256": digest,
            "created_at": created_at,
            "summary": summary,
        }

    def seed_if_empty(self, raw_json: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM catalog_versions").fetchone()
        if int(row["n"]) > 0:
            return None
        return self.import_catalog(raw_json)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def active_version(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT active_version FROM state WHERE id = 1").fetchone()
        return int(row["active_version"]) if row else None

    def get_version_row(self, version: int) -> dict[str, Any] | None:
        """读取版本快照，并在同一条查询中判定其是否为当前生效版本。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT v.version, v.payload, v.canonical, v.summary,
                       v.sha256, v.created_at,
                       (s.active_version = v.version) AS is_active
                FROM catalog_versions v
                LEFT JOIN state s ON s.id = 1
                WHERE v.version = ?
                """,
                (version,),
            ).fetchone()
        return dict(row) if row else None

    def get_active_row(self) -> dict[str, Any] | None:
        """单条查询同时取出指针和快照，保证读到同一已提交版本。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT v.version, v.payload, v.canonical, v.summary,
                       v.sha256, v.created_at, 1 AS is_active
                FROM state s
                JOIN catalog_versions v ON v.version = s.active_version
                WHERE s.id = 1
                """
            ).fetchone()
        return dict(row) if row else None

    def list_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            active_row = conn.execute(
                "SELECT active_version FROM state WHERE id = 1"
            ).fetchone()
            rows = conn.execute(
                """
                SELECT version, sha256, created_at,
                       json_extract(summary, '$.views') AS views_json
                FROM catalog_versions ORDER BY version DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        active = int(active_row["active_version"]) if active_row else None
        result = []
        for row in rows:
            views = json.loads(row["views_json"] or "[]")
            result.append(
                {
                    "version": int(row["version"]),
                    "sha256": row["sha256"],
                    "created_at": row["created_at"],
                    "view_count": len(views),
                    "active": int(row["version"]) == active,
                }
            )
        return result

    def load_catalog(self, version: int) -> tuple[Catalog, dict[str, Any]] | None:
        row = self.get_version_row(version)
        if row is None:
            return None
        catalog = self._get_cached(version)
        if catalog is None:
            catalog = compile_catalog(row["payload"])
            self._cache_put(version, catalog)
        return catalog, row

    def load_active_catalog(self) -> tuple[Catalog, dict[str, Any]] | None:
        row = self.get_active_row()
        if row is None:
            return None
        catalog = self._get_cached(int(row["version"]))
        if catalog is None:
            catalog = compile_catalog(row["payload"])
            self._cache_put(int(row["version"]), catalog)
        return catalog, row

    # ------------------------------------------------------------------
    # 解析缓存（仅缓存不可变版本，命中与否不影响正确性）
    # ------------------------------------------------------------------
    def _get_cached(self, version: int) -> Catalog | None:
        with self._cache_lock:
            return self._cache.get(version)

    def _cache_put(self, version: int, catalog: Catalog) -> None:
        with self._cache_lock:
            self._cache[version] = catalog
            if version in self._cache_order:
                self._cache_order.remove(version)
            self._cache_order.append(version)
            while len(self._cache_order) > self._cache_limit:
                evicted = self._cache_order.pop(0)
                self._cache.pop(evicted, None)


def _canonical_catalog(raw_json: str) -> str:
    payload = json.loads(raw_json)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
