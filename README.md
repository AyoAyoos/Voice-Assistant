# Voice Assistant

A standalone, local-first browser voice assistant. Microphone audio travels over WebRTC to a small FastAPI server, a vendored Pipecat pipeline streams it to **Gemini Live**, and Gemini's spoken reply is streamed back to the browser. Gemini Live handles speech recognition, conversation, and speech synthesis in one speech-to-speech session. Calls, transcripts, distilled memory, and error logs are persisted locally in DuckDB; embeddings come from local Ollama.

This folder is self-contained. No files in the upstream source repository were modified. Only the two BSD-2-Clause `LICENSE` files and the preserved per-file copyright headers carry upstream attribution.

## Features at a glance

- **One hotkey, zero clicks:** `Ctrl + ;` rebuilds the UI, starts the backend, opens a compact always-on-top panel, and begins the call automatically. Press again to stop.
- **Natural speech-to-speech:** Gemini Live owns STT, LLM and TTS, including barge-in interruption and a graceful spoken goodbye before disconnect.
- **Memory across calls:** durable facts are distilled after each call, embedded locally and injected into the next session's prompt, with recent turns kept for continuity.
- **Local storage and search:** one DuckDB file holds sessions, transcripts, vectors and errors; HNSW cosine search falls back to exact/keyword search when Ollama or `vss` is unavailable.
- **Full-page UI:** neubrutalist React interface with live status, waveform, transcript, and a History & Memory drawer.
- **Observable:** structured ERROR logs, a DuckDB error table, launcher logs, and browser error reporting through one endpoint.

## Documentation

| Document | What it covers |
| --- | --- |
| [SETUP.md](SETUP.md) | Step-by-step installation and first run on Windows |
| [STORAGE_AND_MEMORY.md](STORAGE_AND_MEMORY.md) | Database schema, HNSW indexes, context/memory flow, error logs |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Hotkey, microphone, port, DuckDB, Ollama and Gemini diagnostics |
| [VOICE_ASSISTANT_UI_SPEC_REACT.md](VOICE_ASSISTANT_UI_SPEC_REACT.md) | Original UI behaviour and layout specification |
| This README | Architecture, configuration, file map and verification notes |

**New here?** Start with [SETUP.md](SETUP.md), then come back for the architecture below.

## Architecture

```mermaid
flowchart LR
    Browser["Browser page<br/>web/src React UI<br/>getUserMedia + RTCPeerConnection"]
    WS["FastAPI signaling<br/>routes/webrtc_signaling.py<br/>/ws/signaling/{session_id}"]
    PC["SmallWebRTCConnection<br/>vendored Pipecat + aiortc<br/>STUN only"]
    Transport["SmallWebRTCTransport<br/>16 kHz in / 24 kHz out"]
    Pipeline["PipelineWorker<br/>vendored Pipecat"]
    Observer["TranscriptBridge<br/>pipeline observer<br/>state + transcript events"]
    Gemini["Gemini Live API<br/>speech-to-speech<br/>server-side VAD"]
    DB[("DuckDB<br/>sessions / messages / memory<br/>HNSW + app_errors")]
    Ollama["Local Ollama<br/>nomic-embed-text<br/>768-dim embeddings"]

    Browser -- "SDP offer + ICE candidates" --> WS
    WS -- "SDP answer" --> Browser
    WS -- "owns" --> PC
    Browser -- "microphone audio track" --> PC
    PC --> Transport --> Pipeline
    Pipeline -- "audio stream" --> Gemini
    Gemini -- "audio stream + transcript frames" --> Pipeline
    Pipeline -- "reply audio track" --> PC
    PC -- "reply audio" --> Browser
    Pipeline -. "frame events" .-> Observer
    Observer -- "WebSocket state / transcript" --> WS
    WS -- "state / transcript" --> Browser
    Pipeline -- "context + transcripts + errors" --> DB
    DB -- "recent turns + distilled memory" --> Pipeline
    Pipeline -- "local vectors" --> Ollama
```

Active pipeline frame order:

```text
transport.input()
  -> user context aggregator
  -> GeminiLiveLLMService      (STT + LLM + TTS, server-side VAD)
  -> transport.output()
  -> assistant context aggregator
```

`TranscriptBridge` is attached to `PipelineWorker` as an observer, not inserted into the chain. That lets it see both upstream user `TranscriptionFrame`s from Gemini Live and downstream assistant text/speaking frames without changing frame flow.

The browser state badge follows these events:

