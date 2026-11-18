"""NEW (glue): build and run one Gemini Live voice session.

Uses the vendored Pipecat Gemini Live speech-to-speech service directly. The
service handles input transcription, conversation and speech synthesis, so no
separate STT/TTS processors or external proxy/config layers are needed.
"""

import bootstrap  # noqa: F401  (puts the vendored Pipecat package on sys.path)

import asyncio
import json
import os
import re
import traceback
from typing import Any

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    FunctionCallResultProperties,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    LLMRunFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TranscriptionFrame,
    UserIdleTimeoutUpdateFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from services.pipecat.audio_config import AudioConfig
from services.pipecat.pipeline_builder import build_realtime_pipeline
from services.pipecat.transport_setup import create_webrtc_transport
from services.pipecat.worker_runner import run_pipeline_worker
from services.pipecat.ws_sender_registry import get_ws_sender
from storage import embeddings as embedding_service
from storage.duckdb_store import store

SYSTEM_PROMPT = """You are a friendly, helpful voice assistant on a live audio call.

SPEAKING STYLE
- Your words are spoken aloud. Use plain spoken language: no Markdown, lists, bullets, headings, emoji, code, URLs, or special characters. Say numbers and dates the way people say them.
- Answer directly. Usually two to four short sentences; longer only when the question truly needs an explanation. Do not repeat the question back, narrate your reasoning, or say you are an AI.
- If a request is unclear, say so briefly and ask one clarifying question instead of guessing.
- Stay in the user's language. Be warm and conversational. Let the user lead after the greeting.

INTERRUPTIONS
- The user may cut you off mid-sentence. When that happens, drop what you were saying completely and respond only to their new message.
- Never say "as I was saying" and never resume or repeat the interrupted answer unless the user asks you to.
- Short acknowledgements from the user such as "yeah", "okay", "uh-huh" are not questions. Do not treat them as new requests; continue naturally or pause for their next message.
- If the user's message is cut off, empty, or unclear, ask them once to repeat it briefly.

ENDING THE CONVERSATION
End the call only when one of these is clear:
- The user says goodbye or signals they are done ("bye", "that's all", "thanks, that's it", "I'm good").
- The user asks you to end the call or stop.
- The user's need is fully resolved AND they confirm they need nothing else.
Do not end the call just because the user says "thanks" or pauses; if unsure, ask "Is there anything else I can help with?" once.
To end: say one short, warm goodbye sentence, then call the end_conversation tool in the same turn. Never call the tool without speaking a goodbye first, and never keep talking after calling it.
If the user has been silent for a long time, ask once if they are still there; if there is still no reply, say goodbye and call end_conversation.
After the call ends, do not generate any more output."""

MEMORY_EXTRACTION_PROMPT = """You distill durable facts from a voice-assistant conversation.

Return ONLY a JSON array. Each item must be an object with:
- "content": one short standalone sentence written in third person (start with "User ...")
- "category": one of identity, preference, relationship, work, health, note
- "confidence": a number from 0.5 to 0.99

Rules:
- Store only facts that will still be useful in a future conversation: name, preferences,
  recurring routines, important people, pets, work, location, goals, constraints.
- Never store greetings, small talk, questions, assistant claims, secrets, passwords,
  one-off tasks, or anything the user did not state clearly.
- Prefer 0 to 5 facts. If there is nothing durable, return [].
- Do not add commentary before or after the JSON."""


def _redact(text: str) -> str:
    """Remove the API key from anything that may reach an error log."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return text.replace(key, "***REDACTED***") if key else text


async def _record_error(
    source: str,
    message: str,
    exc: Exception | None = None,
    context: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Log an error to stderr, logs/errors.log and the DuckDB app_errors table."""
    safe_message = _redact(message)
    stack: str | None = None
    if exc is not None:
        stack = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    if stack:
        safe_stack: str | None = _redact(stack)
        logger.error(f"[{source}] {safe_message}\n{safe_stack}")
    else:
        safe_stack = None
        logger.error(f"[{source}] {safe_message}")

    try:
        await store.log_error(
            source=source,
            level="ERROR",
            message=safe_message,
            stack=safe_stack,
            context=context,
            session_id=session_id,
        )
    except Exception as db_error:
        logger.error(f"Could not persist error log row: {db_error}")


def _short(text: str, limit: int = 400) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else f"{clean[: limit - 1]}…"


