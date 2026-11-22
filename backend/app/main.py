import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .db import Base, SessionLocal, engine, ensure_schema
from .routers import alerts_api, auth, devices, ingest_api, logs, ops, policies, review_api, simulate
from .seed import seed
from .services import metrics, quality, recovery, scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

_stop = threading.Event()
_threads: list[threading.Thread] = []


def _worker(name: str, fn, interval: float, repeat_while_active: bool = False):
    """后台服务线程：每个 tick 使用独立会话，异常时回滚并继续。"""
    while not _stop.is_set():
        db = SessionLocal()
        try:
            if repeat_while_active:
                # 连续处理直到没有待办（用于质量服务快速排空批次）
                n = 0
                while fn(db) and n < 20 and not _stop.is_set():
                    n += 1
            else:
                fn(db)
        except Exception:
            db.rollback()
            log.exception("后台服务 %s 异常", name)
        finally:
            db.close()
        _stop.wait(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    ensure_schema()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    jobs = [
        ("scheduler", scheduler.scheduler_tick, 0.5, False),
        ("quality", quality.quality_tick, 0.3, True),
        ("metrics", metrics.metrics_tick, 2.0, False),
        ("recovery", recovery.recovery_tick, 5.0, False),
    ]
    for name, fn, interval, repeat in jobs:
        t = threading.Thread(target=_worker, args=(name, fn, interval, repeat), daemon=True, name=f"svc-{name}")
        t.start()
        _threads.append(t)
    log.info("后台服务已启动：scheduler/quality/metrics/recovery")
    yield
    _stop.set()
    for t in _threads:
        t.join(timeout=3)


app = FastAPI(title="化工园区监测写入与恢复控制台", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, devices.router, ingest_api.router, logs.router, policies.router,
          alerts_api.router, ops.router, simulate.router, review_api.router):
    app.include_router(r)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# 托管前端构建产物（存在时），SPA 路由回退到 index.html
_dist = os.path.normpath(config.FRONTEND_DIST)
if os.path.isdir(_dist):
    _assets = os.path.join(_dist, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("api"):
            from fastapi import HTTPException

            raise HTTPException(404, "not found")
        candidate = os.path.join(_dist, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_dist, "index.html"))