| Frame(s) | UI state |
| --- | --- |
| User started/stopped speaking, final user transcript | `listening` |
| Final user transcript, LLM response start | `thinking` |
| Gemini audio starts / bot starts speaking | `speaking` |
| Gemini audio stops / bot stops speaking | `listening` |
| User interrupts / bot output is flushed | `listening` |
| Call ends | `Call ended` |

## Gemini Live behavior

Gemini Live performs speech recognition (STT), conversation (LLM), and speech synthesis (TTS) inside one speech-to-speech model. There are no separate STT or TTS processors in the pipeline; input and output audio transcription are enabled by the vendored service, and `TranscriptBridge` forwards both user and assistant transcript text to the browser.

- **Interruption:** The model's server-side barge-in emits an interruption event; Pipecat flushes queued bot audio and stops the response. The UI switches from `speaking` to `listening`, and the old answer is not resumed.
- **Ending a call:** The `end_conversation` tool is registered on `GeminiLiveLLMService`. After the model says its goodbye, the tool result uses `run_llm=False` and queues a graceful `EndFrame`. Gemini Live defers that frame until the current bot turn completes, so the goodbye audio finishes before the peer connection closes and the browser receives `call-ended`.
- **Idle/silence:** After `IDLE_PROMPT_SECONDS` (default 15) of silence, the assistant asks once whether the user is still there. After `IDLE_END_SECONDS` (default 10) more, it says goodbye and ends the call. Real user transcript activity resets the counter and restores the first timeout.

## Setup and run

See [SETUP.md](SETUP.md) for the full guided install. In short: Python **3.11 or 3.12** is required (the current environment uses Python 3.12.6). Local persistence requires DuckDB (installed from `requirements.txt`), and local embeddings require Ollama with `nomic-embed-text` running at `http://127.0.0.1:11434`.

```powershell
cd D:\voice-assistant
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# Edit .env and set GEMINI_API_KEY=...
.\.venv\Scripts\python.exe app.py
```

If Ollama is not already running, start it and pull the embedding model once:

```powershell
ollama pull nomic-embed-text
```

See [STORAGE_AND_MEMORY.md](STORAGE_AND_MEMORY.md) for the database schema,
HNSW indexes, memory flow, and error-log details.

For the local MVP, `.env` is populated with a working key. Treat it as a secret: rotate it when needed, and never commit it. Only `.env.example` is tracked.

Open <http://127.0.0.1:8000/>, click **Start**, and allow microphone access. Localhost is a secure browser context, so WebRTC microphone access works without HTTPS. Stop the server with `Ctrl+C`.

### Frontend (React + TypeScript)

The browser UI is a Vite/React app in `web/`. Node 20+ is required.

```powershell
cd D:\voice-assistant\web
npm install
npm run dev
```

Open <http://127.0.0.1:5173/> while the Python server is running on port 8000. Vite proxies `/ws` to the FastAPI signaling endpoint, so signaling works from the dev server. `npm run build` writes the production bundle to `web/dist`; restart `app.py` after the first build so FastAPI switches from the unbundled `web/` folder to `web/dist`.

UI code lives in `web/src/features/voice-assistant/`: `useVoiceSession.ts` contains the browser WebRTC/signaling logic, and the `components/` folder holds the presentational neubrutalist components, the history drawer, and error reporting. The Python pipeline now adds persistence, context injection, and memory extraction around the unchanged WebRTC/Gemini Live audio path.

### One-command hotkey (Windows)

Press **Ctrl + ;** anywhere on the laptop to toggle the assistant:

- **First press:** rebuilds the React app, starts the backend, opens a small always-on-top panel in the bottom-right corner at `http://127.0.0.1:8000/?compact=1&autostart=1`, and **starts the voice call automatically**.
- **Second press:** closes the panel and stops the backend.

The rebuild-on-start means UI changes are picked up on the next press. The compact panel also has an **Expand ↗** button that opens the full page. The launcher uses a dedicated browser profile and passes autoplay/microphone flags so the hotkey can start audio without a manual click.

Setup is handled by `scripts/voice.ps1` (launcher) and `scripts/voice-hotkey.ahk` (AutoHotkey v2 hotkey). A portable AutoHotkey binary lives in `tools/autohotkey/` and a shortcut in the Windows Startup folder loads the hotkey at login. After editing the `.ahk` script, use the tray icon → **Reload Script**. Logs: `.voice-hotkey.log`, `.voice-backend.log`, `.voice-backend.err.log`.

