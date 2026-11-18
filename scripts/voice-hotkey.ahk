#Requires AutoHotkey v2.0
#SingleInstance Force

; Global hotkey for the Voice Assistant one-command launcher.
; Ctrl + ; toggles the compact floating panel and the backend.

busy := false

ToggleVoice(*) {
    global busy
    if (busy)
        return
    busy := true
    Run 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\voice-assistant\scripts\voice.ps1" -Toggle', , "Hide"
    SetTimer ClearBusy, -6000
}

ClearBusy() {
    global busy
    busy := false
}

Hotkey "^;", ToggleVoice

A_IconTip := "Voice Assistant"
