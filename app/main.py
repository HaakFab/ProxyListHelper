"""Main FastAPI application entry point for ProxyListHelper."""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine, init_db
from .routes import admin, user

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create DB tables and run any safe column migrations
    init_db()
    yield

app = FastAPI(
    title=settings.app_name,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(user.router)
app.include_router(admin.router)

import logging
from starlette.requests import Request
from starlette.responses import HTMLResponse

logger = logging.getLogger("app")

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Global unhandled exception on %s %s: %s", request.method, request.url, exc)
    if "/resolve" in str(request.url) or "/success" in str(request.url):
        return HTMLResponse(
            content="""<!DOCTYPE html>
<html>
<head>
  <title>Order Submitted | ProxyListHelper</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="cutting-mat-workspace" style="display: flex; align-items: center; justify-content: center; min-height: 100vh;">
  <div class="glass-card" style="max-width: 600px; text-align: center; border: 2px solid var(--border-ink); box-shadow: 4px 4px 0px #000000; padding: 32px;">
    <div style="font-family: var(--font-mono); font-size: 0.8rem; font-weight: 800; color: #047857; text-transform: uppercase; margin-bottom: 8px;">
      [ORDER TRANSMISSION CONFIRMED]
    </div>
    <h1 style="font-size: 1.8rem; margin-bottom: 12px; line-height: 1.25;">Order submitted, you can close this window now.</h1>
    <p style="font-size: 1.1rem; font-weight: 700; margin-bottom: 24px; color: #111111;">Your order has been recorded. Click below when you are ready to return to the starting screen.</p>
    <a href="/?lang=en" class="btn btn-primary" style="padding: 12px 24px;">[RETURN TO STARTING SCREEN]</a>
  </div>
</body>
</html>""",
            status_code=200
        )
    return HTMLResponse(
        content="""<!DOCTYPE html>
<html>
<head>
  <title>Notice | ProxyListHelper</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="cutting-mat-workspace" style="display: flex; align-items: center; justify-content: center; min-height: 100vh;">
  <div class="glass-card" style="max-width: 600px; text-align: center; border: 2px solid var(--border-ink); padding: 32px;">
    <h2 style="font-size: 1.5rem; margin-bottom: 12px;">Notice</h2>
    <p style="font-size: 1rem; margin-bottom: 20px;">An unexpected issue occurred while processing your request.</p>
    <a href="/?lang=en" class="btn btn-primary">[RETURN TO STARTING SCREEN]</a>
  </div>
</body>
</html>""",
        status_code=500
    )

@app.get("/healthz")
def healthz():
    return {"status": "ok", "app": settings.app_name}

if __name__ == "__main__":
    import sys
    print("\n[!] Notice: Please run 'main.py' from the root project directory:")
    print("    cd /Users/fabian/Python/mtg_proxy_parser")
    print("    source .venv-web/bin/activate")
    print("    python3 main.py\n")
    sys.exit(1)

