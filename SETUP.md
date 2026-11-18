# Setup Guide

This guide takes you from a fresh clone to a working voice assistant with a global `Ctrl + ;` hotkey. It is written for Windows because the one-command launcher and AutoHotkey integration are Windows-specific. The Python server and React UI themselves also run on macOS/Linux if you skip the hotkey steps.

At the end you will have:

- A FastAPI server on `http://127.0.0.1:8000` talking to the Gemini Live API.
- A React UI that sends microphone audio over WebRTC and plays the reply.
- A local DuckDB database holding transcripts, memory facts, vectors and errors.
- A compact bottom-right panel that starts and stops with `Ctrl + ;`.

> [!IMPORTANT]
> Everything under `data/`, `logs/`, `.env`, the browser profile and all build output is gitignored. Never commit your API key, call history or local logs.

---

## 1. Prerequisites

| Requirement | Version / notes | Check |
| --- | --- | --- |
| Windows | 10 or 11 for the hotkey launcher | — |
| Python | 3.11 or 3.12 (3.12.6 used here) | `python --version` |
| Node.js | 20+ with npm | `node --version` |
| Gemini API key | From Google AI Studio, with the Live API enabled | — |
| Ollama | Local service for embeddings | `ollama --version` |
| Chrome or Edge | Installed for the launcher's app-mode window | — |
| AutoHotkey v2 | Only for the global hotkey; a portable copy is recommended | — |

You do **not** need a separate Pipecat installation. The repository vendors the BSD-2-Clause Pipecat source under `pipecat/src` and `bootstrap.py` puts it on `sys.path`.

---

## 2. Get the code

```powershell
git clone https://github.com/AyoAyoos/Voice-Assistant.git
cd Voice-Assistant
```

All commands below assume the project lives at `D:\voice-assistant`. If you clone somewhere else, update the two hardcoded paths in `scripts/voice-hotkey.ahk` and the Startup shortcut described in step 7.

---

## 3. Python environment

Use the folder name `.venv` exactly — the launcher looks for `.venv\Scripts\python.exe`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This installs FastAPI, aiortc, the vendored Pipecat audio stack (onnxruntime, soxr, numba, av), duckdb, httpx and loguru. The first DuckDB startup also downloads the `vss` extension if it is not cached; an internet connection is needed once. Without it, search falls back to exact cosine/keyword matching and logs a warning.

---

## 4. Configure your environment

```powershell
copy .env.example .env
notepad .env
```

Set at least:

```dotenv
GEMINI_API_KEY=your-real-key-here
```

Other defaults that usually need no change:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Live speech-to-speech endpoint |
| `GEMINI_VOICE` | `Puck` | Spoken voice |
| `GEMINI_LANGUAGE` | `en-US` | Language hint |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address |
| `VOICE_DB_PATH` | `data/voice_assistant.duckdb` | Local database |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local embeddings service |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `EMBEDDING_DIM` | `768` | Must match the model and stored schema |
| `MEMORY_MODEL` | `gemini-flash-lite-latest` | Post-call fact extraction |
| `MEMORY_CONTEXT_LIMIT` / `CONTEXT_TURN_LIMIT` | `20` / `20` | Context injected per call |

Notes:

- The API key is sent in the `x-goog-api-key` **header**, never in a URL, and is redacted from logs and error rows.
- If Gemini returns `404` for your memory model, keep `gemini-flash-lite-latest`. The older `gemini-2.5-flash` is no longer available to new keys.
- Other supported Live model IDs are listed in `.env.example`.

---

## 5. Ollama (local embeddings)

Install Ollama from <https://ollama.com>, then pull the embedding model once:

```powershell
ollama pull nomic-embed-text
```

Ollama must be running when the server starts. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

If Ollama is down, calls still work: transcripts are stored without vectors and search uses keyword matching. The assistant logs an ERROR for each failed embedding request.

---

## 6. Build and run manually

Build the frontend once:

```powershell
cd D:\voice-assistant\web
npm install
npm run build
cd D:\voice-assistant
```

`npm run build` also runs `tsc --noEmit`, so it fails fast on type errors. It writes `web\dist`, which the Python server mounts. Restart the backend after the **first** build because the static directory is resolved at import time.

Start the server:

```powershell
.\.venv\Scripts\python.exe app.py
```

