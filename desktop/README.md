# Cantinero desktop mode (Phase 6)

Her in the corner of the screen instead of a browser tab.

## What this is

Two pieces, and neither holds application logic:

| | |
|---|---|
| **`/desktop`** | a small page (`pantheon_web/desktop.py`, `static/js/desktop.js`) on the same voice island (`voice.js`) and avatar renderer (`vtuber.mjs`) as the main page — so speech, the mic loop and the avatar behave identically in both |
| **`pantheon-desktop.py`** | a borderless transparent GTK4 + WebKitGTK window pointed at that URL |

The container on `127.0.0.1:7867` stays the brain. The shell is a picture frame.

Which character: `?card=<id>` on the URL, else **Settings → Desktop-mode character**, else
whoever you chatted with last. She continues her latest chat rather than starting a new one.

## Run it

```bash
# the app must already be running (./run-podman.sh)
./desktop/pantheon-desktop.py
```

Options: `--url`, `--width`, `--height`.

## Positioning it — just drag her

Pick **"Move her (drag to position)"** from the tray menu. A grab bar appears at
the top; drag her where you want and untick it again.

The distinction that makes this work: a Wayland client may not *set* its own
coordinates — a hard protocol restriction that applies to Electron and Tauri
equally — but it *may* ask the compositor for an interactive move, which is
exactly what dragging a titlebar does. `Gtk.WindowHandle` does that for us.

Position mode also suspends click-through while it is on, because you cannot
drag a window that does not accept the click; your previous setting comes back
when you leave.

### Making it stick

Import `kwinrule/pantheon.kwinrule` (*System Settings → Window Management →
Window Rules → Import*). It sets always-on-top, no border, and keeps her off
the taskbar, pager and window switcher.

Position and size in that rule are **Remember**, not Force — deliberately, so
your drag is what persists. Force would snap her back every time. The
coordinates in the file are only a first guess for a 1920×1080 screen; once you
have dragged her, they stop mattering.

**gtk4-layer-shell (not used here).** It is packaged for Fedora and would let
the app anchor itself natively, but it needs `rpm-ostree` layering plus a
reboot, which this machine's conventions avoid — and it would not add anything
dragging does not already give you.

## Click-through

Handled in the shell, not by the compositor: `GdkSurface.set_input_region`
limits clicks to a 56 px strip along the bottom where the HUD lives. Everything
above it — the avatar herself — passes clicks through to whatever is behind.

## Why WebKitGTK

Measured on this host before committing to it: WebGL 2.0, `getUserMedia`
(it found the webcam mic), MediaRecorder for STT, AudioContext with worklets —
and the avatar renderer confirmed running inside it at a live frame rate. GTK4
and WebKit 6.0 were already installed; Electron would have been a new stack.

## Tray icon

`tray.py` speaks **StatusNotifierItem directly over D-Bus** — KDE's own tray
protocol. The obvious binding, `AppIndicator3`, is a GTK3 library that wants a
`Gtk.Menu`, which GTK4 removed, so using it would mean two GTK versions in one
process. SNI is just a D-Bus interface, and GLib's `Gio` already provides the
bus, so this adds no dependency at all.

Two objects are published — `/StatusNotifierItem` for the icon and `/MenuBar`
(`com.canonical.dbusmenu`) for the menu — then `org.kde.StatusNotifierWatcher`
is asked to adopt them. Left-click shows or hides her; the menu carries
show/hide, a click-through checkbox, reload and quit.

`--icon NAME` picks the tray icon: a PNG in `desktop/icons/` (default `cantinero`, El Iguana
behind the bar), or else a name from the icon theme. `--no-tray` skips it;
if no watcher is running the shell says so and carries on without one.

## Known gaps

- **No global hotkey.** Bind one in KDE to a script that focuses the window, or
  use the wake word, which already works.
- **Login persists via a cookie store** under `~/.local/share/pantheon-desktop`.
  First launch shows the sign-in page, which returns to `/desktop` afterwards. The
  2026-10-26 switch-over moved the address to `http://127.0.0.1:7867/desktop` and
  the session to the new page's, so the shell asks once more after that update.
