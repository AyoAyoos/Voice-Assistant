# Troubleshooting

Start with the four local log files:

```text
logs/errors.log                 structured backend ERROR records
.voice-hotkey.log               launcher and hotkey lifecycle
.voice-backend.log              backend stdout
.voice-backend.err.log          backend stderr and tracebacks
```

The hotkey launcher writes a timestamp line for every build, start, stop, and
failure. If nothing happens on `Ctrl + ;`, check those logs first.

## Hotkey opens the panel but does not start the call

The launcher opens `?compact=1&autostart=1`, and the React app calls `start()`
once after mount. Verify:

1. `scripts/voice.ps1` still includes `autostart=1` in `$url`.
2. The browser was launched with `--autoplay-policy=no-user-gesture-required`
   and `--use-fake-ui-for-media-stream`.
3. `.voice-backend.err.log` does not show a microphone or signaling error.
4. `logs/errors.log` does not contain a recent `/api/client-error` report.

If the panel was opened manually from `http://127.0.0.1:8000/`, press **Start**
normally; autostart is intentionally tied to the hotkey launcher URL.

## Microphone permission is denied

The browser uses a dedicated profile at `.voice-browser-profile` and passes
`--use-fake-ui-for-media-stream` to auto-approve the local mic prompt. If the
permission was previously denied in that profile:

1. Press `Ctrl + ;` to stop.
2. Delete `.voice-browser-profile`.
3. Press `Ctrl + ;` again.

A real microphone and a browser with WebRTC support are still required.
Headphones are recommended to avoid the assistant hearing its own audio.

## UI changes are not visible

The launcher runs `npm run build` on every start, so the next `Ctrl + ;` press
picks up frontend changes. If a full-page tab was already open, refresh it.
If the backend was started manually with `python app.py`, restart it after the
first build because the static directory is resolved at import time.

## Port 8000 is already in use

Check with:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen
```

Then either press `Ctrl + ;` to let the launcher stop its tracked backend, or
stop the other process. The launcher deliberately leaves ports it did not start
alone to avoid killing unrelated software.

## DuckDB is unavailable or the database is locked

Only one backend process should own the database file. Stop all instances, then
restart. Check `logs/errors.log` for `Could not initialize DuckDB` or
`Could not open database`. The database is at `data/voice_assistant.duckdb`
unless `VOICE_DB_PATH` is set.

To inspect it:

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect(r'data\voice_assistant.duckdb'); print(c.execute('SHOW TABLES').fetchall())"
```

## HNSW index is off

`GET /api/status` reports `hnsw: false` when DuckDB cannot load the `vss`
extension. This usually means the machine is offline and the extension is not
cached. The app still works with exact cosine fallback. To install it manually
when online:

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; duckdb.connect().execute('INSTALL vss')"
```

Restart the backend afterward; indexes are rebuilt on startup.

## Ollama embeddings fail

Check the local service and model:

```powershell
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" list
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull nomic-embed-text
```

Then verify the endpoint:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:11434/api/embed -Method Post `
  -ContentType application/json `
  -Body '{"model":"nomic-embed-text","input":["test"]}' |
  Select-Object -ExpandProperty embeddings
```

If Ollama is unavailable, `logs/errors.log` records `Ollama embedding failed`.
Calls still work, but search uses keyword matching only and no new vectors are
stored.

## Gemini setup or memory extraction fails

- Confirm `GEMINI_API_KEY` is present in `.env`.
- Confirm `GEMINI_LIVE_MODEL` is available for that key and region.
- Memory extraction uses `MEMORY_MODEL`, default `gemini-flash-lite-latest`.
  Some older models such as `gemini-2.5-flash` return `404: no longer available
  to new users`; the default avoids that.
- A failed extraction does not end the call; the local regex fallback runs.

Details are in `logs/errors.log` under `[voice-session]` or `[memory-extraction]`.

## History is empty

Only final transcript turns after this update are stored. Memory facts require
at least one clear user statement. If Gemini extraction returns nothing, the
local fallback recognizes phrases such as "my name is", "I prefer", and
"remember that". Check:

```powershell
.\.venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect(r'data\voice_assistant.duckdb'); print(c.execute('SELECT content FROM memories ORDER BY updated_at DESC LIMIT 10').fetchall())"
```

## Hotkey itself does not fire

The AutoHotkey script is `scripts/voice-hotkey.ahk` and the startup shortcut is
in the Windows Startup folder. Use the tray icon → **Reload Script** after
editing it. Verify the script directly:

```powershell
& 'D:\voice-assistant\tools\autohotkey\AutoHotkey64.exe' 'D:\voice-assistant\scripts\voice-hotkey.ahk'
```

Synthetic keyboard events from automation tools may not trigger global hotkeys;
test by physically pressing `Ctrl + ;`.
