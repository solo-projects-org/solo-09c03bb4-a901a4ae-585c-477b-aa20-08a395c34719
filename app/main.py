"""FastAPI 应用：目录导入、版本查询与字段影响分析。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .catalog import CatalogError
from .impact import analyze_impact
from .storage import CatalogStore

MAX_UPLOAD_BYTES = 2 * 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("SETTLEMENT_DB", str(BASE_DIR / "data" / "settlement.db"))
    store = CatalogStore(db_path)
    app = FastAPI(
        title="结算视图影响分析",
        version="1.0.0",
        description="导入数仓目录，评估停用基础表字段将导致的结算视图失效范围。",
    )
    app.state.store = store

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "active_version": store.active_version()}

    @app.get("/api/sample")
    def sample_catalog() -> dict[str, Any]:
        sample_path = BASE_DIR / "samples" / "catalog.json"
        if not sample_path.is_file():
            raise HTTPException(status_code=404, detail="示例目录不存在")
        return json.loads(sample_path.read_text(encoding="utf-8"))

    @app.get("/api/catalog")
    def get_catalog() -> dict[str, Any]:
        loaded = store.load_active_catalog()
        if loaded is None:
            return {"exists": False, "version": None, "summary": None}
        catalog, row = loaded
        return {
            "exists": True,
            "version": int(row["version"]),
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "summary": catalog.summary(),
        }

    @app.get("/api/versions")
    def get_versions() -> dict[str, Any]:
        return {"versions": store.list_versions()}

    @app.get("/api/versions/{version}")
    def get_version(version: int) -> dict[str, Any]:
        loaded = store.load_catalog(version)
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"版本 {version} 不存在")
        catalog, row = loaded
        return {
            "version": int(row["version"]),
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "summary": catalog.summary(),
        }

    @app.post("/api/catalog/import")
    async def import_catalog(file: UploadFile = File(...)) -> dict[str, Any]:
        raw_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="目录文件超过 2 MB 限制")
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="文件必须是 UTF-8 编码的 JSON")
        if not text.strip():
            raise HTTPException(status_code=400, detail="文件为空")
        try:
            result = store.import_catalog(text)
        except CatalogError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": "目录校验失败，已保留原版目录", "errors": exc.errors},
            ) from exc
        return {
            "ok": True,
            "version": result["version"],
            "sha256": result["sha256"],
            "created_at": result["created_at"],
            "summary": result["summary"],
        }

    @app.post("/api/impact")
    def analyze(payload: dict[str, Any]) -> dict[str, Any]:
        table = payload.get("table")
        column = payload.get("column")
        raw_version = payload.get("version")
        if not isinstance(table, str) or not table.strip():
            raise HTTPException(status_code=422, detail="table 必须是非空字符串")
        if not isinstance(column, str) or not column.strip():
            raise HTTPException(status_code=422, detail="column 必须是非空字符串")

        version: int | None = None
        if raw_version is not None:
            if isinstance(raw_version, bool) or not isinstance(raw_version, int):
                raise HTTPException(status_code=422, detail="version 必须是整数")
            version = raw_version

        if version is None:
            loaded = store.load_active_catalog()
            if loaded is None:
                raise HTTPException(status_code=409, detail="尚未导入目录")
        else:
            loaded = store.load_catalog(version)
            if loaded is None:
                raise HTTPException(status_code=404, detail=f"版本 {version} 不存在")
        catalog, row = loaded
        try:
            result = analyze_impact(catalog, table.strip(), column.strip())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
        result["catalog"] = {
            "version": int(row["version"]),
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "is_active": bool(row.get("is_active")),
        }
        return result

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
