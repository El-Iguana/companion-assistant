"""HTTP client for the Orpheus media server (El-Iguana/orpheus), as a ``controller`` device.

Contract (Orpheus docs/phase1-server-spec.md, architecture.md §5):
  GET  /api/v1/server               {product: "orpheus", server_id, name, api_version}; no auth
  POST /api/v1/pair                 {name, role, platform, app_version} -> {pairing_id, code, expires_at}
  GET  /api/v1/pair/{pairing_id}    pending, or {device_id, token} ONCE after a human approves
  POST /api/v1/resolve              {query, kinds?, limit?} -> {candidates, needs_disambiguation, …}
  GET  /api/v1/devices              the registered devices (bearer token)
  GET  /api/v1/me                   this device's binding: "bound" to a profile, or "shared"
  GET  /api/v1/profiles             every profile: id, name
  PUT  /api/v1/devices/self/profile {profile_id} binds this device (phase 4 AD-2)

The server's address comes from configuration (``ORPHEUS_MEDIA_URL``), never from the model.
The token lives in ``data/u/<uid>/media/orpheus.json`` and never reaches a prompt or a
tool result. Named ``orpheus_media`` because ``ORPHEUS_URL`` once meant the Orpheus TTS voice.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx

TIMEOUT = httpx.Timeout(8.0, connect=4.0)
APP_VERSION = "cantinero"


def configured_url() -> str:
    return (os.environ.get("ORPHEUS_MEDIA_URL") or "").strip().rstrip("/")


SCAN_PORT = int(os.environ.get("ORPHEUS_MEDIA_PORT", "8096") or 8096)
SCAN_MAX_HOSTS = 254                 # a home /24; anything wider is refused, not scanned


async def discover(saved_url: str = "") -> tuple[str, dict]:
    """Find the server: ORPHEUS_MEDIA_URL, else the saved address, else a scan of the LAN
    (ORPHEUS_MEDIA_SCAN_CIDR, from run-podman.sh) for ``GET /api/v1/server`` answering as
    Orpheus. The address is never something the model said. Returns (url, server info)."""
    for url in (configured_url(), saved_url):
        if url:
            try:
                return url, await server_info(url)
            except OrpheusError:
                pass
    cidr = (os.environ.get("ORPHEUS_MEDIA_SCAN_CIDR") or "").strip()
    if not cidr:
        raise OrpheusError("NOT_CONFIGURED", "no music server address, and no network to look on")
    import asyncio
    import ipaddress

    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise OrpheusError("NOT_CONFIGURED", "the network to look on isn't valid") from None
    if net.version != 4 or net.num_addresses > SCAN_MAX_HOSTS + 2 or not net.is_private:
        raise OrpheusError("NOT_CONFIGURED", "the network to look on is too wide or not private")
    quick = httpx.Timeout(1.5, connect=0.8)

    async def probe(host: str):
        url = f"http://{host}:{SCAN_PORT}"
        try:
            async with httpx.AsyncClient(timeout=quick) as c:
                r = await c.get(f"{url}/api/v1/server")
            info = r.json()
            return (url, info) if info.get("product") == "orpheus" else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    sem = asyncio.Semaphore(64)

    async def limited(h):
        async with sem:
            return await probe(h)
    for hit in await asyncio.gather(*(limited(str(h)) for h in net.hosts())):
        if hit:
            return hit
    raise OrpheusError("SERVER_NOT_FOUND", "no Orpheus server answered on the local network")


# --- per-user state ----------------------------------------------------------------------

def _path(root: Path) -> Path:
    return root / "media" / "orpheus.json"


def load(root: Path) -> dict:
    try:
        return json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}


def save(root: Path, state: dict) -> None:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.chmod(0o600)
    tmp.replace(p)


def paired(state: dict) -> bool:
    return bool(state.get("token") and state.get("device_id"))


# --- calls ---------------------------------------------------------------------------------

class OrpheusError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def _request(method: str, url: str, token: str = "", **kw) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.request(method, url, headers=headers, **kw)
    except httpx.HTTPError as e:
        raise OrpheusError("SERVER_UNREACHABLE", f"the music server didn't answer ({type(e).__name__})") from e
    if r.status_code == 401:
        raise OrpheusError("NOT_PAIRED", "the music server no longer accepts this device")
    if r.status_code == 404:
        raise OrpheusError("NOT_FOUND", "the music server doesn't know that")
    if r.status_code >= 400:
        try:
            err = (r.json() or {}).get("error") or {}
        except ValueError:
            err = {}
        code = str(err.get("code") or "")
        # Phase 5 commands: 409 carries the player's refusal (offline, autoplay_blocked,
        # nothing_playing), 422 nothing_to_play, 504 a player that didn't answer.
        if r.status_code == 409 and code:
            raise OrpheusError(code.upper(), str(err.get("message") or code))
        if r.status_code == 422:
            raise OrpheusError("NOTHING_TO_PLAY", "nothing in that can be played")
        if r.status_code == 504:
            raise OrpheusError("PLAYER_TIMEOUT", "the player didn't answer")
        raise OrpheusError("SERVER_ERROR", f"the music server answered {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise OrpheusError("SERVER_ERROR", "the music server sent something unreadable") from e


async def server_info(base: str) -> dict:
    info = await _request("GET", f"{base}/api/v1/server")
    if info.get("product") != "orpheus":
        raise OrpheusError("NOT_ORPHEUS", "that address isn't an Orpheus server")
    return info


async def start_pairing(base: str) -> dict:
    return await _request("POST", f"{base}/api/v1/pair", json={
        "name": f"Cantinero on {socket.gethostname()}"[:60], "role": "controller",
        "platform": "linux", "app_version": APP_VERSION})


async def poll_pairing(base: str, pairing_id: str) -> dict:
    return await _request("GET", f"{base}/api/v1/pair/{pairing_id}")


async def me(base: str, token: str) -> dict:
    return await _request("GET", f"{base}/api/v1/me", token=token)


async def profiles(base: str, token: str) -> list[dict]:
    out = await _request("GET", f"{base}/api/v1/profiles", token=token)
    return out.get("profiles", []) if isinstance(out, dict) else out


async def bind_self(base: str, token: str, profile_id: str) -> dict:
    return await _request("PUT", f"{base}/api/v1/devices/self/profile", token=token,
                          json={"profile_id": profile_id})


async def resolve(base: str, token: str, query: str, kinds: list[str] | None, limit: int = 5) -> dict:
    body: dict = {"query": query, "limit": limit}
    if kinds:
        body["kinds"] = kinds
    return await _request("POST", f"{base}/api/v1/resolve", token=token, json=body)


async def command(base: str, token: str, device_id: str, body: dict) -> dict:
    """Phase 5: one command to a player, answered with its ack (raises on a refusal)."""
    return await _request("POST", f"{base}/api/v1/devices/{device_id}/commands", token=token, json=body)


async def devices(base: str, token: str) -> list[dict]:
    out = await _request("GET", f"{base}/api/v1/devices", token=token)
    return out.get("devices", out) if isinstance(out, dict) else out


# --- what a play expands to (RESIDENT_PLAN.md R-7: Cantinero's own player) ---------------------

QUEUE_MAX = 500


def _entry(item: dict, artist: str = "", album: str = "") -> dict:
    return {"id": int(item["id"]), "identity_key": item.get("identity_key") or "",
            "title": item.get("title") or "", "artist": artist, "album": album,
            "artwork_id": item.get("artwork_id"), "duration_ms": item.get("duration_ms")}


async def expand(base: str, token: str, *, item_id: int | None = None, playlist_id: str = "") -> list[dict]:
    """What a play of this item or playlist means, in order: a track, an album's tracks, an
    artist's albums' tracks, a playlist's present entries. Each entry carries what the player
    shows (title, artist, album, artwork id, duration). Unplayable tracks are left out."""
    if playlist_id:
        pl = await _request("GET", f"{base}/api/v1/playlists/{playlist_id}", token=token)
        out = []
        for e in pl.get("entries") or []:
            t = e.get("track")
            if t and not e.get("missing") and t.get("playable", True):
                out.append(_entry(t, e.get("artist") or t.get("artist", ""), e.get("album") or t.get("album", "")))
        return out[:QUEUE_MAX]
    item = await _request("GET", f"{base}/api/v1/items/{item_id}", token=token)
    kind = item.get("kind")

    async def album_tracks(album: dict, artist: str = "") -> list[dict]:
        if not artist:
            artist = ((album.get("album") or {}).get("album_artist") or {}).get("title", "")
        kids = (await _request("GET", f"{base}/api/v1/items/{album['id']}/children", token=token)).get("items") or []
        kids = sorted((k for k in kids if k.get("kind") == "track" and k.get("playable")),
                      key=lambda k: (k.get("disc_number") or 0, k.get("track_number") or 0, k.get("sort_title") or ""))
        return [_entry(k, artist, album.get("title", "")) for k in kids]

    if kind == "track":
        album = item.get("parent") or {}
        artist = ""
        if album.get("id"):
            full = await _request("GET", f"{base}/api/v1/items/{album['id']}", token=token)
            artist = ((full.get("album") or {}).get("album_artist") or {}).get("title", "")
        return [_entry(item, artist, album.get("title", ""))] if item.get("playable") else []
    if kind == "album":
        return (await album_tracks(item))[:QUEUE_MAX]
    if kind == "artist":
        albums = (await _request("GET", f"{base}/api/v1/items/{item_id}/children", token=token)).get("items") or []
        out: list[dict] = []
        for a in albums:
            if a.get("kind") == "album":
                out += await album_tracks(a, item.get("title", ""))
            if len(out) >= QUEUE_MAX:
                break
        return out[:QUEUE_MAX]
    raise OrpheusError("NOT_MUSIC", "that isn't music")
