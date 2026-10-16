"""Main FastAPI application entry point for ProxyListHelper."""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import Base, engine
from .routes import admin, user

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create DB tables if they don't exist
    Base.metadata.create_all(bind=engine)
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

