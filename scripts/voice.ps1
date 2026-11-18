param(
    [ValidateSet("Toggle", "Start", "Stop", "Status")]
    [string]$Action,
    [switch]$Toggle,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

if ($Status)      { $Action = "Status" }
elseif ($Stop)    { $Action = "Stop" }
elseif ($Start)   { $Action = "Start" }
elseif ($Toggle)  { $Action = "Toggle" }
if (-not $Action) { $Action = "Toggle" }

$Root = Split-Path -Parent $PSScriptRoot
$WebDir = Join-Path $Root "web"
$PidFile = Join-Path $Root ".voice-pids.json"
$LogFile = Join-Path $Root ".voice-hotkey.log"
$BackendOut = Join-Path $Root ".voice-backend.log"
$BackendErr = Join-Path $Root ".voice-backend.err.log"
$BrowserProfile = Join-Path $Root ".voice-browser-profile"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BackendPort = 8000
$MiniTitle = "Voice Assistant Mini"

function Write-VoiceLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line
}

function Test-VoicePort {
    param([int]$Number)
    return [bool](Get-NetTCPConnection -LocalPort $Number -State Listen -ErrorAction SilentlyContinue)
}

function Get-MiniWindows {
    return @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -like "*$MiniTitle*" })
}

function Read-SavedPid {
    if (-not (Test-Path $PidFile)) {
        return $null
    }
    try {
        $value = Get-Content $PidFile -Raw | ConvertFrom-Json
        if ($value -and $value.BackendPid) {
            return [int]$value.BackendPid
        }
    } catch {
        Write-VoiceLog "Could not read PID file: $_"
    }
    return $null
}

function Save-BackendPid {
    param([int]$Id)
    @{ BackendPid = $Id } | ConvertTo-Json | Set-Content $PidFile
}

function Build-Frontend {
    if (-not (Test-Path (Join-Path $WebDir "node_modules"))) {
        Write-VoiceLog "node_modules missing; running npm install"
        Push-Location $WebDir
        try {
            & npm install 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit $LASTEXITCODE)" }
        } finally {
            Pop-Location
        }
    }

    Write-VoiceLog "Building frontend"
    Push-Location $WebDir
    try {
        $output = & npm run build 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        throw "npm run build failed (exit $code): $output"
    }
}

function Start-Backend {
    if (Test-VoicePort $BackendPort) {
        Write-VoiceLog "Backend already listening on $BackendPort"
        return
    }
    if (-not (Test-Path $Python)) {
        throw "Missing Python interpreter: $Python"
    }

    $process = Start-Process -FilePath $Python -ArgumentList "app.py" `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr
    Save-BackendPid -Id $process.Id
    Write-VoiceLog "Started backend PID $($process.Id)"

    for ($i = 0; $i -lt 60; $i += 1) {
        if (Test-VoicePort $BackendPort) {
            Write-VoiceLog "Backend ready on $BackendPort"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Backend did not become ready; see $BackendErr"
}

function Stop-Backend {
    $savedPid = Read-SavedPid
    if ($savedPid) {
        $alive = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($alive) {
            & taskkill /PID $savedPid /T /F 2>&1 | Out-Null
            Write-VoiceLog "Stopped backend PID $savedPid"
        } else {
            Write-VoiceLog "Backend PID $savedPid was already gone"
        }
    } elseif (Test-VoicePort $BackendPort) {
        Write-VoiceLog "Port $BackendPort is in use but was not started by this launcher; leaving it alone"
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

function Close-MiniWindow {
    $windows = Get-MiniWindows
    foreach ($window in $windows) {
        Write-VoiceLog "Closing mini window PID $($window.Id)"
        $window.CloseMainWindow() | Out-Null
    }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;

public static class VoiceWindowApi
{
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
    public const uint SWP_NOSIZE = 0x0001;
    public const uint SWP_SHOWWINDOW = 0x0040;
}
'@

function Open-MiniWindow {
    $stamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $url = "http://127.0.0.1:$BackendPort/?compact=1&autostart=1&t=$stamp"

    $browser = @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $browser) {
        throw "Neither Chrome nor Edge was found"
    }

    Start-Process -FilePath $browser -ArgumentList @(
        "--app=$url",
        "--window-size=420,320",
        "--autoplay-policy=no-user-gesture-required",
        "--use-fake-ui-for-media-stream",
        "--user-data-dir=$BrowserProfile",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble"
    )
    Write-VoiceLog "Opened mini window with autostart: $url"

    $handle = [IntPtr]::Zero
    for ($i = 0; $i -lt 40; $i += 1) {
        $window = Get-MiniWindows | Select-Object -First 1
        if ($window -and $window.MainWindowHandle -ne [IntPtr]::Zero) {
            $handle = $window.MainWindowHandle
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if ($handle -eq [IntPtr]::Zero) {
        Write-VoiceLog "Mini window handle not found; skipping topmost positioning"
        return
    }

    $work = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
    $x = $work.Right - 420 - 16
    $y = $work.Bottom - 320 - 16
    [void][VoiceWindowApi]::SetWindowPos(
        $handle,
        [VoiceWindowApi]::HWND_TOPMOST,
        $x, $y, 0, 0,
        [VoiceWindowApi]::SWP_NOSIZE -bor [VoiceWindowApi]::SWP_SHOWWINDOW
    )
    Write-VoiceLog "Pinned mini window at $x,$y"
}

try {
    switch ($Action) {
        "Status" {
            $windows = Get-MiniWindows
            $savedPid = Read-SavedPid
            [pscustomobject]@{
                BackendListening = Test-VoicePort $BackendPort
                TrackedPid = $savedPid
                MiniWindows = $windows.Count
            } | ConvertTo-Json
        }
        "Stop" {
            Close-MiniWindow
            Stop-Backend
            Write-VoiceLog "Stop complete"
        }
        "Start" {
            Build-Frontend
            Start-Backend
            Open-MiniWindow
            Write-VoiceLog "Start complete"
        }
        "Toggle" {
            $windows = Get-MiniWindows
            $savedPid = Read-SavedPid
            $trackedAlive = $savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)
            if ($windows.Count -gt 0 -or $trackedAlive) {
                Write-VoiceLog "Toggle: stopping"
                Close-MiniWindow
                Stop-Backend
            } else {
                Write-VoiceLog "Toggle: starting"
                Build-Frontend
                Start-Backend
                Open-MiniWindow
            }
        }
    }
    exit 0
} catch {
    Write-VoiceLog "ERROR: $_"
    Write-Error $_
    exit 1
}
