"""DuckDB-backed sessions, transcripts, memory, HNSW vectors and error logs.

All data stays in one local file. The VSS extension supplies HNSW indexes; if it
cannot be loaded the same tables still work with an exact cosine scan.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from loguru import logger

try:
    import duckdb
except Exception as exc:  # pragma: no cover - only hit when dependency is missing
    duckdb = None  # type: ignore[assignment]
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _redact(text: str) -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return text.replace(key, "***REDACTED***") if key else text


class VoiceStore:
    """Async facade around a single DuckDB connection.

    DuckDB is synchronous, so every public method takes an asyncio lock. The
    project only runs one local assistant session, which makes this simpler and
    safer than managing a connection pool.
    """

    def __init__(self) -> None:
        configured = os.getenv("VOICE_DB_PATH", "").strip()
        root = Path(__file__).resolve().parent.parent
        self.path = Path(configured) if configured else root / "data" / "voice_assistant.duckdb"
        self.embedding_dim = self._positive_int("EMBEDDING_DIM", 768)
        self.dedupe_threshold = float(os.getenv("MEMORY_DEDUPE_THRESHOLD", "0.93"))
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self._available = False
        self._vectors_available = False
        self._hnsw_available = False

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
            return value if value > 0 else default
        except ValueError:
            logger.error(f"Invalid {name}; using default {default}")
            return default

    @property
    def available(self) -> bool:
        return self._available

    @property
    def hnsw_available(self) -> bool:
        return self._hnsw_available

    async def initialize(self) -> None:
        if duckdb is None:
            logger.error(f"DuckDB is unavailable; persistence disabled: {_IMPORT_ERROR}")
            return

        async with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._conn = duckdb.connect(str(self.path))
                self._load_vss()
                self._create_schema()
                # Any active row belongs to a previous backend process.
                stale = self._conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'ended', ended_at = COALESCE(ended_at, current_timestamp)
                    WHERE status = 'active'
                    """
                ).rowcount
                if stale and stale > 0:
                    logger.info(f"Marked {stale} stale session row(s) as ended")
                self._rebuild_indexes()
                self._available = True
                logger.info(
                    f"DuckDB ready at {self.path} "
                    f"(HNSW={'on' if self._hnsw_available else 'off'})"
                )
            except Exception as exc:
                self._available = False
                logger.opt(exception=exc).error(f"Could not initialize DuckDB at {self.path}")

    def _load_vss(self) -> None:
        try:
            self._conn.execute("LOAD vss")
            self._hnsw_available = True
        except Exception as load_error:
            logger.warning(f"DuckDB vss extension not preinstalled: {load_error}")
            try:
                self._conn.execute("INSTALL vss")
                self._conn.execute("LOAD vss")
                self._hnsw_available = True
            except Exception as install_error:
                logger.error(f"Could not enable DuckDB HNSW index: {install_error}")
                self._hnsw_available = False

        if self._hnsw_available:
            try:
                # DuckDB requires this opt-in for HNSW indexes in file-backed DBs.
                self._conn.execute("SET hnsw_enable_experimental_persistence=true")
            except Exception as exc:
                logger.error(f"Could not enable HNSW persistence: {exc}")
                self._hnsw_available = False

    def _create_schema(self) -> None:
        vector_type = f"FLOAT[{self.embedding_dim}]"
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id VARCHAR PRIMARY KEY,
                signaling_session_id INTEGER NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ,
                status VARCHAR NOT NULL,
                title VARCHAR,
                model VARCHAR,
                voice VARCHAR,
                language VARCHAR,
                message_count INTEGER NOT NULL DEFAULT 0,
                memory_count INTEGER NOT NULL DEFAULT 0,
                metadata TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id VARCHAR PRIMARY KEY,
                session_id VARCHAR NOT NULL,
                role VARCHAR NOT NULL,
                text VARCHAR NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                turn_index INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id VARCHAR PRIMARY KEY,
                content VARCHAR NOT NULL,
                category VARCHAR,
                confidence DOUBLE NOT NULL DEFAULT 0.7,
                source_session_id VARCHAR,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                last_used_at TIMESTAMPTZ,
                use_count INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                metadata TEXT
            )
            """
        )
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS message_embeddings (
                id VARCHAR PRIMARY KEY,
                message_id VARCHAR NOT NULL,
                session_id VARCHAR NOT NULL,
                model VARCHAR NOT NULL,
                embedding {vector_type} NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                id VARCHAR PRIMARY KEY,
                memory_id VARCHAR NOT NULL,
                model VARCHAR NOT NULL,
                embedding {vector_type} NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_errors (
                id VARCHAR PRIMARY KEY,
                occurred_at TIMESTAMPTZ NOT NULL,
                source VARCHAR NOT NULL,
                level VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                stack TEXT,
                context TEXT,
                session_id VARCHAR
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS messages_session_idx ON messages(session_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS messages_created_idx ON messages(created_at)"
        )

        expected = vector_type.upper().replace(" ", "")
        for table in ("message_embeddings", "memory_embeddings"):
            try:
                description = self._rows(f"DESCRIBE {table}")
            except Exception:
                continue
            column = next(
                (row for row in description if row.get("column_name") == "embedding"),
                None,
            )
            if column:
                actual = str(column.get("column_type", "")).upper().replace(" ", "")
                if actual != expected:
                    logger.error(
                        f"{table}.embedding is {actual}, expected {expected}; "
                        "vector search disabled until EMBEDDING_DIM matches the stored schema"
                    )
                    self._vectors_available = False
                    return
        self._vectors_available = True

    def _rebuild_indexes(self) -> None:
        # Rebuild normal indexes too. A force-killed backend can leave a stale
        # secondary index in the file, which makes joins return wrong counts.
        try:
            normal_indexes = (
                ("messages_session_idx", "CREATE INDEX messages_session_idx ON messages(session_id)"),
                ("messages_created_idx", "CREATE INDEX messages_created_idx ON messages(created_at)"),
            )
            for index_name, create_sql in normal_indexes:
                self._conn.execute(f"DROP INDEX IF EXISTS {index_name}")
                self._conn.execute(create_sql)
        except Exception as exc:
            logger.error(f"Could not rebuild DuckDB indexes: {exc}")

        if not self._vectors_available or not self._hnsw_available:
            return
        try:
            for table in ("message_embeddings", "memory_embeddings"):
                index_name = f"{table}_hnsw_idx"
                self._conn.execute(f"DROP INDEX IF EXISTS {index_name}")
                count = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if count:
                    self._conn.execute(
                        f"CREATE INDEX {index_name} ON {table} USING HNSW (embedding) "
                        "WITH (metric = 'cosine')"
                    )
        except Exception as exc:
            logger.error(f"Could not rebuild HNSW indexes: {exc}")
            self._hnsw_available = False

    def _rows(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, list(params or []))
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self._conn.execute(sql, list(params or []))

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as exc:
                    logger.error(f"Could not close DuckDB cleanly: {exc}")
                self._conn = None
            self._available = False

    # ------------------------------------------------------------------ sessions

    async def create_session(
        self,
        signaling_session_id: int,
        model: str | None,
        voice: str | None,
        language: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        session_id = _new_id()
        if not self._available:
            return session_id
        async with self._lock:
            try:
                self._execute(
                    """
                    INSERT INTO sessions (
                        id, signaling_session_id, started_at, status, model,
                        voice, language, metadata
                    ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    [
                        session_id,
                        int(signaling_session_id),
                        _utcnow(),
                        model,
                        voice,
                        language,
                        _json(metadata),
                    ],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not create session row")
        return session_id

    async def finish_session(
        self,
        session_id: str,
        status: str = "ended",
        memory_count: int | None = None,
    ) -> None:
        if not self._available:
            return
        async with self._lock:
            try:
                self._execute(
                    """
                    UPDATE sessions
                    SET ended_at = ?, status = ?, memory_count = COALESCE(?, memory_count)
                    WHERE id = ?
                    """,
                    [_utcnow(), status, memory_count, session_id],
                )
            except Exception as exc:
                logger.opt(exception=exc).error(f"Could not finish session {session_id}")

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._available:
            return []
        async with self._lock:
            try:
                return self._rows(
                    """
                    SELECT
                        s.id, s.signaling_session_id, s.started_at, s.ended_at,
                        s.status, s.title, s.model, s.voice, s.language,
                        s.message_count, s.memory_count,
                        count(m.id) AS stored_message_count
                    FROM sessions s
                    LEFT JOIN messages m ON m.session_id = s.id
                    GROUP BY
                        s.id, s.signaling_session_id, s.started_at, s.ended_at,
                        s.status, s.title, s.model, s.voice, s.language,
                        s.message_count, s.memory_count
                    ORDER BY s.started_at DESC
                    LIMIT ?
                    """,
                    [limit],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not list sessions")
                return []

    # ------------------------------------------------------------------ messages

    async def add_message(
        self,
        session_id: str,
        role: str,
        text: str,
        turn_index: int,
    ) -> str | None:
        message_id = _new_id()
        if not self._available:
            return None
        async with self._lock:
            try:
                self._execute(
                    """
                    INSERT INTO messages (id, session_id, role, text, created_at, turn_index)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [message_id, session_id, role, text, _utcnow(), turn_index],
                )
                self._execute(
                    """
                    UPDATE sessions
                    SET message_count = message_count + 1,
                        title = COALESCE(title, CASE WHEN ? = 'user' THEN ? ELSE NULL END)
                    WHERE id = ?
                    """,
                    [role, text[:120], session_id],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not persist message")
                return None
        return message_id

    async def add_message_embedding(
        self,
        message_id: str,
        session_id: str,
        embedding: Sequence[float],
        model: str,
    ) -> None:
        if not self._available or not self._vectors_available:
            return
        async with self._lock:
            try:
                self._execute(
                    """
                    INSERT INTO message_embeddings (
                        id, message_id, session_id, model, embedding, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [_new_id(), message_id, session_id, model, list(embedding), _utcnow()],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not persist message embedding")

    async def get_session_messages(
        self, session_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        async with self._lock:
            try:
                return self._rows(
                    """
                    SELECT id, session_id, role, text, created_at, turn_index
                    FROM (
                        SELECT * FROM messages
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                    )
                    ORDER BY created_at ASC
                    """,
                    [session_id, limit],
                )
            except Exception as exc:
                logger.opt(exception=exc).error(f"Could not read messages for {session_id}")
                return []

    async def get_recent_messages(
        self,
        limit: int = 20,
        exclude_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        where = "WHERE session_id <> ?" if exclude_session_id else ""
        params: list[Any] = [exclude_session_id] if exclude_session_id else []
        params.append(limit)
        async with self._lock:
            try:
                return self._rows(
                    f"""
                    SELECT id, session_id, role, text, created_at, turn_index
                    FROM (
                        SELECT * FROM messages
                        {where}
                        ORDER BY created_at DESC
                        LIMIT ?
                    )
                    ORDER BY created_at ASC
                    """,
                    params,
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not read recent context")
                return []

    # ------------------------------------------------------------------- memories

    async def list_memories(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._available:
            return []
        async with self._lock:
            try:
                return self._rows(
                    """
                    SELECT id, content, category, confidence, source_session_id,
                           created_at, updated_at, last_used_at, use_count
                    FROM memories
                    WHERE is_active = TRUE
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    [limit],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not list memories")
                return []

    async def touch_memories(self, memory_ids: Iterable[str]) -> None:
        ids = list(memory_ids)
        if not self._available or not ids:
            return
        async with self._lock:
            try:
                for memory_id in ids:
                    self._execute(
                        """
                        UPDATE memories
                        SET last_used_at = ?, use_count = use_count + 1
                        WHERE id = ?
                        """,
                        [_utcnow(), memory_id],
                    )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not update memory usage")

    @staticmethod
    def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 1.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 1.0
        return max(0.0, min(2.0, 1.0 - dot / (left_norm * right_norm)))

    async def search_memories(
        self,
        embedding: Sequence[float] | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        if self._vectors_available and embedding is not None:
            vector_type = f"FLOAT[{self.embedding_dim}]"
            async with self._lock:
                try:
                    rows = self._rows(
                        f"""
                        SELECT m.id, m.content, m.category, m.confidence,
                               r.distance
                        FROM (
                            SELECT memory_id,
                                   array_cosine_distance(embedding, ?::{vector_type}) AS distance
                            FROM memory_embeddings
                            ORDER BY distance
                            LIMIT ?
                        ) r
                        JOIN memories m ON m.id = r.memory_id
                        WHERE m.is_active = TRUE
                        ORDER BY r.distance
                        """,
                        [list(embedding), limit],
                    )
                    if rows:
                        return rows
                except Exception as exc:
                    logger.opt(exception=exc).error("Vector memory search failed; using fallback")

            async with self._lock:
                try:
                    candidates = self._rows(
                        """
                        SELECT m.id, m.content, m.category, m.confidence, e.embedding
                        FROM memory_embeddings e
                        JOIN memories m ON m.id = e.memory_id
                        WHERE m.is_active = TRUE
                        """
                    )
                    scored = [
                        {
                            "id": row["id"],
                            "content": row["content"],
                            "category": row["category"],
                            "confidence": row["confidence"],
                            "distance": self._cosine_distance(embedding, row["embedding"]),
                        }
                        for row in candidates
                    ]
                    return sorted(scored, key=lambda row: row["distance"])[:limit]
                except Exception as exc:
                    logger.opt(exception=exc).error("Memory embedding fallback failed")

        if query and query.strip():
            async with self._lock:
                try:
                    return self._rows(
                        """
                        SELECT id, content, category, confidence, NULL::DOUBLE AS distance
                        FROM memories
                        WHERE is_active = TRUE AND lower(content) LIKE ?
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        [f"%{query.lower().strip()}%", limit],
                    )
                except Exception as exc:
                    logger.opt(exception=exc).error("Keyword memory search failed")
        return []

    async def search_messages(
        self,
        embedding: Sequence[float] | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        if self._vectors_available and embedding is not None:
            vector_type = f"FLOAT[{self.embedding_dim}]"
            async with self._lock:
                try:
                    rows = self._rows(
                        f"""
                        SELECT m.id, m.session_id, m.role, m.text, m.created_at,
                               r.distance
                        FROM (
                            SELECT message_id,
                                   array_cosine_distance(embedding, ?::{vector_type}) AS distance
                            FROM message_embeddings
                            ORDER BY distance
                            LIMIT ?
                        ) r
                        JOIN messages m ON m.id = r.message_id
                        ORDER BY r.distance
                        """,
                        [list(embedding), limit],
                    )
                    if rows:
                        return rows
                except Exception as exc:
                    logger.opt(exception=exc).error("Vector message search failed; using fallback")

            async with self._lock:
                try:
                    candidates = self._rows(
                        """
                        SELECT m.id, m.session_id, m.role, m.text, m.created_at,
                               e.embedding
                        FROM message_embeddings e
                        JOIN messages m ON m.id = e.message_id
                        """
                    )
                    scored = [
                        {
                            "id": row["id"],
                            "session_id": row["session_id"],
                            "role": row["role"],
                            "text": row["text"],
                            "created_at": row["created_at"],
                            "distance": self._cosine_distance(embedding, row["embedding"]),
                        }
                        for row in candidates
                    ]
                    return sorted(scored, key=lambda row: row["distance"])[:limit]
                except Exception as exc:
                    logger.opt(exception=exc).error("Message embedding fallback failed")

        if query and query.strip():
            async with self._lock:
                try:
                    return self._rows(
                        """
                        SELECT id, session_id, role, text, created_at,
                               NULL::DOUBLE AS distance
                        FROM messages
                        WHERE lower(text) LIKE ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        [f"%{query.lower().strip()}%", limit],
                    )
                except Exception as exc:
                    logger.opt(exception=exc).error("Keyword message search failed")
        return []

    async def upsert_memory(
        self,
        content: str,
        category: str | None,
        confidence: float,
        source_session_id: str | None,
        embedding: Sequence[float] | None,
        model: str | None,
    ) -> str:
        content = " ".join(content.split())
        if not self._available:
            return _new_id()

        existing_id: str | None = None
        if embedding is not None and self._vectors_available:
            matches = await self.search_memories(embedding=embedding, limit=5)
            for match in matches:
                distance = match.get("distance")
                if distance is not None and isinstance(distance, (int, float)):
                    if (1.0 - float(distance)) >= self.dedupe_threshold:
                        existing_id = str(match["id"])
                        break
        if existing_id is None:
            async with self._lock:
                try:
                    row = self._rows(
                        "SELECT id FROM memories WHERE lower(content) = lower(?) LIMIT 1",
                        [content],
                    )
                    if row:
                        existing_id = str(row[0]["id"])
                except Exception as exc:
                    logger.opt(exception=exc).error("Memory duplicate check failed")

        memory_id = existing_id or _new_id()
        now = _utcnow()
        async with self._lock:
            try:
                if existing_id:
                    self._execute(
                        """
                        UPDATE memories
                        SET content = ?, category = COALESCE(?, category),
                            confidence = GREATEST(confidence, ?), updated_at = ?,
                            is_active = TRUE
                        WHERE id = ?
                        """,
                        [content, category, confidence, now, memory_id],
                    )
                else:
                    self._execute(
                        """
                        INSERT INTO memories (
                            id, content, category, confidence, source_session_id,
                            created_at, updated_at, last_used_at, use_count, is_active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, TRUE)
                        """,
                        [
                            memory_id,
                            content,
                            category,
                            confidence,
                            source_session_id,
                            now,
                            now,
                            now,
                        ],
                    )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not save memory")
                return memory_id

        if embedding is not None and model and self._vectors_available:
            async with self._lock:
                try:
                    self._execute(
                        "DELETE FROM memory_embeddings WHERE memory_id = ?",
                        [memory_id],
                    )
                    self._execute(
                        """
                        INSERT INTO memory_embeddings (
                            id, memory_id, model, embedding, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [memory_id, memory_id, model, list(embedding), now],
                    )
                except Exception as exc:
                    logger.opt(exception=exc).error("Could not save memory embedding")
        return memory_id

    # --------------------------------------------------------------------- errors

    async def log_error(
        self,
        source: str,
        level: str,
        message: str,
        stack: str | None = None,
        context: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        safe_message = _redact(message)[:2000]
        safe_stack = _redact(stack) if stack else None
        context_json = _json(context)
        safe_context = _redact(context_json) if context_json else None
        logger.error(f"[{source}] {safe_message}")
        if not self._available:
            return
        async with self._lock:
            try:
                self._execute(
                    """
                    INSERT INTO app_errors (
                        id, occurred_at, source, level, message, stack, context, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        _new_id(),
                        _utcnow(),
                        source[:80],
                        level[:20],
                        safe_message,
                        safe_stack,
                        safe_context,
                        session_id,
                    ],
                )
            except Exception as exc:
                logger.opt(exception=exc).error("Could not persist error log")


store = VoiceStore()
