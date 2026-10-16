"""The FastAPI app factory. Run with:
    uvicorn portal.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import jobs
from .auth import GateRequired, LoginRequired
from .db import init_engine, session_scope
from .routes import account, admin, lists, orders, public
from .settings import get_settings

logging.basicConfig(level=logging.INFO)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app() -> FastAPI:
    settings = get_settings()
    init_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_scope() as session:
            requeued = jobs.requeue_stuck_jobs(session)
            if requeued:
                logging.getLogger("portal.main").warning("requeued %s stuck job(s) at startup", requeued)

        stop_event = threading.Event()
        job_thread = threading.Thread(
            target=jobs.run_worker_loop, args=(stop_event,),
            name="portal-job-worker", daemon=True,
        )
        job_thread.start()

        yield

        stop_event.set()
        job_thread.join(timeout=5)

    app = FastAPI(title="Proxy-Sammelbestellung", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    app.include_router(public.router)
    app.include_router(lists.router)
    app.include_router(orders.router)
    app.include_router(account.router)
    app.include_router(admin.router)

    @app.exception_handler(GateRequired)
    async def _gate_required(request: Request, exc: GateRequired):
        return RedirectResponse("/gate", status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if not settings.debug:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    return app


app = create_app()