You can also drive the launcher directly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Toggle
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Status
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | required | Gemini API key used only by the Live session |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Live API model endpoint |
| `GEMINI_VOICE` | `Puck` | Gemini Live voice |
| `GEMINI_LANGUAGE` | `en-US` | Language hint for transcription |
| `IDLE_PROMPT_SECONDS` | `15` | Silence before the one-time idle prompt |
| `IDLE_END_SECONDS` | `10` | Silence after the prompt before goodbye/end |
| `HOST` | `127.0.0.1` | Bind host |
| `PORT` | `8000` | Bind port |
| `VOICE_DB_PATH` | `data/voice_assistant.duckdb` | Local DuckDB file |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama service |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Local embedding model |
| `EMBEDDING_DIM` | `768` | Embedding vector width |
| `MEMORY_MODEL` | `gemini-flash-lite-latest` | Post-call fact extraction model |
| `MEMORY_CONTEXT_LIMIT` | `20` | Memories injected into each call |
| `CONTEXT_TURN_LIMIT` | `20` | Older turns injected into each call |

Supported model IDs are listed in `.env.example` and `.env`:

```text
gemini-3-flash-preview
gemini-3.1-flash-live-preview
gemini-3.5-live-translate-preview
gemini-3.5-transcribe-live
gemini-3.8-live
gemini-3.8-live-extended-thinking
```

Change `GEMINI_LIVE_MODEL` to switch models. Actual availability depends on the Gemini project and region attached to the key.

Health check: `GET /health` returns `{"status":"ok"}`.

## What was removed

No auth, organizations/workspaces, workflows, campaigns, phone-call subsystems, migrations, billing, recording, external proxy/telemetry services, or product branding are present. A local DuckDB database and local Ollama embeddings are present. The outbound services are the Gemini Live API, the optional Gemini text call for memory distillation, and the public Google STUN server used for WebRTC candidate discovery.

## File map

### NEW files written for this standalone app

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI entrypoint, static UI mount, health/status APIs, error sink, storage lifespan |
| `bootstrap.py` | Puts `pipecat/src` on `sys.path` for the vendored package |
| `pipeline.py` | Gemini Live session, context injection, transcripts, memory extraction, `start_pipeline()` |
| `storage/duckdb_store.py` | DuckDB schema, sessions/messages/memory/errors, HNSW search and fallbacks |
| `storage/embeddings.py` | Local Ollama embedding client |
| `scripts/voice.ps1` | Hotkey launcher: rebuild, start/stop backend, autostart compact panel |
| `scripts/voice-hotkey.ahk` | AutoHotkey v2 `Ctrl + ;` toggle |
| `web/src/features/voice-assistant/**` | React/TypeScript UI, history drawer, error reporting |
| `STORAGE_AND_MEMORY.md` | Storage, HNSW, memory, and error-log reference |
| `TROUBLESHOOTING.md` | Hotkey, audio, DB, Ollama, and Gemini diagnostics |
| `.env.example` | Provider-key-only environment template |
| `.env` | Local populated environment (gitignored) |
| `.gitignore` | Ignores virtualenvs, build output, database, logs, browser profile |
| `requirements.txt` | Runtime dependency set including DuckDB |
| `README.md` | This document: architecture, configuration and verification notes |
| `SETUP.md` | Guided installation and first-run walkthrough |
| `services/__init__.py` | Package marker |
| `services/pipecat/__init__.py` | Package marker |

### Copied files

| Path | Status |
| --- | --- |
| `LICENSE` | Verbatim BSD-2-Clause license |
| `pipecat/LICENSE` | Verbatim BSD-2-Clause license |
| `pipecat/src/pipecat/**/*.py` | 144 vendored Pipecat Python files |
| `pipecat/src/pipecat/audio/vad/data/silero_vad.onnx` | Vendored Silero VAD model (part of the import closure, not used for user turns) |
| `pipecat/src/pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx` | Vendored local smart-turn model loaded by the aggregator |
| `services/pipecat/audio_config.py` | Copied audio config; edited |
| `services/pipecat/transport_params.py` | Copied tuning helper; comment wording edited |
| `services/pipecat/transport_setup.py` | Copied WebRTC transport factory; comment wording edited |
| `services/pipecat/pipeline_builder.py` | Copied pipeline assembly; edited |
| `services/pipecat/worker_runner.py` | Verbatim |
| `services/pipecat/ws_sender_registry.py` | Verbatim |
| `routes/webrtc_signaling.py` | Rewritten from the upstream signaling route |