def _format_system_instruction(
    memories: list[dict[str, Any]], turns: list[dict[str, Any]]
) -> str:
    parts = [SYSTEM_PROMPT]

    if memories:
        parts.append(
            "\n\nPERSISTENT CONTEXT ABOUT THE USER\n"
            "Use these facts naturally when relevant. Never say that you retrieved, "
            "stored, or remember them, and never list them unless asked."
        )
        for memory in memories[:20]:
            content = _short(str(memory.get("content", "")), 300)
            if content:
                parts.append(f"- {content}")

    if turns:
        parts.append(
            "\n\nRECENT EARLIER CONVERSATION (for continuity only; do not repeat it verbatim)"
        )
        for turn in turns[-20:]:
            role = "User" if turn.get("role") == "user" else "Assistant"
            text = _short(str(turn.get("text", "")), 350)
            if text:
                parts.append(f"{role}: {text}")

    return "\n".join(parts)


async def build_system_instruction(exclude_session_id: str) -> str:
    """Blend the base prompt with distilled memory and recent prior turns."""
    memories: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    try:
        memory_limit = int(os.getenv("MEMORY_CONTEXT_LIMIT", "20"))
        turn_limit = int(os.getenv("CONTEXT_TURN_LIMIT", "20"))

        memories = await store.list_memories(limit=memory_limit)
        if len(memories) > 8:
            query = " ".join(str(item.get("content", "")) for item in memories[-8:])
            embedding = await embedding_service.embedder.embed_text(query)
            if embedding:
                semantic = await store.search_memories(embedding=embedding, limit=memory_limit)
                merged: dict[str, dict[str, Any]] = {
                    str(item["id"]): item for item in semantic
                }
                for item in memories:
                    merged.setdefault(str(item["id"]), item)
                memories = list(merged.values())[:memory_limit]

        if memories:
            await store.touch_memories([str(item["id"]) for item in memories])

        turns = await store.get_recent_messages(
            limit=turn_limit, exclude_session_id=exclude_session_id
        )
    except Exception as exc:
        await _record_error(
            "context-builder",
            f"Could not load assistant context: {exc}",
            exc=exc,
            session_id=exclude_session_id,
        )
        memories, turns = [], []

    return _format_system_instruction(memories, turns)


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start < 0 or end <= start:
        return []

    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    facts: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            content = str(item.get("content") or item.get("fact") or "").strip()
            category = str(item.get("category") or "note").strip().lower()
            try:
                confidence = float(item.get("confidence", 0.75))
            except (TypeError, ValueError):
                confidence = 0.75
        elif isinstance(item, str):
            content, category, confidence = item.strip(), "note", 0.7
        else:
            continue

        if content:
            facts.append(
                {
                    "content": _short(content, 300),
                    "category": category[:40],
                    "confidence": max(0.5, min(0.99, confidence)),
                }
            )
    return facts


