# Storage, Context, Memory, and Error Logs

This project now keeps all local assistant state in one DuckDB file and uses the
existing local Ollama service for embeddings. Only the Gemini Live call and the
optional post-call memory distillation call leave the machine; embeddings,
database, transcripts, and logs stay local.

## Database

Default file:

```text
D:\voice-assistant\data\voice_assistant.duckdb
```

Override with `VOICE_DB_PATH`. The file is created automatically on server
startup. Deleting it resets all sessions, memory, and stored errors.

### Tables

| Table | Purpose |
| --- | --- |
| `sessions` | One row per call: start/end time, status, model, voice, language, title, counts |
| `messages` | Final user/assistant transcript turns with session id and turn order |
| `memories` | Distilled durable facts, category, confidence, usage count, active flag |
| `message_embeddings` | 768-dim Ollama vectors for transcript turns |
| `memory_embeddings` | 768-dim Ollama vectors for memory facts |
| `app_errors` | Backend and reported frontend errors, stack, context, timestamp |

### HNSW vector index

DuckDB's `vss` extension provides the HNSW indexes on both embedding tables:

```sql
CREATE INDEX message_embeddings_hnsw_idx
ON message_embeddings USING HNSW (embedding) WITH (metric = 'cosine');

CREATE INDEX memory_embeddings_hnsw_idx
ON memory_embeddings USING HNSW (embedding) WITH (metric = 'cosine');
```

DuckDB requires `SET hnsw_enable_experimental_persistence=true` for file-backed
HNSW indexes. The store sets this on startup and rebuilds both indexes from the
stored vectors, so an old or partially written index cannot make search stale.
If `vss` cannot be loaded (for example, offline with no cached extension), the
same tables still work via exact cosine fallback and the error is logged.

## Local embeddings

Embeddings are generated through Ollama only:

```text
POST http://127.0.0.1:11434/api/embed
model: nomic-embed-text
dimensions: 768
```

Configure with `OLLAMA_URL`, `OLLAMA_EMBED_MODEL`, `EMBEDDING_DIM`, and
`OLLAMA_TIMEOUT_SECONDS`. If Ollama is down, the assistant still works:
transcripts and memories are stored without vectors, and search falls back to
SQLite-style keyword matching over DuckDB text columns.

## Call lifecycle

1. `start_pipeline()` inserts a `sessions` row.
2. `build_system_instruction()` loads active memories and recent prior turns,
   optionally re-ranks memories with HNSW, and appends them to the base prompt.
3. `TranscriptBridge` forwards final transcript text to the browser and schedules
   a background task that inserts a `messages` row plus an Ollama embedding.
4. On call end, pending persistence tasks are awaited.
5. `extract_and_store_memories()` asks Gemini for 0–5 durable facts. If Gemini is
   unavailable, a local regex fallback runs. Facts are embedded and upserted;
   near-duplicates update the existing memory instead of adding a new row.
6. The session row is marked ended with the number of stored facts.

The pipeline never fails a call because storage, embedding, or extraction had a
problem. Every such failure is logged as an ERROR.

## History and search API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Storage path, HNSW status, embedding model |
| `GET /api/sessions?limit=50` | Recent calls |
| `GET /api/sessions/{id}/messages?limit=200` | Stored transcript for a call |
| `GET /api/memory?limit=100` | Active distilled facts |
| `GET /api/search?q=...&limit=10` | Semantic search with keyword fallback |
| `POST /api/client-error` | Browser errors written to logs and DuckDB |

The full page has a **History & Memory** drawer that uses these endpoints.

## Error logs

Errors are written in four places:

| Location | Source |
| --- | --- |
| `logs/errors.log` | All backend `ERROR` records via loguru, rotated at 10 MB |
| `data/voice_assistant.duckdb` → `app_errors` | Backend errors and `/api/client-error` reports |
| `.voice-backend.err.log` | Raw stderr from the hotkey-launched server |
| `.voice-hotkey.log` | Launcher, build, browser, and port lifecycle |

Browser `window.error`, `unhandledrejection`, WebRTC ICE failures, and call-start
failures are reported to `/api/client-error`.

Query the database directly if needed:

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect(r'data\voice_assistant.duckdb'); print(c.execute('SELECT occurred_at, source, message FROM app_errors ORDER BY occurred_at DESC LIMIT 20').fetchall())"
```

## Configuration reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `VOICE_DB_PATH` | `data/voice_assistant.duckdb` | DuckDB file location |
| `EMBEDDING_DIM` | `768` | Must match the Ollama model and stored schema |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama service |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Local embedding model |
| `OLLAMA_TIMEOUT_SECONDS` | `10` | Embedding request timeout |
| `MEMORY_MODEL` | `gemini-flash-lite-latest` | Text model for post-call distillation |
| `MEMORY_EXTRACTION_TIMEOUT_SECONDS` | `15` | Distillation timeout |
| `MEMORY_DEDUPE_THRESHOLD` | `0.93` | Cosine similarity threshold for memory upsert |
| `MEMORY_CONTEXT_LIMIT` | `20` | Max memories injected into a call |
| `CONTEXT_TURN_LIMIT` | `20` | Max older transcript turns injected into a call |
