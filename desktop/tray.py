"""System tray icon via StatusNotifierItem, spoken directly over D-Bus.

The obvious route, AppIndicator3, is a GTK3 library that wants a Gtk.Menu --
which GTK4 removed -- so using it would mean two GTK versions in one process.
SNI is just a D-Bus interface, so talking to it directly costs nothing but the
boilerplate below and drops the toolkit dependency entirely. GLib's Gio already
provides the bus, so there is no new package either.

Two objects are published:

  /StatusNotifierItem   org.kde.StatusNotifierItem  -- the icon itself
  /MenuBar              com.canonical.dbusmenu      -- its menu

then org.kde.StatusNotifierWatcher is asked to adopt us. That is the whole
protocol; the fiddly part is that dbusmenu describes its layout as a nested
variant, built in _layout() below.
"""
from __future__ import annotations

import os
from typing import Callable

from gi.repository import Gio, GLib

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER = "org.kde.StatusNotifierWatcher"

SNI_XML = f"""
<node><interface name="{SNI_IFACE}">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg name="x" type="i" direction="in"/>
    <arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/>
    <arg name="y" type="i" direction="in"/></method>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/>
    <arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/>
    <arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewIcon"/><signal name="NewTitle"/><signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface></node>
"""

MENU_XML = f"""
<node><interface name="{MENU_IFACE}">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg name="parentId" type="i" direction="in"/>
    <arg name="recursionDepth" type="i" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="revision" type="u" direction="out"/>
    <arg name="layout" type="(ia{{sv}}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="properties" type="a(ia{{sv}})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg name="id" type="i" direction="in"/>
    <arg name="name" type="s" direction="in"/>
    <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg name="id" type="i" direction="in"/>
    <arg name="eventId" type="s" direction="in"/>
    <arg name="data" type="v" direction="in"/>
    <arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="AboutToShow">
    <arg name="id" type="i" direction="in"/>
    <arg name="needUpdate" type="b" direction="out"/>
  </method>
  <signal name="LayoutUpdated">
    <arg name="revision" type="u"/><arg name="parent" type="i"/>
  </signal>
</interface></node>
"""


class MenuItem:
    __slots__ = ("id", "label", "on_click", "toggle", "checked", "is_sep", "enabled")

    def __init__(self, id_, label="", on_click=None, toggle=False,
                 checked=False, is_sep=False, enabled=True):
        self.id, self.label, self.on_click = id_, label, on_click
        self.toggle, self.checked, self.is_sep, self.enabled = toggle, checked, is_sep, enabled

    def props(self) -> dict:
        if self.is_sep:
            return {"type": GLib.Variant("s", "separator")}
        p = {
            "label": GLib.Variant("s", self.label),
            "enabled": GLib.Variant("b", self.enabled),
            "visible": GLib.Variant("b", True),
        }
        if self.toggle:
            p["toggle-type"] = GLib.Variant("s", "checkmark")
            p["toggle-state"] = GLib.Variant("i", 1 if self.checked else 0)
        return p


class Tray:
    def __init__(self, app_id: str, title: str, icon: str,
                 items: list[MenuItem], on_activate: Callable | None = None):
        self.app_id, self.title, self.icon = app_id, title, icon
        self.items = items
        self.on_activate = on_activate
        self.revision = 1
        self.bus: Gio.DBusConnection | None = None
        self._name_id = None
        self._reg = []

    # -- publishing --------------------------------------------------------
    def start(self) -> bool:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as e:                            # noqa: BLE001
            print(f"[tray] no session bus: {e}")
            return False

        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        try:
            self._reg.append(self.bus.register_object(
                "/StatusNotifierItem", sni, self._sni_call, self._sni_get, None))
            self._reg.append(self.bus.register_object(
                "/MenuBar", menu, self._menu_call, self._menu_get, None))
        except Exception as e:                            # noqa: BLE001
            print(f"[tray] could not publish objects: {e}")
            return False

        # The watcher identifies us by our unique bus name; a well-known name is
        # conventional but the spec accepts either.
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._name_id = Gio.bus_own_name_on_connection(
            self.bus, name, Gio.BusNameOwnerFlags.NONE,
            lambda *_: self._register_with_watcher(name), None)
        return True

    def _register_with_watcher(self, name: str):
        try:
            self.bus.call_sync(
                WATCHER, "/StatusNotifierWatcher", WATCHER,
                "RegisterStatusNotifierItem", GLib.Variant("(s)", (name,)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
            print(f"[tray] registered as {name}")
        except Exception as e:                            # noqa: BLE001
            # No watcher means no tray in this session -- not fatal, the HUD
            # still works, so say so and carry on.
            print(f"[tray] no StatusNotifierWatcher ({e}); continuing without a tray")

    def update(self):
        """Re-publish the menu after a checkbox changes."""
        self.revision += 1
        try:
            self.bus.emit_signal(None, "/MenuBar", MENU_IFACE, "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))
        except Exception:                                 # noqa: BLE001
            pass

    # -- StatusNotifierItem ------------------------------------------------
    def _sni_get(self, _c, _s, _p, _i, prop):
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], self.title, "")),
            # False, so a left-click reaches Activate instead of only opening
            # the menu -- clicking the icon should show her.
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/MenuBar"),
        }.get(prop)

    def _sni_call(self, _c, _s, _p, _i, method, params, inv):
        if method in ("Activate", "SecondaryActivate") and self.on_activate:
            self.on_activate()
        inv.return_value(None)

    # -- dbusmenu ----------------------------------------------------------
    def _menu_get(self, _c, _s, _p, _i, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _layout(self):
        """Root node plus one level of children, as a PLAIN (ia{sv}av) tuple.

        Returned unwrapped on purpose. GLib.Variant() builds a value out of
        Python primitives and cannot consume an already-built Variant for a
        nested field, so the whole reply has to be constructed in one call.
        The `av` children are the exception: every element of a variant array
        must itself be a Variant.
        """
        kids = [GLib.Variant("(ia{sv}av)", (it.id, it.props(), []))
                for it in self.items]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, kids)

    def _menu_call(self, _c, _s, _p, _i, method, params, inv):
        if method == "GetLayout":
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            out = [(it.id, it.props()) for it in self.items if not ids or it.id in ids]
            inv.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            mid, name = params.unpack()[0], params.unpack()[1]
            item = next((i for i in self.items if i.id == mid), None)
            val = item.props().get(name) if item else None
            inv.return_value(GLib.Variant("(v)", (val or GLib.Variant("s", ""),)))
        elif method == "Event":
            mid, event_id = params.unpack()[0], params.unpack()[1]
            if event_id == "clicked":
                item = next((i for i in self.items if i.id == mid), None)
                if item and item.on_click:
                    if item.toggle:
                        item.checked = not item.checked
                    item.on_click(item)
                    self.update()
            inv.return_value(None)
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        else:
            inv.return_value(None)