def _heuristic_facts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Local fallback when the Gemini memory-extraction call is unavailable."""
    patterns = (
        (r"\bmy name is\s+([^.!?]+)", "identity", "User's name is {value}"),
        (r"\bi (?:like|love|enjoy|prefer)\s+([^.!?]+)", "preference", "User likes {value}"),
        (r"\bremember(?: that)?\s+([^.!?]+)", "note", "User asked to remember: {value}"),
        (r"\bi (?:work|am working)\s+(?:as|at|on)\s+([^.!?]+)", "work", "User works on {value}"),
        (r"\bmy (?:favorite|favourite)\s+([^.!?]+)", "preference", "User's favorite {value}"),
    )
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for message in messages:
        text = str(message.get("text", ""))
        claimed_spans: list[tuple[int, int]] = []
        for pattern, category, template in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if any(
                    not (match.end() <= start or match.start() >= end)
                    for start, end in claimed_spans
                ):
                    continue
                claimed_spans.append(match.span())
                value = _short(match.group(1), 120)
                if len(value) < 2:
                    continue
                content = template.format(value=value.rstrip(" ."))
                key = content.lower()
                if key not in seen:
                    seen.add(key)
                    facts.append({"content": content, "category": category, "confidence": 0.7})
                if len(facts) >= 5:
                    return facts
    return facts


async def _extract_with_gemini(transcript: str) -> list[dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return []

    model = os.getenv("MEMORY_MODEL", "gemini-flash-lite-latest")
    timeout = float(os.getenv("MEMORY_EXTRACTION_TIMEOUT_SECONDS", "15"))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": MEMORY_EXTRACTION_PROMPT}]},
                    "contents": [
                        {"role": "user", "parts": [{"text": f"Conversation:\n{transcript}"}]}
                    ],
                    "generationConfig": {
                        "temperature": 0,
                        "responseMimeType": "application/json",
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            candidates = payload.get("candidates") or []
            if not candidates:
                return []
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
            return _extract_json_array(text)
    except Exception as exc:
        await _record_error(
            "memory-extraction",
            f"Gemini memory extraction failed: {exc}",
            exc=exc,
        )
        return []


async def extract_and_store_memories(session_id: str) -> int:
    """Best-effort memory distillation after a call. Never break teardown."""
    try:
        messages = await store.get_session_messages(session_id, limit=200)
        user_turns = [
            message
            for message in messages
            if message.get("role") == "user" and str(message.get("text", "")).strip()
        ]
        if not user_turns:
            return 0

        transcript = "\n".join(
            f"{'User' if item.get('role') == 'user' else 'Assistant'}: "
            f"{_short(str(item.get('text', '')), 500)}"
            for item in messages
        )
        facts = await _extract_with_gemini(transcript)
        if not facts:
            facts = _heuristic_facts(user_turns)
        if not facts:
            return 0

        embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        saved = 0
        for fact in facts[:5]:
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            embedding = await embedding_service.embedder.embed_text(content)
            await store.upsert_memory(
                content=content,
                category=str(fact.get("category") or "note"),
                confidence=float(fact.get("confidence") or 0.7),
                source_session_id=session_id,
                embedding=embedding,
                model=embed_model,
            )
            saved += 1
        return saved
    except Exception as exc:
        await _record_error(
            "memory-extraction",
            f"Could not save memories for session {session_id}: {exc}",
            exc=exc,
            session_id=session_id,
        )
        return 0


class IdleHandler:
    """Two-stage silence handling with one prompt and one goodbye.

    The handler only queues frames through a local PipelineWorker.
    """

    def __init__(self, prompt_seconds: float, end_seconds: float):
        self._prompt_seconds = prompt_seconds
        self._end_seconds = end_seconds
        self._retry_count = 0
        self._ending = False
        self._pending_end = False
        self._worker: PipelineWorker | None = None

    def attach_worker(self, worker: PipelineWorker) -> None:
        self._worker = worker

    def reset(self) -> None:
        if self._ending:
            return
        self._retry_count = 0

    async def reset_async(self) -> None:
        """Reset on real user activity and restore the shorter prompt timeout."""
        self.reset()
        if self._worker is not None and not self._ending:
            await self._worker.queue_frame(
                UserIdleTimeoutUpdateFrame(self._prompt_seconds)
            )

    async def handle_idle(self, aggregator) -> None:
        if self._ending:
            return

        self._retry_count += 1
        logger.info(f"User idle; prompt attempt {self._retry_count}")

        if self._retry_count == 1:
            message = {
                "role": "user",
                "content": (
                    "The user has been quiet. Briefly ask if they are still there in "
                    "the language they have been speaking."
                ),
            }
            await aggregator.push_frame(
                LLMMessagesAppendFrame([message], run_llm=True)
            )
            if self._worker is not None:
                await self._worker.queue_frame(
                    UserIdleTimeoutUpdateFrame(self._end_seconds)
                )
            return

        self._ending = True
        self._pending_end = True
        message = {
            "role": "user",
            "content": (
                "The user is still quiet. Say one short, warm goodbye in the language "
                "they have been speaking and then stop."
            ),
        }
        await aggregator.push_frame(LLMMessagesAppendFrame([message], run_llm=True))
        # The terminal frame is queued only after the goodbye audio has played.

    async def on_goodbye_finished(self) -> None:
        """End the call after the idle goodbye turn has finished."""
        if not self._pending_end or self._worker is None:
            return
        self._pending_end = False
        await self._worker.queue_frame(
            EndFrame(reason="user_idle_max_duration_exceeded")
        )


class TranscriptBridge(BaseObserver):
    """Forward transcript and speaking-state frames to the browser.

    Gemini Live pushes user transcriptions upstream and assistant text/audio
    lifecycle frames downstream. A pipeline observer sees both directions
    without changing frame flow, so no processor needs to be inserted into the
    pipeline for UI reporting.
    """

    _TRACKED_FRAMES = (
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
        InterruptionFrame,
        TranscriptionFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        LLMFullResponseEndFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
    )

    def __init__(
        self,
        session_id: int,
        on_user_activity=None,
        on_goodbye_finished=None,
        on_transcript=None,
    ):
        super().__init__()
        self._session_id = session_id
        self._on_user_activity = on_user_activity
        self._on_goodbye_finished = on_goodbye_finished
        self._on_transcript = on_transcript
        self._bot_text = ""
        self._bot_text_emitted = False
        self._current_state: str | None = None
        self._seen_frame_ids: set[int] = set()

    async def _emit(self, message: dict) -> None:
        sender = get_ws_sender(self._session_id)
        if sender is not None:
            try:
                await sender(message)
            except Exception as e:
                logger.debug(f"Could not send browser event: {e}")

    async def _set_state(self, state: str) -> None:
        if state == self._current_state:
            return
        self._current_state = state
        await self._emit({"type": "state", "payload": {"state": state}})

    def _reset_bot_text(self) -> None:
        self._bot_text = ""
        self._bot_text_emitted = False

    async def _emit_bot_text(self) -> None:
        if self._bot_text_emitted or not self._bot_text.strip():
            return
        text = self._bot_text.strip()
        await self._emit(
            {
                "type": "transcript",
                "payload": {
                    "role": "assistant",
                    "text": text,
                    "final": True,
                },
            }
        )
        self._bot_text_emitted = True
        if self._on_transcript is not None:
            await self._on_transcript("assistant", text)

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, self._TRACKED_FRAMES):
            return

        # The same frame is observed once per processor handoff. Deduplicate by
        # object identity so text and state events are emitted only once.
        frame_id = id(frame)
        if frame_id in self._seen_frame_ids:
            return
        self._seen_frame_ids.add(frame_id)

        if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            await self._set_state("listening")
        elif isinstance(frame, InterruptionFrame):
            self._reset_bot_text()
            await self._set_state("listening")
        elif isinstance(frame, TranscriptionFrame):
            # Gemini Live pushes completed user transcriptions only.
            is_final = True
            await self._emit(
                {
                    "type": "transcript",
                    "payload": {
                        "role": "user",
                        "text": frame.text,
                        "final": is_final,
                    },
                }
            )
            if frame.text.strip():
                if self._on_user_activity is not None:
                    await self._on_user_activity()
                if self._on_transcript is not None:
                    await self._on_transcript("user", frame.text)
            if is_final:
                await self._set_state("thinking")
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._reset_bot_text()
            if self._current_state != "speaking":
                await self._set_state("thinking")
        elif isinstance(frame, LLMTextFrame):
            self._bot_text += frame.text
            if self._current_state != "speaking":
                await self._set_state("thinking")
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._emit_bot_text()
        elif isinstance(frame, (TTSStartedFrame, BotStartedSpeakingFrame)):
            await self._set_state("speaking")
        elif isinstance(frame, (TTSStoppedFrame, BotStoppedSpeakingFrame)):
            await self._emit_bot_text()
            await self._set_state("listening")
            if self._on_goodbye_finished is not None:
                await self._on_goodbye_finished()


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY; set it in .env (see .env.example)")
    return key


def create_end_conversation_handler(worker_getter):
    """Return the end_conversation tool handler for one session."""

    async def end_conversation(params: FunctionCallParams) -> None:
        logger.info("end_conversation tool called")
        await params.result_callback(
            {"status": "success", "action": "ending_call"},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        worker = worker_getter()
        if worker is None:
            raise RuntimeError("Pipeline worker is not ready")
        # Gemini Live defers EndFrame until the current bot turn finishes, so the
        # goodbye audio is fully played before the transport disconnects.
        await worker.queue_frame(EndFrame(reason="end_call_tool"))

    return end_conversation


async def start_pipeline(
    webrtc_connection: SmallWebRTCConnection,
    session_id: int,
    call_context_vars: dict | None = None,
) -> None:
    """Create the Gemini Live service and run one persistent voice session."""
    logger.info(f"Starting voice session {session_id}")

    model_name = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
    voice_name = os.getenv("GEMINI_VOICE", "Puck")
    language_name = os.getenv("GEMINI_LANGUAGE", "en-US")
    session_row_id = await store.create_session(
        signaling_session_id=session_id,
        model=model_name,
        voice=voice_name,
        language=language_name,
        metadata={"context_keys": sorted((call_context_vars or {}).keys())},
    )

    session_status = "ended"
    persistence_tasks: set[asyncio.Task] = set()
    turn_counter = 0
    worker: PipelineWorker | None = None

    def schedule(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        persistence_tasks.add(task)
        task.add_done_callback(persistence_tasks.discard)
        return task

    async def persist_transcript(role: str, text: str) -> None:
        nonlocal turn_counter
        clean = text.strip()
        if not clean:
            return
        turn_counter += 1
        try:
            message_id = await store.add_message(
                session_row_id, role, clean, turn_counter
            )
            if message_id:
                embedding = await embedding_service.embedder.embed_text(
                    f"{role}: {clean}"
                )
                if embedding:
                    await store.add_message_embedding(
                        message_id,
                        session_row_id,
                        embedding,
                        os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
                    )
        except Exception as exc:
            await _record_error(
                "transcript-persistence",
                f"Could not persist {role} transcript: {exc}",
                exc=exc,
                context={"role": role, "text": _short(clean, 200)},
                session_id=session_row_id,
            )

    async def queue_transcript(role: str, text: str) -> None:
        schedule(persist_transcript(role, text))

    async def notify_error(
        message: str, exc: Exception | None = None, context: dict[str, Any] | None = None
    ) -> None:
        await _record_error(
            "voice-session",
            message,
            exc=exc,
            context=context,
            session_id=session_row_id,
        )
        sender = get_ws_sender(session_id)
        if sender is not None:
            try:
                await sender({"type": "error", "payload": {"message": message}})
            except Exception as send_error:
                logger.debug(f"Could not send setup error: {send_error}")

    idle_prompt_seconds = float(os.getenv("IDLE_PROMPT_SECONDS", "15"))
    idle_end_seconds = float(os.getenv("IDLE_END_SECONDS", "10"))

    end_conversation_tool = FunctionSchema(
        name="end_conversation",
        description=(
            "End the voice call after the assistant has spoken a short goodbye."
        ),
        properties={},
        required=[],
    )

    end_conversation = create_end_conversation_handler(lambda: worker)
    idle_handler = IdleHandler(idle_prompt_seconds, idle_end_seconds)

    try:
        audio_config = AudioConfig(
            transport_in_sample_rate=16000,
            transport_out_sample_rate=24000,
            vad_sample_rate=16000,
            pipeline_sample_rate=16000,
        )

        transport = await create_webrtc_transport(
            webrtc_connection, audio_config, is_realtime=True
        )

        system_instruction = await build_system_instruction(session_row_id)
        llm = GeminiLiveLLMService(
            api_key=_api_key(),
            tools=[end_conversation_tool],
            settings=GeminiLiveLLMService.Settings(
                model=model_name,
                voice=voice_name,
                language=language_name,
                system_instruction=system_instruction,
            ),
        )
        llm.register_function("end_conversation", end_conversation)

        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_idle_timeout=idle_prompt_seconds
            ),
        )

        @user_aggregator.event_handler("on_user_turn_idle")
        async def on_user_turn_idle(aggregator):
            await idle_handler.handle_idle(aggregator)

        @user_aggregator.event_handler("on_user_turn_started")
        async def on_user_turn_started(aggregator, *_):
            await idle_handler.reset_async()

        pipeline = build_realtime_pipeline(
            transport,
            llm,
            user_aggregator,
            assistant_aggregator,
        )

        worker = PipelineWorker(
            pipeline,
            observers=[
                TranscriptBridge(
                    session_id,
                    on_user_activity=idle_handler.reset_async,
                    on_goodbye_finished=idle_handler.on_goodbye_finished,
                    on_transcript=queue_transcript,
                )
            ],
            params=PipelineParams(
                audio_in_sample_rate=audio_config.transport_in_sample_rate,
                audio_out_sample_rate=audio_config.transport_out_sample_rate,
                enable_metrics=False,
                enable_usage_metrics=False,
                enable_heartbeats=True,
            ),
            enable_tracing=False,
            conversation_id=f"voice-session-{session_row_id}",
        )
        idle_handler.attach_worker(worker)

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info("Client connected; starting conversation")
            context.add_message(
                {
                    "role": "user",
                    "content": "Greet the user briefly and ask how you can help.",
                }
            )
            await worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            logger.info("Client disconnected; ending conversation")
            await worker.cancel()

        try:
            await run_pipeline_worker(worker, auto_end=True)
        except asyncio.CancelledError:
            logger.info(f"Voice session {session_id} cancelled")
    except asyncio.CancelledError:
        logger.info(f"Voice session {session_id} cancelled during setup")
    except Exception as exc:
        session_status = "error"
        await notify_error(
            f"Voice session {session_id} failed: {exc}",
            exc=exc,
            context={"phase": "setup_or_run", "signaling_session_id": session_id},
        )
        try:
            await webrtc_connection.disconnect()
        except Exception as disconnect_error:
            logger.debug(f"Failed to disconnect after session error: {disconnect_error}")
    finally:
        logger.info(f"Voice session {session_id} finished")
        if persistence_tasks:
            await asyncio.gather(*list(persistence_tasks), return_exceptions=True)
        memory_count = await extract_and_store_memories(session_row_id)
        await store.finish_session(session_row_id, session_status, memory_count)