<details>
<summary>Exact vendored Pipecat file list (144 Python files + 2 models)</summary>

```text
pipecat/src/pipecat/__init__.py
pipecat/src/pipecat/adapters/base_llm_adapter.py
pipecat/src/pipecat/adapters/schemas/direct_function.py
pipecat/src/pipecat/adapters/schemas/function_schema.py
pipecat/src/pipecat/adapters/schemas/tools_schema.py
pipecat/src/pipecat/adapters/services/gemini_adapter.py
pipecat/src/pipecat/adapters/services/open_ai_adapter.py
pipecat/src/pipecat/audio/dtmf/types.py
pipecat/src/pipecat/audio/dtmf/utils.py
pipecat/src/pipecat/audio/filters/base_audio_filter.py
pipecat/src/pipecat/audio/krisp_instance.py
pipecat/src/pipecat/audio/mixers/base_audio_mixer.py
pipecat/src/pipecat/audio/resamplers/base_audio_resampler.py
pipecat/src/pipecat/audio/resamplers/soxr_resampler.py
pipecat/src/pipecat/audio/resamplers/soxr_stream_resampler.py
pipecat/src/pipecat/audio/turn/base_turn_analyzer.py
pipecat/src/pipecat/audio/turn/smart_turn/_whisper_features.py
pipecat/src/pipecat/audio/turn/smart_turn/base_smart_turn.py
pipecat/src/pipecat/audio/turn/smart_turn/data/__init__.py
pipecat/src/pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx
pipecat/src/pipecat/audio/turn/smart_turn/local_smart_turn_v3.py
pipecat/src/pipecat/audio/utils.py
pipecat/src/pipecat/audio/vad/data/silero_vad.onnx
pipecat/src/pipecat/audio/vad/silero.py
pipecat/src/pipecat/audio/vad/vad_analyzer.py
pipecat/src/pipecat/audio/vad/vad_controller.py
pipecat/src/pipecat/bus/__init__.py
pipecat/src/pipecat/bus/bridge_processor.py
pipecat/src/pipecat/bus/bus.py
pipecat/src/pipecat/bus/local/__init__.py
pipecat/src/pipecat/bus/local/async_queue.py
pipecat/src/pipecat/bus/messages.py
pipecat/src/pipecat/bus/queue.py
pipecat/src/pipecat/bus/subscriber.py
pipecat/src/pipecat/bus/ui/messages.py
pipecat/src/pipecat/clocks/base_clock.py
pipecat/src/pipecat/clocks/system_clock.py
pipecat/src/pipecat/frames/frames.py
pipecat/src/pipecat/metrics/metrics.py
pipecat/src/pipecat/observers/base_observer.py
pipecat/src/pipecat/observers/turn_tracking_observer.py
pipecat/src/pipecat/observers/user_bot_latency_observer.py
pipecat/src/pipecat/pipeline/base_pipeline.py
pipecat/src/pipecat/pipeline/job_context.py
pipecat/src/pipecat/pipeline/job_decorator.py
pipecat/src/pipecat/pipeline/pipeline.py
pipecat/src/pipecat/pipeline/worker.py
pipecat/src/pipecat/pipeline/worker_observer.py
pipecat/src/pipecat/pipeline/worker_ready_decorator.py
pipecat/src/pipecat/processors/aggregators/__init__.py
pipecat/src/pipecat/processors/aggregators/async_tool_messages.py
pipecat/src/pipecat/processors/aggregators/llm_context.py
pipecat/src/pipecat/processors/aggregators/llm_context_summarizer.py
pipecat/src/pipecat/processors/aggregators/llm_response_universal.py
pipecat/src/pipecat/processors/frame_processor.py
pipecat/src/pipecat/processors/frameworks/rtvi/__init__.py
pipecat/src/pipecat/processors/frameworks/rtvi/frames.py
pipecat/src/pipecat/processors/frameworks/rtvi/models.py
pipecat/src/pipecat/processors/frameworks/rtvi/observer.py
pipecat/src/pipecat/processors/frameworks/rtvi/processor.py
pipecat/src/pipecat/processors/metrics/frame_processor_metrics.py
pipecat/src/pipecat/registry/__init__.py
pipecat/src/pipecat/registry/registry.py
pipecat/src/pipecat/registry/types.py
pipecat/src/pipecat/services/ai_service.py
pipecat/src/pipecat/services/google/frames.py
pipecat/src/pipecat/services/google/gemini_live/file_api.py
pipecat/src/pipecat/services/google/gemini_live/llm.py
pipecat/src/pipecat/services/google/utils.py
pipecat/src/pipecat/services/llm_service.py
pipecat/src/pipecat/services/openai/_constants.py
pipecat/src/pipecat/services/openai/base_llm.py
pipecat/src/pipecat/services/openai/llm.py
pipecat/src/pipecat/services/openai/stt.py
pipecat/src/pipecat/services/openai/tts.py
pipecat/src/pipecat/services/settings.py
pipecat/src/pipecat/services/stt_latency.py
pipecat/src/pipecat/services/stt_service.py
pipecat/src/pipecat/services/tts_service.py
pipecat/src/pipecat/services/websocket_service.py
pipecat/src/pipecat/services/whisper/base_stt.py
pipecat/src/pipecat/transcriptions/language.py
pipecat/src/pipecat/transports/base_input.py
pipecat/src/pipecat/transports/base_output.py
pipecat/src/pipecat/transports/base_transport.py
pipecat/src/pipecat/transports/smallwebrtc/connection.py
pipecat/src/pipecat/transports/smallwebrtc/transport.py
pipecat/src/pipecat/turns/types.py
pipecat/src/pipecat/turns/user_idle_controller.py
pipecat/src/pipecat/turns/user_mute/__init__.py
pipecat/src/pipecat/turns/user_mute/always_user_mute_strategy.py
pipecat/src/pipecat/turns/user_mute/base_user_mute_strategy.py
pipecat/src/pipecat/turns/user_mute/callback_user_mute_strategy.py
pipecat/src/pipecat/turns/user_mute/first_speech_user_mute_strategy.py
pipecat/src/pipecat/turns/user_mute/function_call_user_mute_strategy.py
pipecat/src/pipecat/turns/user_mute/mute_until_first_bot_complete_user_mute_strategy.py
pipecat/src/pipecat/turns/user_start/__init__.py
pipecat/src/pipecat/turns/user_start/base_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/external_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/krisp_viva_ip_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/min_words_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/provisional_vad_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/transcription_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/vad_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_start/wake_phrase_user_turn_start_strategy.py
pipecat/src/pipecat/turns/user_stop/__init__.py
pipecat/src/pipecat/turns/user_stop/base_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/deferred_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/external_user_turn_completion_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/external_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/llm_turn_completion_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_stop/turn_analyzer_user_turn_stop_strategy.py
pipecat/src/pipecat/turns/user_turn_completion_mixin.py
pipecat/src/pipecat/turns/user_turn_controller.py
pipecat/src/pipecat/turns/user_turn_strategies.py
pipecat/src/pipecat/utils/async_tool_cancellation.py
pipecat/src/pipecat/utils/asyncio/task_manager.py
pipecat/src/pipecat/utils/base_object.py
pipecat/src/pipecat/utils/context/aggregated_frame_sequencer.py
pipecat/src/pipecat/utils/context/llm_context_summarization.py
pipecat/src/pipecat/utils/context/message_sanitization.py
pipecat/src/pipecat/utils/context/text_segment_map.py
pipecat/src/pipecat/utils/context/word_completion_tracker.py
pipecat/src/pipecat/utils/deprecation.py
pipecat/src/pipecat/utils/env.py
pipecat/src/pipecat/utils/frame_queue.py
pipecat/src/pipecat/utils/network.py
pipecat/src/pipecat/utils/startup.py
pipecat/src/pipecat/utils/string.py
pipecat/src/pipecat/utils/text/base_text_aggregator.py
pipecat/src/pipecat/utils/text/base_text_filter.py
pipecat/src/pipecat/utils/text/pattern_pair_aggregator.py
pipecat/src/pipecat/utils/text/simple_text_aggregator.py
pipecat/src/pipecat/utils/text/transforms/_alnum_utils.py
pipecat/src/pipecat/utils/text/word_timestamp_utils.py
pipecat/src/pipecat/utils/time.py
pipecat/src/pipecat/utils/tracing/langfuse_helpers.py
pipecat/src/pipecat/utils/tracing/service_attributes.py
pipecat/src/pipecat/utils/tracing/service_decorators.py
pipecat/src/pipecat/utils/tracing/setup.py
pipecat/src/pipecat/utils/tracing/tracing_context.py
pipecat/src/pipecat/utils/tracing/turn_trace_observer.py
pipecat/src/pipecat/utils/utils.py
pipecat/src/pipecat/workers/base_worker.py
pipecat/src/pipecat/workers/runner.py
```

