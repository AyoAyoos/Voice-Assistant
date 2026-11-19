"""Shared function-calling tools for Gemini Live and the local LLM fallback.

All API keys are read from the environment on the backend and are never part of
a tool result, browser message, or log line. Every handler always calls the
result callback, even on failure, so a tool call can never stall a voice turn.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytz
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import EndFrame, FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

from storage import embeddings as embedding_service
from storage.duckdb_store import store

_WEB_TIMEOUT = 8.0
_MAX_RESULT_CHARS = 4000
_web_client: httpx.AsyncClient | None = None


async def get_web_client() -> httpx.AsyncClient:
    """One pooled client so search/extract connections stay warm."""
    global _web_client
    if _web_client is None or _web_client.is_closed:
        _web_client = httpx.AsyncClient(
            timeout=_WEB_TIMEOUT,
            headers={"User-Agent": "voice-assistant/1.0 (+local)"},
        )
    return _web_client


async def close_web_client() -> None:
    global _web_client
    if _web_client is not None and not _web_client.is_closed:
        await _web_client.aclose()
    _web_client = None


def _redact(text: str) -> str:
    safe = text
    for name in ("TAVILY_API_KEY", "BRAVE_API_KEY", "GEMINI_API_KEY"):
        key = os.getenv(name, "").strip()
        if key:
            safe = safe.replace(key, "***REDACTED***")
    return safe


async def _safe_call(params: FunctionCallParams, handler) -> None:
    try:
        await handler()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = _redact(str(exc))
        logger.error(f"Tool {params.function_name} failed: {message}")
        await params.result_callback(
            {"error": f"{params.function_name} is unavailable right now."},
            properties=FunctionCallResultProperties(run_llm=True),
        )


def _coerce_limit(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- web search


async def _tavily_search(query: str, limit: int) -> dict[str, Any]:
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Tavily is not configured")
    client = await get_web_client()
    response = await client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": key,
            "query": query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": limit,
            "include_answer": True,
            "include_raw_content": False,
        },
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or "")[:200],
                "url": str(item.get("url") or "")[:500],
                "snippet": " ".join(str(item.get("content") or "").split())[:600],
            }
        )
    return {
        "answer": " ".join(str(data.get("answer") or "").split())[:800] or None,
        "results": results,
        "source": "tavily",
    }


async def _brave_search(query: str, limit: int) -> dict[str, Any]:
    key = os.getenv("BRAVE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Brave is not configured")
    client = await get_web_client()
    response = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    response.raise_for_status()
    data = response.json()
    results = []
    for item in (data.get("web") or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or "")[:200],
                "url": str(item.get("url") or "")[:500],
                "snippet": " ".join(str(item.get("description") or "").split())[:600],
            }
        )
    return {"answer": None, "results": results, "source": "brave"}


async def web_search(params: FunctionCallParams) -> None:
    query = str(params.arguments.get("query") or "").strip()
    limit = _coerce_limit(params.arguments.get("max_results"), 5, 8)

    async def handler() -> None:
        if not query:
            await params.result_callback(
                {"error": "Please tell me what to search for."},
                properties=FunctionCallResultProperties(run_llm=True),
            )
            return
        payload: dict[str, Any] | None = None
        failures: list[str] = []
        for search in (_tavily_search, _brave_search):
            try:
                payload = await search(query, limit)
                if payload.get("results") or payload.get("answer"):
                    break
                payload = None
            except Exception as exc:
                failures.append(_redact(str(exc)))
        if payload is None:
            logger.warning(f"Web search unavailable: {'; '.join(failures)[:300]}")
            payload = {
                "error": "Web search is unavailable right now.",
                "results": [],
                "answer": None,
                "source": "none",
            }
        payload["query"] = query[:300]
        await params.result_callback(
            payload, properties=FunctionCallResultProperties(run_llm=True)
        )

    await _safe_call(params, handler)


# --------------------------------------------------------------- fetch page


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return " ".join(text.split())


async def _tavily_extract(url: str) -> str:
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Tavily is not configured")
    client = await get_web_client()
    response = await client.post(
        "https://api.tavily.com/extract",
        json={"api_key": key, "urls": [url], "include_raw_content": True},
    )
    response.raise_for_status()
    data = response.json()
    for item in data.get("results") or []:
        if isinstance(item, dict):
            content = item.get("raw_content") or item.get("content") or ""
            if content:
                return " ".join(str(content).split())[:_MAX_RESULT_CHARS]
    return ""


async def fetch_page(params: FunctionCallParams) -> None:
    url = str(params.arguments.get("url") or "").strip()

    async def handler() -> None:
        if not url.lower().startswith(("http://", "https://")):
            await params.result_callback(
                {"error": "That is not a valid web address."},
                properties=FunctionCallResultProperties(run_llm=True),
            )
            return
        text, source, failure = "", "none", ""
        try:
            text = await _tavily_extract(url)
            if text:
                source = "tavily"
        except Exception as exc:
            failure = _redact(str(exc))
        if not text:
            try:
                client = await get_web_client()
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                text = _html_to_text(response.text)[:_MAX_RESULT_CHARS]
                if text:
                    source = "direct"
            except Exception as exc:
                failure = f"{failure}; {_redact(str(exc))}" if failure else _redact(str(exc))
        if not text:
            logger.warning(f"fetch_page failed for {url[:120]}: {failure[:300]}")
        await params.result_callback(
            {
                "url": url[:500],
                "text": text or None,
                "source": source,
                "error": None if text else "I could not read that page.",
            },
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


# ------------------------------------------------------------ memory search


async def memory_search(params: FunctionCallParams) -> None:
    query = str(params.arguments.get("query") or "").strip()

    async def handler() -> None:
        if not query:
            await params.result_callback(
                {"memories": [], "count": 0},
                properties=FunctionCallResultProperties(run_llm=True),
            )
            return
        limit = _coerce_limit(params.arguments.get("limit"), 6, 12)
        embedding = await embedding_service.embedder.embed_text(query)
        matches = await store.search_memories(
            embedding=embedding, query=query, limit=limit
        )
        memories = [
            {
                "content": str(item.get("content") or "")[:300],
                "category": item.get("category"),
            }
            for item in matches
            if str(item.get("content") or "").strip()
        ]
        if matches:
            await store.touch_memories([str(item["id"]) for item in matches])
        await params.result_callback(
            {"memories": memories, "count": len(memories)},
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


# ------------------------------------------------------------- time and date


async def get_current_time(params: FunctionCallParams) -> None:
    tz_name = str(params.arguments.get("timezone") or "").strip() or "local"

    async def handler() -> None:
        zone = None
        if tz_name != "local":
            try:
                zone = pytz.timezone(tz_name)
            except (pytz.UnknownTimeZoneError, ValueError):
                zone = None
        now = datetime.now(zone) if zone else datetime.now().astimezone()
        label = tz_name if zone else "local"
        await params.result_callback(
            {
                "time": now.strftime("%I:%M %p").lstrip("0"),
                "date": now.strftime("%A, %B %-d, %Y")
                if os.name != "nt"
                else now.strftime("%A, %B %d, %Y"),
                "iso": now.isoformat(timespec="seconds"),
                "timezone": label,
            },
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


# ----------------------------------------------------------------- reminders


def _parse_due_at(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning(f"Ignoring unparseable reminder due_at: {text[:60]}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


async def store_reminder(params: FunctionCallParams) -> None:
    content = str(params.arguments.get("content") or "").strip()

    async def handler() -> None:
        if not content:
            await params.result_callback(
                {"error": "I need the reminder text."},
                properties=FunctionCallResultProperties(run_llm=True),
            )
            return
        due_at = _parse_due_at(params.arguments.get("due_at"))
        category = params.arguments.get("category")
        reminder_id = await store.add_reminder(
            content=content,
            due_at=due_at,
            category=str(category)[:40] if category else None,
            metadata={"source": "voice"},
        )
        await params.result_callback(
            {
                "status": "success",
                "action": "stored",
                "id": reminder_id,
                "content": content[:300],
                "due_at": due_at.isoformat() if due_at else None,
            },
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


async def list_reminders(params: FunctionCallParams) -> None:
    async def handler() -> None:
        status = str(params.arguments.get("status") or "pending").strip().lower()
        if status not in {"pending", "done", "all"}:
            status = "pending"
        limit = _coerce_limit(params.arguments.get("limit"), 10, 25)
        rows = await store.list_reminders(
            status=None if status == "all" else status, limit=limit
        )
        reminders = [
            {
                "content": str(item.get("content") or "")[:300],
                "status": item.get("status"),
                "due_at": str(item["due_at"]) if item.get("due_at") else None,
            }
            for item in rows
        ]
        await params.result_callback(
            {"reminders": reminders, "count": len(reminders)},
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


async def complete_reminder(params: FunctionCallParams) -> None:
    async def handler() -> None:
        reminder_id = str(params.arguments.get("reminder_id") or "").strip() or None
        text = str(params.arguments.get("content_contains") or "").strip() or None
        done = await store.complete_reminder(reminder_id=reminder_id, content_contains=text)
        await params.result_callback(
            {"status": "success" if done else "not_found", "completed": done},
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


# --------------------------------------------------------------- system info


async def get_system_info(params: FunctionCallParams) -> None:
    async def handler() -> None:
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"
        await params.result_callback(
            {
                "os": f"{platform.system()} {platform.release()}",
                "architecture": platform.machine(),
                "cpu_cores": os.cpu_count(),
                "hostname": hostname,
                "backend": os.getenv("VOICE_BACKEND", "auto"),
                "python": platform.python_version(),
            },
            properties=FunctionCallResultProperties(run_llm=True),
        )

    await _safe_call(params, handler)


# ----------------------------------------------------------- end conversation


def create_end_conversation_handler(worker_getter):
    """End the call after the assistant has spoken its goodbye."""

    async def end_conversation(params: FunctionCallParams) -> None:
        logger.info("end_conversation tool called")
        await params.result_callback(
            {"status": "success", "action": "ending_call"},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        worker = worker_getter()
        if worker is None:
            raise RuntimeError("Pipeline worker is not ready")
        # Gemini Live defers EndFrame until the current bot turn finishes; in the
        # local pipeline TTS also drains its audio context before the EndFrame.
        await worker.queue_frame(EndFrame(reason="end_call_tool"))

    return end_conversation


# ------------------------------------------------------------- schema factory


def build_tool_schemas(worker_getter) -> list[FunctionSchema]:
    """Build one fresh set of tools (with handlers) for a single call."""
    return [
        FunctionSchema(
            name="web_search",
            description=(
                "Search the live web for current or recent information, breaking news, "
                "people, companies, prices, or anything the assistant cannot know from "
                "its own knowledge. Returns short source snippets. Use this whenever a "
                "question could depend on information that changed recently."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "A focused search query in the user's language.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many sources to return (1-8). Defaults to 5.",
                },
            },
            required=["query"],
            handler=web_search,
        ),
        FunctionSchema(
            name="fetch_page",
            description=(
                "Read the text of a specific web page when a search snippet is not "
                "enough. Pass a full http:// or https:// URL."
            ),
            properties={
                "url": {"type": "string", "description": "The full page URL to read."}
            },
            required=["url"],
            handler=fetch_page,
        ),
        FunctionSchema(
            name="memory_search",
            description=(
                "Search saved long-term facts about the user when their question depends "
                "on something they told the assistant in an earlier conversation."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "What to look for in saved memories.",
                },
                "limit": {"type": "integer", "description": "Maximum results (1-12)."},
            },
            required=["query"],
            handler=memory_search,
        ),
        FunctionSchema(
            name="get_current_time",
            description=(
                "Get the current local time and date, or the time in a named IANA "
                "timezone such as Asia/Kolkata or America/New_York. Use this for any "
                "question about what time or date it is."
            ),
            properties={
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name. Omit for the user's local time.",
                }
            },
            required=[],
            handler=get_current_time,
        ),
        FunctionSchema(
            name="store_reminder",
            description=(
                "Store a reminder for later recall. This only saves it; it does not "
                "schedule a notification or speak it back. Convert relative times to an "
                "ISO 8601 due_at such as 2026-11-20T09:00:00 when the user gives one."
            ),
            properties={
                "content": {
                    "type": "string",
                    "description": "The reminder text, written as a short sentence.",
                },
                "due_at": {
                    "type": "string",
                    "description": "Optional ISO 8601 due date/time.",
                },
                "category": {"type": "string", "description": "Optional short category."},
            },
            required=["content"],
            handler=store_reminder,
        ),
        FunctionSchema(
            name="list_reminders",
            description="Recall reminders the user previously asked to store.",
            properties={
                "status": {
                    "type": "string",
                    "description": "pending (default), done, or all.",
                },
                "limit": {"type": "integer", "description": "Maximum results (1-25)."},
            },
            required=[],
            handler=list_reminders,
        ),
        FunctionSchema(
            name="complete_reminder",
            description="Mark a previously stored reminder as done.",
            properties={
                "reminder_id": {
                    "type": "string",
                    "description": "The reminder id returned when it was stored.",
                },
                "content_contains": {
                    "type": "string",
                    "description": "Or a few words from the reminder text.",
                },
            },
            required=[],
            handler=complete_reminder,
        ),
        FunctionSchema(
            name="get_system_info",
            description=(
                "Get basic information about the local machine running the assistant "
                "(operating system, CPU count, hostname, current backend)."
            ),
            properties={},
            required=[],
            handler=get_system_info,
        ),
        FunctionSchema(
            name="end_conversation",
            description=(
                "End the voice call after the assistant has spoken a short goodbye."
            ),
            properties={},
            required=[],
            handler=create_end_conversation_handler(worker_getter),
        ),
    ]
