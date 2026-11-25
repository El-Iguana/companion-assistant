"""#112: a shared Orpheus device sees only shared playlists, so Cantinero binds itself."""
import asyncio

from services.skills import SkillContext, media
from services.skills import orpheus_media as om


def _ctx(tmp_path):
    return SkillContext(user_id="u1", root=tmp_path)


def _fake(monkeypatch, binding="shared", profiles=({"id": "P1", "name": "admin"},), fail=False):
    bound = []

    async def me(base, token):
        if fail:
            raise om.OrpheusError("SERVER_UNREACHABLE", "down")
        return {"binding": binding, "profile": {"name": "sam"} if binding == "bound" else None}

    async def profs(base, token):
        return list(profiles)

    async def bind(base, token, pid):
        bound.append(pid)
        return {}
    monkeypatch.setattr(om, "me", me)
    monkeypatch.setattr(om, "profiles", profs)
    monkeypatch.setattr(om, "bind_self", bind)
    return bound


def _paired(tmp_path):
    st = {"url": "http://o", "device_id": "d", "token": "t"}
    om.save(tmp_path, st)
    return st


def test_a_shared_device_binds_to_the_only_profile_once(tmp_path, monkeypatch):
    bound = _fake(monkeypatch)
    ctx, st = _ctx(tmp_path), _paired(tmp_path)
    assert asyncio.run(media._ensure_profile(ctx, st)) == {"profile": "admin"}
    assert bound == ["P1"] and om.load(tmp_path)["profile"] == "admin"
    assert asyncio.run(media._ensure_profile(ctx, om.load(tmp_path))) == {"profile": "admin"}
    assert bound == ["P1"]                                   # remembered: not asked again


def test_an_already_bound_device_is_left_alone(tmp_path, monkeypatch):
    bound = _fake(monkeypatch, binding="bound")
    ctx, st = _ctx(tmp_path), _paired(tmp_path)
    assert asyncio.run(media._ensure_profile(ctx, st)) == {"profile": "sam"}
    assert bound == []


def test_several_profiles_are_named_not_guessed(tmp_path, monkeypatch):
    bound = _fake(monkeypatch, profiles=({"id": "P1", "name": "admin"}, {"id": "P2", "name": "Sam"}))
    ctx, st = _ctx(tmp_path), _paired(tmp_path)
    assert asyncio.run(media._ensure_profile(ctx, st)) == {"choose_profile": ["admin", "Sam"]}
    assert bound == [] and "profile" not in om.load(tmp_path)  # looked at again next time


def test_an_unreachable_server_never_fails_the_play(tmp_path, monkeypatch):
    _fake(monkeypatch, fail=True)
    ctx, st = _ctx(tmp_path), _paired(tmp_path)
    assert asyncio.run(media._ensure_profile(ctx, st)) == {}
    assert "profile" not in om.load(tmp_path)


def test_pairing_binds_the_new_device(tmp_path, monkeypatch):
    bound = _fake(monkeypatch)

    async def discover(saved=""):
        return "http://homelab:8096", {"product": "orpheus", "server_id": "S1", "name": "Orpheus"}

    async def start(base):
        return {"pairing_id": "p1", "code": "482193", "expires_at": "later"}

    async def poll(base, pid):
        return {"device_id": "dev1", "token": "TOKEN-xyz"}
    monkeypatch.setattr(om, "discover", discover)
    monkeypatch.setattr(om, "start_pairing", start)
    monkeypatch.setattr(om, "poll_pairing", poll)
    ctx = _ctx(tmp_path)
    assert asyncio.run(media._connect(ctx, {}))["status"] == "waiting_for_approval"
    assert asyncio.run(media._connect(ctx, {})) == {"ok": True, "status": "paired", "server": "Orpheus"}
    assert bound == ["P1"] and om.load(tmp_path)["profile"] == "admin"


def test_pairing_with_several_profiles_says_how_to_bind(tmp_path, monkeypatch):
    _fake(monkeypatch, profiles=({"id": "P1", "name": "admin"}, {"id": "P2", "name": "Sam"}))

    async def discover(saved=""):
        return "http://homelab:8096", {"product": "orpheus", "server_id": "S1", "name": "Orpheus"}

    async def start(base):
        return {"pairing_id": "p1", "code": "1", "expires_at": "later"}

    async def poll(base, pid):
        return {"device_id": "dev1", "token": "T"}
    monkeypatch.setattr(om, "discover", discover)
    monkeypatch.setattr(om, "start_pairing", start)
    monkeypatch.setattr(om, "poll_pairing", poll)
    ctx = _ctx(tmp_path)
    asyncio.run(media._connect(ctx, {}))
    done = asyncio.run(media._connect(ctx, {}))
    assert done["status"] == "paired" and done["profiles"] == ["admin", "Sam"] and "dashboard" in done["how"]