</details>

### Edits made to copied files

| Copied file | Edit |
| --- | --- |
| `pipecat/src/pipecat/__init__.py` | Wrapped `lib_version("pipecat-ai")` in `try/except` with a `"1.5.0"` fallback because the distribution is not installed. Copyright header preserved. |
| `pipecat/src/pipecat/services/stt_latency.py` | Removed one unused provider-specific TTFS constant. Copyright header preserved. |
| `pipecat/src/pipecat/utils/tracing/langfuse_helpers.py` | Reworded one module docstring. Copyright header preserved. |
| `services/pipecat/audio_config.py` | Removed `create_audio_config()` and its telephony/enum imports; kept the `AudioConfig` dataclass. |
| `services/pipecat/transport_setup.py` | Removed mixer, ambient-noise, app-root and workflow-run coupling; switched `AudioConfig` import to `services.pipecat.audio_config.AudioConfig`; kept `create_webrtc_transport()`. |
| `services/pipecat/pipeline_builder.py` | Reduced to `build_pipeline(...)` plus `build_realtime_pipeline(...)`; removed recording, voicemail, metrics, turn-logging and workflow branches; UI reporting is a worker-level observer rather than an in-chain processor. |
| `routes/webrtc_signaling.py` | Rewritten from the upstream signaling route: removed auth, organization/workspace, quota, concurrency, TURN, embed-session and orchestration coupling; STUN-only; calls the glue `start_pipeline()`. |
| `services/pipecat/transport_params.py` | Module docstring reworded to remove phone-call-provider references; no code change. |
| `services/pipecat/transport_setup.py` | Module docstring title reworded; no code change. |

