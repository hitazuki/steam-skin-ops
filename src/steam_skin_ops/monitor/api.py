from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from steam_skin_ops import __version__

from .events import StoreAlertDriver
from .config import DEFAULT_CONFIG, MonitorConfig, load_config
from .integrations.astrbot import AstrBotNotifier
from .integrations.smis import SmisClient
from .manager import MonitoringManager, ServiceError
from .repository import MonitorStorage
from .runtime import ServiceRuntime

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


class RuleCreate(BaseModel):
    recipient_key: str = Field(min_length=1, max_length=500)
    smis_id: int = Field(gt=0)
    rule_type: str = Field(min_length=2, max_length=20)
    threshold: float = Field(gt=0)


class RuleUpdate(BaseModel):
    recipient_key: str = Field(min_length=1, max_length=500)
    threshold: float = Field(gt=0)


class RecipientRequest(BaseModel):
    recipient_key: str = Field(min_length=1, max_length=500)


def ok(data=None) -> dict:
    return {"ok": True, "data": data}


def create_app(
    manager: MonitoringManager | None = None,
    runtime: ServiceRuntime | None = None,
    service_token: str | None = None,
    config: MonitorConfig | None = None,
    config_path: str | Path = DEFAULT_CONFIG,
) -> FastAPI:
    settings = config
    if settings is None and (
        service_token is None or manager is None or runtime is None
    ):
        settings = load_config(config_path)
    token = service_token if service_token is not None else settings.service_token
    if manager is None:
        driver_name = settings.alert_driver
        if driver_name == "store":
            driver = StoreAlertDriver()
        elif driver_name == "astrbot":
            driver = AstrBotNotifier(
                settings.astrbot_base_url,
                settings.astrbot_api_key,
                settings.astrbot_message_path,
                settings.astrbot_timeout_seconds,
            )
        else:
            raise RuntimeError("alerts.driver 必须是 store 或 astrbot")
        manager = MonitoringManager(
            MonitorStorage(settings.database),
            SmisClient(
                timeout=settings.smis_timeout_seconds,
                max_retries=settings.smis_max_retries,
                min_request_interval=settings.smis_min_request_interval_seconds,
            ),
            driver,
            max_items=settings.max_items,
            quote_cache_seconds=settings.quote_cache_seconds,
            breakthrough_step_percent=settings.breakthrough_step_percent,
            poll_interval_seconds=settings.interval_seconds,
        )
    if runtime is None:
        runtime = ServiceRuntime(
            manager,
            interval_seconds=settings.interval_seconds,
            backup_dir=settings.backup_dir,
            daily_summary_time=settings.daily_summary_time,
        )

    async def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if not token or credentials is None or not secrets.compare_digest(
            credentials.credentials, token
        ):
            raise ServiceError(401, "unauthorized", "服务令牌无效")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.start()
        yield
        runtime.stop()

    app = FastAPI(title="steam-skin-ops", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.runtime = runtime

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError):
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": error})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {
                    "code": "validation_error",
                    "message": "请求参数无效",
                    "data": exc.errors(),
                },
            },
        )

    @app.get("/healthz")
    async def healthz():
        status = runtime.status()
        return JSONResponse(
            status_code=200 if status.get("running") else 503,
            content=ok({"version": __version__, **status}),
        )

    @app.get("/v2/market/search", dependencies=[Depends(require_token)])
    async def search_items(
        q: str = Query(min_length=1, max_length=200),
        limit: int = Query(default=10, ge=1, le=20),
    ):
        return ok(await run_in_threadpool(manager.search_items, q, limit))

    @app.get("/v2/market/quote", dependencies=[Depends(require_token)])
    async def quote_item(q: str = Query(min_length=1, max_length=200)):
        return ok(await run_in_threadpool(manager.quote, q))

    @app.get("/v2/market/history", dependencies=[Depends(require_token)])
    async def market_history(
        q: str = Query(min_length=1, max_length=200),
        days: int = Query(default=7, ge=1, le=30),
    ):
        return ok(await run_in_threadpool(manager.market_history, q, days))

    @app.get("/v2/rules", dependencies=[Depends(require_token)])
    async def rules(
        recipient_key: str = Query(min_length=1, max_length=500),
        smis_id: int | None = Query(default=None, gt=0),
    ):
        return ok(await run_in_threadpool(manager.list_rules, recipient_key, smis_id))

    @app.post("/v2/rules", dependencies=[Depends(require_token)])
    async def add_rule(body: RuleCreate):
        return ok(await run_in_threadpool(
            manager.add_rule,
            body.recipient_key,
            body.smis_id,
            body.rule_type,
            body.threshold,
        ))

    @app.patch("/v2/rules/{rule_id}", dependencies=[Depends(require_token)])
    async def update_rule(rule_id: int, body: RuleUpdate):
        return ok(await run_in_threadpool(
            manager.update_rule, body.recipient_key, rule_id, body.threshold
        ))

    @app.delete("/v2/rules/{rule_id}", dependencies=[Depends(require_token)])
    async def remove_rule(
        rule_id: int,
        recipient_key: str = Query(min_length=1, max_length=500),
    ):
        await run_in_threadpool(manager.remove_rule, recipient_key, rule_id)
        return ok({"removed": True})

    @app.get("/v2/events", dependencies=[Depends(require_token)])
    async def events(
        recipient_key: str = Query(min_length=1, max_length=500),
        acknowledged: bool | None = Query(default=False),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        return ok(await run_in_threadpool(
            manager.storage.list_events,
            recipient_key,
            acknowledged=acknowledged,
            limit=limit,
        ))

    @app.post("/v2/events/{event_id}/ack", dependencies=[Depends(require_token)])
    async def acknowledge_event(event_id: int, body: RecipientRequest):
        event = await run_in_threadpool(
            manager.storage.acknowledge_event, event_id, body.recipient_key
        )
        if event is None:
            raise ServiceError(404, "event_not_found", "未找到该告警事件")
        return ok(event)

    @app.post("/v2/events/test", dependencies=[Depends(require_token)])
    async def test_event(body: RecipientRequest):
        return ok(await run_in_threadpool(manager.test_event, body.recipient_key))

    @app.get("/v2/monitor/items", dependencies=[Depends(require_token)])
    async def monitored_items():
        return ok(await run_in_threadpool(manager.list_items))

    @app.get("/v2/monitor/status", dependencies=[Depends(require_token)])
    async def monitor_status():
        return ok({"version": __version__, **runtime.status()})

    return app
