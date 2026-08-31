"""Read and set our own window geometry through KWin's scripting interface.

Why this exists
---------------
A Wayland client may not know or set where it is. That is a protocol rule, not
a toolkit gap: GTK, Electron and Tauri are equally unable to. Only the
compositor knows, so "remember where I dragged her" has to go through it.

The obvious route is a KWin window rule with position set to Remember (2).
Measured on Plasma 6, that does not write back: the avatar sat at 1070,387
while the rule still read 1400,380, so the position was never persisted and
every restart lost the drag.

So the rule keeps only what it is reliable at -- always-on-top, no border, off
the taskbar -- and position is handled here instead:

  read   a KWin script walks the window list and calls back into this process
         over D-Bus with our frameGeometry
  write  a KWin script finds the same window and assigns frameGeometry

Both directions were verified against the live compositor before this was
written; the window reports as class `ai.jotunheim.pantheon.desktop`.

KWin's Scripting interface takes a file path, so each call writes a tiny script
into the runtime dir and unloads it again.
"""
from __future__ import annotations

import os
from typing import Callable

from gi.repository import Gio, GLib

KWIN = "org.kde.KWin"
SCRIPTING_PATH = "/Scripting"
SCRIPTING_IFACE = "org.kde.kwin.Scripting"

# Our own callback surface, so a KWin script has somewhere to send the answer.
REPORT_NAME = "org.jotunheim.PantheonShell"
REPORT_PATH = "/Shell"
REPORT_IFACE = "org.jotunheim.PantheonShell"

REPORT_XML = f"""
<node><interface name="{REPORT_IFACE}">
  <method name="ReportGeometry">
    <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    <arg name="w" type="i" direction="in"/><arg name="h" type="i" direction="in"/>
  </method>
</interface></node>
"""


class KWinGeometry:
    """Geometry round-trip for one window, matched by its resource class."""

    def __init__(self, match: str, on_geometry: Callable[[int, int, int, int], None]):
        self.match = match.lower()
        self.on_geometry = on_geometry
        self.bus: Gio.DBusConnection | None = None
        self.available = False

    # -- setup -------------------------------------------------------------
    def start(self) -> bool:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            info = Gio.DBusNodeInfo.new_for_xml(REPORT_XML).interfaces[0]
            self.bus.register_object(REPORT_PATH, info, self._on_call, None, None)
            Gio.bus_own_name_on_connection(
                self.bus, REPORT_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
        except Exception as e:                                # noqa: BLE001
            print(f"[kwin] cannot publish callback: {e}")
            return False
        # No KWin means no geometry control, but the window still works.
        self.available = self._kwin_present()
        if not self.available:
            print("[kwin] KWin scripting unavailable — position will not persist")
        return self.available

    def _kwin_present(self) -> bool:
        try:
            self.bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "NameHasOwner",
                GLib.Variant("(s)", (KWIN,)), GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except Exception:                                     # noqa: BLE001
            return False

    def _on_call(self, _c, _s, _p, _i, method, params, inv):
        if method == "ReportGeometry":
            x, y, w, h = params.unpack()
            try:
                self.on_geometry(int(x), int(y), int(w), int(h))
            except Exception as e:                            # noqa: BLE001
                print(f"[kwin] geometry handler failed: {e}")
        inv.return_value(None)

    # -- running scripts ---------------------------------------------------
    def _run(self, body: str, tag: str) -> bool:
        """Load, start and unload a one-shot KWin script."""
        if not self.available:
            return False
        path = os.path.join(
            GLib.get_user_runtime_dir() or "/tmp", f"pantheon-{tag}.js")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            self.bus.call_sync(
                KWIN, SCRIPTING_PATH, SCRIPTING_IFACE, "loadScript",
                GLib.Variant("(ss)", (path, f"pantheon-{tag}")),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            self.bus.call_sync(
                KWIN, SCRIPTING_PATH, SCRIPTING_IFACE, "start",
                None, None, Gio.DBusCallFlags.NONE, 3000, None)
            # Unload, or the next load of the same name is refused as a duplicate.
            GLib.timeout_add(700, self._unload, tag)
            return True
        except Exception as e:                                # noqa: BLE001
            print(f"[kwin] script {tag} failed: {e}")
            return False

    def _unload(self, tag: str):
        try:
            self.bus.call_sync(
                KWIN, SCRIPTING_PATH, SCRIPTING_IFACE, "unloadScript",
                GLib.Variant("(s)", (f"pantheon-{tag}",)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception:                                     # noqa: BLE001
            pass
        return False

    # -- the two operations -------------------------------------------------
    def request(self) -> bool:
        """Ask KWin where our window is; the answer arrives at on_geometry."""
        return self._run(f"""
workspace.windowList().forEach(function (w) {{
  if (String(w.resourceClass).toLowerCase().indexOf("{self.match}") !== -1) {{
    var g = w.frameGeometry;
    callDBus("{REPORT_NAME}", "{REPORT_PATH}", "{REPORT_IFACE}", "ReportGeometry",
             Math.round(g.x), Math.round(g.y), Math.round(g.width), Math.round(g.height));
  }}
}});
""", "read")

    def apply(self, x: int, y: int, w: int, h: int) -> bool:
        """Put our window back where it was."""
        return self._run(f"""
workspace.windowList().forEach(function (win) {{
  if (String(win.resourceClass).toLowerCase().indexOf("{self.match}") !== -1) {{
    win.frameGeometry = {{ x: {int(x)}, y: {int(y)},
                           width: {int(w)}, height: {int(h)} }};
  }}
}});
""", "apply")