Wording-only coupling cleanup, logged individually:

- `README.md`: "telephony" → "phone-call subsystems" in the "What was removed" section.
- `services/pipecat/transport_params.py`: replaced the module docstring that named phone-call provider factories with a standalone WebRTC-only description.
- `services/pipecat/transport_setup.py`: replaced "non-telephony pipelines" in the module docstring with "browser/WebRTC voice app".

All other copied files are verbatim. No files in the source repository were modified.

## Verification performed

- React/TypeScript production build passes with strict type checking.
- FastAPI `/health`, `/api/status`, `/api/sessions`, `/api/memory`, `/api/search`, and `/api/client-error` exercised with a temporary DuckDB file.
- DuckDB `vss` HNSW indexes verified with cosine queries; exact cosine and keyword fallbacks verified.
- Context injection and post-call memory extraction verified with local transcript fixtures.
- `logs/errors.log` and the `app_errors` table verified with synthetic frontend/backend errors.
- Fresh virtualenv created with Python 3.12.6.
- `pip install -r requirements.txt` succeeded, including `aiortc 1.16.0`, `av 16.1.0`, `numba 0.68.1`, `onnxruntime 1.24.4`, `google-genai 2.34.0`, and `aiohttp 3.14.5`.
- Case-insensitive audit for the three blocked source-identifying patterns, excluding `.venv` and LICENSE files: `TOTAL=0`.
- `import app` and `import pipeline` succeeded.
- `GET /health` returned `200 {"status":"ok"}`; `GET /` returned `200` with the expected title and state badge.
- A synthetic aiortc WebRTC offer received an `answer`, the server logged `Connected to Gemini service`, and the browser received `state speaking`, a final assistant transcript ending in `How can I help you today?`, and `state listening`.
- Source repository `git status --porcelain` was empty after the work.

## Known limitations

- A real browser-microphone spoken turn was not exercised in this environment; the end-to-end audio check used a synthetic aiortc audio track.
- Browser microphone permission and a real audio device are required; there is no file-upload fallback.
- Only one browser session is wired by default (`/ws/signaling/1`); there is no multi-user session manager.
- STUN-only WebRTC: restrictive NAT/firewall networks may need TURN, which is intentionally not configured.
- Gemini Live uses server-side VAD, so local user start/stop frames are not normally emitted. The UI derives `listening` from user transcriptions, bot-audio stop events, and interruption frames.
- Physical microphone spoken turns were not exercised here; interruption, end-call, and idle behavior were exercised with synthetic frames and injected text.
- Echo-sensitive behavior should be checked with headphones first. The browser requests echo cancellation, noise suppression, and auto gain control; on speakers the model can hear its own audio and interrupt itself.
- Final transcripts, memory facts, and errors are persisted locally, but there is no auth, workspaces, or audio recording. Browser state events are live-only.
- The only function tool is `end_conversation`; there is no workflow or general tool-calling layer.
