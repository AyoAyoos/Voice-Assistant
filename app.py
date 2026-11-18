"""NEW (glue): single-command entrypoint for the voice assistant."""

import bootstrap  # noqa: F401  (puts the vendored Pipecat package on sys.path)

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

load_dotenv()

_ROOT = Path(__file__).parent
_LOG_DIR = _ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# One ERROR-only file sink in addition to stderr. stderr goes to
# .voice-backend.err.log when started by the hotkey launcher.
logger.add(
    str(_LOG_DIR / "errors.log"),
    level="ERROR",
    rotation="10 MB",
    retention="14 days",
    compression="zip",
    encoding="utf-8",
    enqueue=True,
    backtrace=True,
    diagnose=True,
)

from routes.webrtc_signaling import router as signaling_router  # noqa: E402
from storage.duckdb_store import store  # noqa: E402
from storage.embeddings import embedder  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.initialize()
    try:
        yield
    finally:
        await embedder.close()
        await store.close()


app = FastAPI(title="Voice Assistant", lifespan=lifespan)
app.include_router(signaling_router)


class ClientErrorPayload(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    source: str = "frontend"
    stack: str | None = None
    context: dict[str, Any] | None = None
    session_id: str | None = None


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.opt(exception=exc).error(
        f"Unhandled error on {request.method} {request.url.path}"
    )
    await store.log_error(
        source="fastapi",
        level="ERROR",
        message=f"Unhandled error on {request.method} {request.url.path}: {exc}",
        stack=str(exc),
        context={"path": request.url.path, "method": request.method},
    )
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.get("/health", include_in_schema=False)
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/status")
async def api_status():
    return {
        "storage": {
            "available": store.available,
            "path": str(store.path),
            "hnsw": store.hnsw_available,
            "embedding_dim": store.embedding_dim,
        },
        "embeddings": {
            "provider": "ollama",
            "model": embedder.model,
            "url": embedder.base_url,
        },
    }


@app.post("/api/client-error", include_in_schema=False)
async def client_error(payload: ClientErrorPayload):
    await store.log_error(
        source=payload.source[:80],
        level="ERROR",
        message=payload.message,
        stack=payload.stack,
        context=payload.context,
        session_id=payload.session_id,
    )
    return {"ok": True}


@app.get("/api/sessions")
async def api_sessions(limit: int = Query(50, ge=1, le=200)):
    return {"sessions": await store.list_sessions(limit=limit)}


@app.get("/api/sessions/{session_id}/messages")
async def api_session_messages(
    session_id: str, limit: int = Query(200, ge=1, le=1000)
):
    return {"messages": await store.get_session_messages(session_id, limit=limit)}


@app.get("/api/memory")
async def api_memory(limit: int = Query(100, ge=1, le=500)):
    return {"memories": await store.list_memories(limit=limit)}


@app.get("/api/search")
async def api_search(
    q: str = Query("", max_length=500),
    limit: int = Query(10, ge=1, le=50),
):
    query = q.strip()
    if not query:
        return {"query": "", "embedding_used": False, "memories": [], "messages": []}

    embedding = await embedder.embed_text(query)
    memories = await store.search_memories(embedding=embedding, query=query, limit=limit)
    messages = await store.search_messages(embedding=embedding, query=query, limit=limit)
    return {
        "query": query,
        "embedding_used": embedding is not None,
        "memories": memories,
        "messages": messages,
    }


_web_root = _ROOT / "web"
_static_dir = _web_root / "dist" if (_web_root / "dist").is_dir() else _web_root
app.mount(
    "/",
    StaticFiles(directory=_static_dir, html=True),
    name="web",
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )
