"""启动入口：可选地用 SEED_CATALOG 初始化空库。"""

from __future__ import annotations

import os
from pathlib import Path

from .main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    seed = os.environ.get("SEED_CATALOG")
    if seed:
        seed_path = Path(seed)
        if not seed_path.is_file():
            raise SystemExit(f"SEED_CATALOG 指向的文件不存在: {seed_path}")
        store = app.state.store
        if store.active_version() is None:
            try:
                info = store.seed_if_empty(seed_path.read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover - 启动期显式失败
                raise SystemExit(f"种子目录导入失败: {exc}") from exc
            if info:
                print(
                    "已导入种子目录，版本 "
                    f"{info['version']}，视图 {len(info['summary']['views'])} 个"
                )
        else:
            print("数据库已有目录，跳过种子导入")

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
