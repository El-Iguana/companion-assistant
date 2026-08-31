# Pantheon desktop mode (Phase 6)

Her in the corner of the screen instead of a browser tab.

## What this is

Two pieces, and neither holds application logic:

| | |
|---|---|
| **`/desktop`** | the same SPA in avatar-only dress — chrome hidden by CSS, so the voice loop, TTS queue, STT and avatar bridge all run untouched |
| **`pantheon-desktop.py`** | a borderless transparent GTK4 + WebKitGTK window pointed at that URL |

The container on `:7861` stays the brain. The shell is a picture frame.

## Run it

```bash
# the app must already be running (./run-podman.sh)
./desktop/pantheon-desktop.py
```

Options: `--url`, `--width`, `--height`.

## Positioning it (one-time)

A Wayland client **cannot place itself or keep itself above** — that is a
protocol restriction, not a toolkit gap, and it applies to Electron and Tauri
equally. Two ways round it:

**KWin rule (recommended, no new packages).** Import
`kwinrule/pantheon.kwinrule` via *System Settings → Window Management → Window
Rules → Import*, then adjust `position=` for your screen. This gives position,
always-on-top, no border, and keeps her off the taskbar and pager.

**gtk4-layer-shell (not used here).** It is packaged for Fedora and would let
the app anchor itself natively, but it needs `rpm-ostree` layering plus a
reboot, which this machine's conventions avoid.

## Click-through

Handled in the shell, not by the compositor: `GdkSurface.set_input_region`
limits clicks to a 56 px strip along the bottom where the HUD lives. Everything
above it — the avatar herself — passes clicks through to whatever is behind.

## Why WebKitGTK

Measured on this host before committing to it: WebGL 2.0, `getUserMedia`
(it found the webcam mic), MediaRecorder for STT, AudioContext with worklets —
and the avatar renderer confirmed running inside it at a live frame rate. GTK4
and WebKit 6.0 were already installed; Electron would have been a new stack.

## Known gaps

- **No tray icon yet.** `AppIndicator3` is a GTK3 library and wants a
  `Gtk.Menu`, which GTK4 removed. Doing it properly means speaking
  StatusNotifierItem over D-Bus directly. The HUD carries the controls for now.
- **No global hotkey.** Bind one in KDE to a script that focuses the window, or
  use the wake word, which already works.
- **Login persists via a cookie store** under `~/.local/share/pantheon-desktop`.
  First launch will show the login screen.