Verify in a browser or terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health     # {"status":"ok"}
Invoke-RestMethod http://127.0.0.1:8000/api/status # storage + embeddings info
```

Open <http://127.0.0.1:8000/>, click **Start**, allow microphone access, and say hello. Localhost is a secure browser context, so WebRTC does not need HTTPS. Headphones are recommended to prevent the assistant hearing its own output. Stop the server with `Ctrl+C`.

For frontend development with hot reload, run `npm run dev` in `web/` and open <http://127.0.0.1:5173/> while the Python server keeps running; Vite proxies `/ws` and `/api` to port 8000.

---

## 7. One-command hotkey (Windows)

### 7.1 Get AutoHotkey v2

Download the portable zip from <https://www.autohotkey.com/download/ahk-v2.zip> and place `AutoHotkey64.exe` in:

```text
D:\voice-assistant\tools\autohotkey\AutoHotkey64.exe
```

`tools/` is gitignored, so this local binary is never pushed.

### 7.2 Load the script at login

Create a shortcut in your Startup folder that runs the portable binary with the script:

```powershell
$startup = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startup 'Voice Assistant Hotkey.lnk'
$ws = New-Object -ComObject WScript.Shell
$link = $ws.CreateShortcut($shortcut)
$link.TargetPath = 'D:\voice-assistant\tools\autohotkey\AutoHotkey64.exe'
$link.Arguments = '"D:\voice-assistant\scripts\voice-hotkey.ahk"'
$link.WorkingDirectory = 'D:\voice-assistant'
$link.Description = 'Voice Assistant global Ctrl+; hotkey'
$link.Save()
```

Log out and back in, or run the shortcut once to start the hotkey immediately. The tray icon is labelled "Voice Assistant". After editing the `.ahk` file, use tray icon → **Reload Script**.

### 7.3 Use it

- **First `Ctrl + ;`**: rebuilds the React app, starts the backend, opens a small always-on-top window at the bottom-right with `?compact=1&autostart=1`, and starts the call automatically.
- **Second `Ctrl + ;`**: closes the panel and stops the backend.
- **Expand ↗** in the compact panel opens the full page with the History & Memory drawer.

The launcher uses a dedicated `.voice-browser-profile` with autoplay and mic flags so the call can start without a manual click. Check `.voice-hotkey.log` for every build/start/stop decision.

You can also drive it directly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Toggle
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\voice-assistant\scripts\voice.ps1 -Status
```

---

## 8. First-run checklist

1. `GET /health` returns `{"status":"ok"}`.
2. `GET /api/status` shows `storage.available: true` and your embedding model.
3. The full page shows **Idle**, then **Listening…** → **Thinking…** → **Speaking…** during a turn.
4. After ending a call, **History & Memory** lists a session with turns.
5. `data\voice_assistant.duckdb` exists and `logs\errors.log` has no new ERROR entries.
6. `Ctrl + ;` toggles the compact panel and the backend without manual clicks.

If something fails, follow [TROUBLESHOOTING.md](TROUBLESHOOTING.md). For details on the data written, see [STORAGE_AND_MEMORY.md](STORAGE_AND_MEMORY.md).

---

## 9. Day-to-day workflow

- Start/stop with `Ctrl + ;` (or the PowerShell commands above). The launcher rebuilds the UI on every start, so frontend edits appear on the next press.
- Keep Ollama running in the background for semantic search and memory vectors.
- The full page at <http://127.0.0.1:8000/> is the place to browse history, memory facts and search results.
- To update the project: `git pull`, then stop and restart via `Ctrl + ;` (and re-run `pip install -r requirements.txt` if dependencies changed).

## 10. Local files you should never commit

```text
.env                         real API key
data/voice_assistant.duckdb  call history, memory, vectors, errors
logs/errors.log              structured error log
.voice-hotkey.log            launcher log
.voice-backend*.log          backend stdout/stderr
.voice-pids.json             tracked backend PID
.voice-browser-profile/      browser state and mic permission
tools/autohotkey/            portable AutoHotkey binary
web/node_modules/  web/dist/  .venv/  venv/  __pycache__/
```

`.env.example` is the only configuration file tracked. If you accidentally expose a key, rotate it in Google AI Studio immediately and clear `logs/` and the `app_errors` table.
