"""Smoke test for the pytincture page: every screen, zero console errors, screenshots.

    PANTHEON_URL=http://127.0.0.1:7867 PANTHEON_USER=… PANTHEON_PASS=… \\
    CHROME=~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome \\
      uv run --with playwright python tests/smoke_web.py [shots_dir]

Writes to the library and talks to KoboldCPP: it starts chats, sends messages, takes
swipes, edits and deletes messages, and switches the theme to Ember and back. Run it
against a scratch library (PANTHEON_DATA_DIR), never your real one — it signs in as a
real account. Needs KoboldCPP up (PANTHEON_SKIP_CHAT=1 skips the chat steps).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = os.environ.get("PANTHEON_URL", "http://127.0.0.1:7867").rstrip("/")
USER = os.environ["PANTHEON_USER"]
PASS = os.environ["PANTHEON_PASS"]
CHROME = os.path.expanduser(os.environ.get("CHROME", ""))
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/shots")
ROUTES = ["discover", "create", "chats", "personas", "settings"]


def _idle(page, timeout=180_000):
    """Wait until no reply is streaming (the send button is not Stop)."""
    page.wait_for_function("!document.querySelector('#ch-send.stop')", timeout=timeout)
    page.wait_for_timeout(600)


def _send(page, text):
    page.fill("#ch-input", text)
    page.keyboard.press("Enter")


def chat_steps(page) -> None:
    """Phase 3: the chat view, against the live backend."""
    # The chat started above is fresh: only the greeting.
    page.wait_for_selector("#ch-msgs .pt-msg", timeout=30_000)
    assert page.locator("#ch-msgs .pt-msg").count() == 1, "a fresh chat has only the greeting"
    sugs = page.locator(".pt-sug")
    if sugs.count():
        page.screenshot(path=str(SHOTS / "chat-fresh.png"))
        sugs.first.click()
    else:
        _send(page, "Hello! Tell me one thing about yourself in a sentence.")
    page.wait_for_selector("#ch-live", timeout=10_000)
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "chat-streaming.png"))
    _idle(page)
    assert page.locator(".pt-sug").count() == 0 or page.locator("#ch-sugs[hidden]").count() == 1
    assert page.locator("#ch-msgs .pt-msg.user").count() == 1
    assert page.locator("#ch-msgs .pt-msg.char").count() == 2, "greeting + reply"

    # another take: › past the end, then ‹ back
    page.hover("#ch-msgs .pt-msg.char >> nth=-1")
    page.click("#ch-msgs .pt-msg.char >> nth=-1 >> [data-swipe='1']")
    _idle(page)
    assert "2/2" in page.inner_text("#ch-msgs .pt-msg.char >> nth=-1 >> .pt-swipe")
    page.hover("#ch-msgs .pt-msg.char >> nth=-1")
    page.click("#ch-msgs .pt-msg.char >> nth=-1 >> [data-swipe='-1']")
    page.wait_for_timeout(800)
    assert "1/2" in page.inner_text("#ch-msgs .pt-msg.char >> nth=-1 >> .pt-swipe")
    page.screenshot(path=str(SHOTS / "chat.png"))

    # edit your message in place (Save, not resend)
    page.hover("#ch-msgs .pt-msg.user")
    page.click("#ch-msgs .pt-msg.user >> [data-mmenu]")
    page.click(".wapyt-cmenu-item:has-text('Edit & resend')")
    page.wait_for_selector("#ch-edit", timeout=5_000)
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('.pt-chead').getBoundingClientRect().top") >= 0, \
        "focusing the editor must not scroll the chat header away"
    page.screenshot(path=str(SHOTS / "chat-edit.png"))
    page.fill("#ch-edit", "An edited question, smoke test.")
    page.click("[data-edit=save]")
    page.wait_for_function("document.querySelector('#ch-msgs .pt-msg.user .pt-bubble')"
                           "?.textContent.includes('edited question')", timeout=10_000)
    assert page.evaluate("document.querySelector('.pt-chead').getBoundingClientRect().top") >= 0, \
        "the chat header must not scroll out of view"

    # delete the reply
    before = page.locator("#ch-msgs .pt-msg").count()
    page.hover("#ch-msgs .pt-msg.char >> nth=-1")
    page.click("#ch-msgs .pt-msg.char >> nth=-1 >> [data-mmenu]")
    page.click(".wapyt-cmenu-item:has-text('Delete')")
    page.wait_for_timeout(800)
    assert page.locator("#ch-msgs .pt-msg").count() == before - 1, "message deleted"

    # stop mid-reply: the turn is not saved and the text comes back
    text = "Please describe the room around you in great detail, at length."
    _send(page, text)
    page.wait_for_selector("#ch-send.stop", timeout=10_000)
    page.click("#ch-send")
    _idle(page)
    assert page.input_value("#ch-input") == text, "stopped text is restored to the input"


def studio_steps(page) -> None:
    """Phase 4: create, edit, draft restore, VRM import, duplicate."""
    page.evaluate("location.hash = '#/create'")
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    page.fill('[data-f="name"]', "Smoke Tester")
    page.fill('[data-f="creator_notes"]', "Exists only to be tested")
    page.fill('[data-f="tags"]', "test, smoke")
    page.fill("[data-greet]", "*{{char}} waves.* Hello, {{user}}!")
    page.click('[data-act="add-greet"]')
    page.fill('[data-rep="greet"] >> nth=1 >> textarea', "Back again?")
    page.click('[data-act="add-starter"]')
    page.fill("[data-starter]", "Run the tests")
    page.click('[data-act="add-lore"]')
    page.fill('[data-lore="keys"]', "smoke")
    page.fill('[data-lore="content"]', "Smoke is what the tests make.")
    page.wait_for_timeout(300)
    preview = page.inner_text("#st-preview")
    assert "Smoke Tester" in preview and "Hello," in preview and "Run the tests" in preview, preview
    page.screenshot(path=str(SHOTS / "studio-new.png"))
    page.click('[data-act="save"]')
    page.wait_for_function("/^#\\/create\\/.+/.test(location.hash)", timeout=15_000)
    card_id = page.evaluate("location.hash.split('/')[2]")
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    assert page.input_value('[data-f="name"]') == "Smoke Tester"
    assert page.locator('[data-rep="greet"]').count() == 2
    assert page.input_value("[data-starter]") == "Run the tests"
    assert page.input_value('[data-lore="keys"]') == "smoke"

    # edit + save, then the profile shows it
    page.fill('[data-f="creator_notes"]', "Edited tagline")
    page.click('[data-act="save"]')
    page.wait_for_function("document.querySelector('#st-status').textContent === 'Saved'", timeout=15_000)
    page.evaluate(f"location.hash = '#/character/{card_id}'")
    page.wait_for_selector(".pt-prof-head h2", timeout=20_000)
    assert "Edited tagline" in page.inner_text(".pt-prof-head")
    assert page.locator(".pt-greet").count() and "2" in page.inner_text(".pt-prof-sec .pt-swipe")
    page.keyboard.press("Escape")

    # draft: leave with unsaved changes, come back, restore
    page.evaluate(f"location.hash = '#/create/{card_id}'")
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    page.fill('[data-f="personality"]', "unsaved draft text")
    page.evaluate("location.hash = '#/discover'")
    page.wait_for_timeout(500)
    page.evaluate(f"location.hash = '#/create/{card_id}'")
    page.wait_for_selector('[data-act="restore"]', timeout=20_000)
    page.click('[data-act="restore"]')
    page.wait_for_timeout(300)
    assert page.input_value('[data-f="personality"]') == "unsaved draft text"
    page.click('[data-act="save"]')
    page.wait_for_function("document.querySelector('#st-status').textContent === 'Saved'", timeout=15_000)

    # 3D avatar: import the repo's baseline model, see its audit
    baseline = Path(__file__).resolve().parents[1] / "data" / "vrm" / "baseline.vrm"
    if baseline.exists():
        page.check('input[name="st-avatar"][value="vrm"]')
        page.set_input_files("#st-vrm-file", str(baseline))
        page.wait_for_function("document.querySelector('#st-vrm-info').textContent.includes('triangles')",
                               timeout=60_000)
        page.screenshot(path=str(SHOTS / "studio-vrm.png"))
        page.check('input[name="st-avatar"][value="sprite"]')
    page.click('[data-jump="look"]')
    page.wait_for_timeout(600)
    page.screenshot(path=str(SHOTS / "studio-sprites.png"))

    # duplicate: the VRM import left unsaved changes, so it asks first
    page.click('[data-act="duplicate"]')
    page.wait_for_selector(".wapyt-msg-dialog", timeout=5_000)
    page.click(".wapyt-msg-dialog .wapyt-msg-btn:has-text('Duplicate')")
    page.wait_for_function(f"location.hash.startsWith('#/create/') && !location.hash.endsWith('{card_id}')",
                           timeout=15_000)
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    assert page.input_value('[data-f="name"]') == "Smoke Tester (copy)"

    # import: export the copy as a V2 PNG card, then import it back as a new character
    copy_id = page.evaluate("location.hash.split('/')[2]")
    png = page.request.get(f"{URL}/pmedia/card/{copy_id}/export.png?version=2")
    assert png.ok and png.body()[:8] == b"\x89PNG\r\n\x1a\n", "export should be a PNG card"
    card_file = SHOTS / "smoke-export.png"
    card_file.write_bytes(png.body())
    page.evaluate("location.hash = '#/create'")
    page.wait_for_selector("#st-import", state="attached", timeout=20_000)
    page.set_input_files("#st-import", str(card_file))
    page.wait_for_function(f"location.hash.startsWith('#/create/') && !location.hash.endsWith('{copy_id}')",
                           timeout=20_000)
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    assert page.input_value('[data-f="name"]') == "Smoke Tester (copy)", "imported card keeps its name"
    assert page.input_value("[data-starter]") == "Run the tests", "starters round-trip through V2 export"
    return card_id


def phase6_steps(page, card_id: str) -> None:
    """Voice controls, the wake-word gate, the mic picker and the 3D avatar.

    Chromium runs with a fake microphone (a tone, or PANTHEON_FAKE_AUDIO's WAV), so
    hands-free really opens a device. Speech itself needs the TTS/STT models, which only
    the container has: PANTHEON_VOICE=1 adds the steps that need them.
    """
    page.goto(f"{URL}/pantheon/#/character/{card_id}")
    page.wait_for_selector("[data-act=start]", timeout=120_000)
    page.click("[data-act=start]")
    page.wait_for_function("location.hash.startsWith('#/chat/')", timeout=15_000)
    page.wait_for_selector("#ch-hf", timeout=20_000)
    page.wait_for_function("!!window.PantheonVoice", timeout=10_000)

    # speaker: a per-browser preference
    page.click("#ch-speaker")
    page.wait_for_selector("#ch-speaker.on", timeout=3_000)
    assert page.evaluate("localStorage.getItem('pantheon.speaker')") == "1"
    page.click("#ch-speaker")
    page.wait_for_selector("#ch-speaker:not(.on)", timeout=3_000)

    # hands-free opens the (fake) microphone, and off closes it
    page.click("#ch-hf")
    page.wait_for_selector("#ch-hf.on", timeout=10_000)
    page.screenshot(path=str(SHOTS / "chat-handsfree.png"))
    page.click("#ch-hf")
    page.wait_for_selector("#ch-hf:not(.on)", timeout=5_000)
    assert not page.evaluate("PantheonVoice.state().hfActive")

    # the wake-word gate (pure logic; nothing is persisted by setSettings)
    gate = page.evaluate("""() => {
        const V = PantheonVoice, out = [];
        V.setSettings({wake_word: "luna", wake_mode: "phrase"});
        out.push(V._gate("Luna, what time is it?"), V._gate("what time is it"));
        V.setSettings({wake_mode: "continuous", sleep_phrase: "that will be all."});
        out.push(V._gate("Luna"), V.state().awake, V._gate("and tomorrow?"),
                 V._gate("That'll be all"), V.state().awake);
        V.setSettings({wake_word: "", wake_mode: "phrase"});
        out.push(V._gate("anything at all"));
        return out;
    }""")
    assert gate == ["what time is it?", "", "", True, "and tomorrow?", "", False, "anything at all"], gate
    spoken = page.evaluate("PantheonVoice._clean('| a | b |\\n|---|---|\\n*waves* ~150 [expr:joy]')")
    assert spoken == "a, b. about 150", spoken

    # the 3D avatar: put the card on the VRM tier, open a chat, see the canvas
    page.evaluate(f"location.hash = '#/create/{card_id}'")
    page.wait_for_selector('[data-f="name"]', timeout=20_000)
    model = page.evaluate("[...document.querySelectorAll('[data-f=vrm] option')].map(o => o.value).find(v => v)")
    if model:
        page.check('input[name="st-avatar"][value="vrm"]')
        page.select_option('[data-f="vrm"]', model)
        page.click('[data-act="save"]')
        page.wait_for_function("document.querySelector('#st-status').textContent === 'Saved'", timeout=15_000)
        page.go_back()
        page.wait_for_function("location.hash.startsWith('#/chat/')", timeout=10_000)
        page.wait_for_selector("#ch-avatar-slot canvas.pt-avatar:not([hidden])", timeout=60_000)
        assert page.evaluate("document.body.classList.contains('pt-avatar-on')")
        assert page.locator("#ch-portrait:not([hidden])").count() == 0, "sprite hides under the avatar"
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "chat-avatar.png"))
        # leaving the chat parks the canvas and stops the renderer
        page.evaluate("location.hash = '#/discover'")
        page.wait_for_timeout(800)
        assert not page.evaluate("document.body.classList.contains('pt-avatar-on')")
        page.evaluate(f"location.hash = '#/create/{card_id}'")
        page.wait_for_selector('[data-f="name"]', timeout=20_000)
        page.check('input[name="st-avatar"][value="sprite"]')
        page.click('[data-act="save"]')
        page.wait_for_function("document.querySelector('#st-status').textContent === 'Saved'", timeout=15_000)

    # settings: the microphone picker lists the fake device
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector("#set-mic", timeout=20_000)
    page.click("[data-act=mics]")
    page.wait_for_function("document.querySelectorAll('#set-mic option').length > 1", timeout=10_000)
    page.locator("#set-voice").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / "settings-mic.png"))

    # settings: the context window round-trips, then goes back to Auto
    page.select_option("#set-ctx", "16384")
    page.click("[data-act=save]")
    page.wait_for_function("document.querySelector('#set-status').textContent === 'Saved'", timeout=15_000)
    page.reload()
    page.wait_for_selector("#set-ctx", timeout=20_000)
    assert page.input_value("#set-ctx") == "16384"
    page.locator("#set-ctx").scroll_into_view_if_needed()
    page.screenshot(path=str(SHOTS / "settings-context.png"))
    page.select_option("#set-ctx", "0")
    page.click("[data-act=save]")
    page.wait_for_function("document.querySelector('#set-status').textContent === 'Saved'", timeout=15_000)

    if os.environ.get("PANTHEON_VOICE"):
        voice_steps(page, card_id)


def voice_steps(page, card_id: str) -> None:
    """Needs the TTS/STT models (the container) and, for push-to-talk, a fake mic WAV
    of speech (PANTHEON_FAKE_AUDIO)."""
    tts = page.evaluate("""async () => {
        const csrf = document.cookie.split(';').map(s => s.trim()).find(p => /csrf/i.test(p.split('=')[0]));
        const r = await fetch('/pmedia/tts', {method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(csrf.split('=')[1])},
            body: JSON.stringify({text: 'Hello there.', voice: 'af_sarah', speed: 1})});
        return [r.status, r.headers.get('content-type'), (await r.arrayBuffer()).byteLength];
    }""")
    assert tts[0] == 200 and "audio/wav" in tts[1] and tts[2] > 10_000, tts
    # outside the browser, so the expected 403 is not a console error
    no_csrf = page.request.post(f"{URL}/pmedia/tts", data={"text": "x"}).status
    assert no_csrf == 403, no_csrf
    # read aloud a reply: playback starts (speaking goes true)
    page.goto(f"{URL}/pantheon/#/character/{card_id}")
    page.wait_for_selector("[data-act=start]", timeout=120_000)
    page.click("[data-act=start]")
    page.wait_for_selector("#ch-msgs .pt-msg.char", timeout=30_000)
    page.hover("#ch-msgs .pt-msg.char >> nth=-1")
    page.click("#ch-msgs .pt-msg.char >> nth=-1 >> [data-mmenu]")
    page.click(".wapyt-cmenu-item:has-text('Read aloud')")
    page.wait_for_function("PantheonVoice.state().speaking", timeout=20_000)
    page.evaluate("PantheonVoice.stop()")
    if os.environ.get("PANTHEON_FAKE_AUDIO"):
        # push-to-talk: hold for the length of the fake recording, release, see it sent
        before = page.locator("#ch-msgs .pt-msg.user").count()
        page.hover("#ch-ptt")
        page.mouse.down()
        page.wait_for_selector("#ch-ptt.recording", timeout=10_000)
        page.wait_for_timeout(int(os.environ.get("PANTHEON_FAKE_AUDIO_MS", "4000")))
        page.mouse.up()
        page.wait_for_function(f"document.querySelectorAll('#ch-msgs .pt-msg.user').length > {before}",
                               timeout=60_000)
        heard = page.inner_text("#ch-msgs .pt-msg.user >> nth=-1 >> .pt-bubble")
        print("push-to-talk heard:", heard)
        page.wait_for_selector("#ch-send.stop", timeout=10_000)
        page.click("#ch-send")                            # stop the reply; not needed here
        _idle(page)


def switchover_steps(browser, page, card_id: str, errors: list) -> None:
    """Phase 7: one server. Redirects, the health probe, and the desktop avatar's page."""
    r = page.request.get(f"{URL}/", max_redirects=0)
    assert r.status in (302, 307) and r.headers["location"].rstrip("/").endswith("/pantheon"), (r.status, r.headers)
    assert page.request.get(f"{URL}/api/health").json() == {"ok": True}
    local = URL.replace("127.0.0.1", "localhost")
    r = page.request.get(f"{local}/pantheon/?x=1", max_redirects=0)
    assert r.status == 307 and r.headers["location"] == f"{URL}/pantheon/?x=1", (r.status, r.headers)

    # signed in: the desktop page boots on voice.js, and the tray hooks answer
    desk = browser.new_page(viewport={"width": 420, "height": 640},
                            storage_state=page.context.storage_state())
    desk.on("console", lambda m: errors.append(f"[desktop] {m.text}") if m.type == "error" else None)
    desk.on("pageerror", lambda e: errors.append(f"[desktop] pageerror {e}"))
    desk.goto(f"{URL}/desktop")
    desk.wait_for_function("window.__desktopReady === true", timeout=30_000)
    chars = desk.evaluate("window.__desktopCharacters()")
    assert any(c["id"] == card_id for c in chars), chars
    assert desk.evaluate(f"window.__desktopSetCharacter({card_id!r})") is True
    desk.wait_for_timeout(600)
    cur = [c for c in desk.evaluate("window.__desktopCharacters()") if c["current"]]
    assert [c["id"] for c in cur] == [card_id], cur
    listening = desk.evaluate("window.__desktopListening()")
    assert set(listening) >= {"on", "wake", "sleep", "mode", "awake", "duplex"}, listening
    assert desk.evaluate("PantheonVoice.state().speaker"), "voice is on by default on the desktop"
    if not os.environ.get("PANTHEON_SKIP_CHAT"):
        # a spoken turn, as voice.js would deliver it: the reply streams into the caption.
        # Speech output needs the TTS models (the container); mute it elsewhere.
        if not os.environ.get("PANTHEON_VOICE"):
            desk.evaluate("PantheonVoice.speaker(false)")
        desk.evaluate("window.dispatchEvent(new CustomEvent('pvoice:send', "
                      "{detail: {text: 'Say hello in five words.'}}))")
        desk.wait_for_function("document.getElementById('dt-state').textContent === 'thinking…'", timeout=10_000)
        desk.wait_for_function("document.getElementById('dt-state').textContent !== 'thinking…'", timeout=180_000)
        said = desk.inner_text("#dt-caption")
        assert said and said != "…", f"no reply in the caption: {said!r}"
    desk.hover("#dt-bar")
    desk.wait_for_timeout(300)
    desk.screenshot(path=str(SHOTS / "desktop.png"))
    desk.close()

    # signed out: /desktop -> login -> back to /desktop
    ctx = browser.new_context(viewport={"width": 420, "height": 640})
    anon = ctx.new_page()
    anon.goto(f"{URL}/desktop")
    anon.wait_for_selector("input[name=email]", timeout=30_000)
    anon.fill("input[name=email]", USER)
    anon.fill("input[name=password]", PASS)
    anon.keyboard.press("Enter")
    anon.wait_for_url(f"{URL}/desktop", timeout=30_000)
    anon.wait_for_function("window.__desktopReady === true", timeout=30_000)
    ctx.close()


def _confirm(page, label):
    page.wait_for_selector(".wapyt-msg-dialog", timeout=5_000)
    page.click(f".wapyt-msg-dialog .wapyt-msg-btn:has-text('{label}')")


def phase5_steps(page) -> None:
    """Personas, settings, password, users, guide, chat memory."""
    # personas: create, use, delete
    page.evaluate("location.hash = '#/personas'")
    page.wait_for_selector("[data-new]", timeout=20_000)
    page.click("[data-new]")
    page.fill("#pe-name", "Smoke Persona")
    page.fill("#pe-title", "Test pilot")
    page.uncheck("#pe-default")
    page.click("[data-save]")
    page.wait_for_selector(".pt-pcard:has-text('Smoke Persona')", timeout=10_000)
    page.screenshot(path=str(SHOTS / "personas.png"))
    before = page.inner_text("#pt-persona-name")
    page.locator(".pt-pcard:has-text('Smoke Persona') [data-use]").first.click()
    page.wait_for_function("document.querySelector('#pt-persona-name').textContent === 'Smoke Persona'", timeout=10_000)
    page.click(f".pt-pcard:has-text('{before}') [data-use]")         # back to the original
    page.wait_for_function(f"document.querySelector('#pt-persona-name').textContent === '{before}'", timeout=10_000)
    n = page.locator(".pt-pcard:has-text('Smoke Persona')").count()
    page.locator(".pt-pcard:has-text('Smoke Persona')").first.click()
    page.click(".pt-modal [data-delete]")
    _confirm(page, "Delete")
    page.wait_for_function(f"document.querySelectorAll('.pt-pcard').length && "
                           f"[...document.querySelectorAll('.pt-pcard')].filter(c => c.innerText.includes('Smoke Persona')).length === {n - 1}",
                           timeout=10_000)

    # settings: change, save, reload, restore
    page.evaluate("location.hash = '#/settings'")
    page.wait_for_selector('[data-s="disable_thinking"]', timeout=20_000)
    was = page.is_checked('[data-s="disable_thinking"]')
    page.click('[data-s="disable_thinking"]')
    page.click('[data-act="save"]')
    page.wait_for_function("document.querySelector('#set-status').textContent === 'Saved'", timeout=10_000)
    page.screenshot(path=str(SHOTS / "settings.png"), full_page=False)
    page.reload()
    page.wait_for_selector('[data-s="disable_thinking"]', timeout=120_000)
    assert page.is_checked('[data-s="disable_thinking"]') != was, "setting persisted"
    page.click('[data-s="disable_thinking"]')
    page.click('[data-act="save"]')
    page.wait_for_function("document.querySelector('#set-status').textContent === 'Saved'", timeout=10_000)

    # password: change and change back
    page.fill("#pw-cur", PASS)
    page.fill("#pw-new", PASS + "-x")
    page.click('[data-act="password"]')
    page.wait_for_function("document.querySelector('#pw-cur').value === ''", timeout=10_000)
    page.fill("#pw-cur", PASS + "-x")
    page.fill("#pw-new", PASS)
    page.click('[data-act="password"]')
    page.wait_for_function("document.querySelector('#pw-cur').value === ''", timeout=10_000)

    # users (the test account is an admin): add, reset, delete
    if page.locator("#set-users").count():
        page.wait_for_selector("#nu-name", timeout=10_000)
        page.fill("#nu-name", "smoke-user")
        page.fill("#nu-pass", "smoke-pass")
        page.click('[data-act="add-user"]')
        page.wait_for_selector(".pt-urow:has-text('smoke-user')", timeout=10_000)
        page.click(".pt-urow:has-text('smoke-user') [data-reset]")
        page.wait_for_selector(".wapyt-msg-dialog input", timeout=5_000)
        page.fill(".wapyt-msg-dialog input", "another-pass")
        page.click(".wapyt-msg-dialog .wapyt-msg-btn:has-text('OK')")
        page.wait_for_timeout(800)
        page.click(".pt-urow:has-text('smoke-user') [data-udel]")
        _confirm(page, "Delete")
        page.wait_for_function("!document.querySelector('#set-users').innerText.includes('smoke-user')", timeout=10_000)

    # the guide keeps its {{char}} macros (the classic page lost them to Jinja)
    page.evaluate("location.hash = '#/help'")
    page.wait_for_function("document.querySelector('#help-body').textContent.includes('{{char}}')", timeout=15_000)
    page.screenshot(path=str(SHOTS / "help.png"))

    # chat memory, in a chat with the seeded example assistant
    page.evaluate("location.hash = '#/chats'")
    page.wait_for_selector(".pt-crow:has-text('Mira')", timeout=20_000)
    page.locator(".pt-crow:has-text('Mira')").first.click()
    page.wait_for_selector("#ch-msgs .pt-msg", timeout=20_000)
    page.click('[data-menu="head"]')
    page.click(".wapyt-cmenu-item:has-text('Memory')")
    page.wait_for_selector('[data-add="pinned"]', timeout=10_000)
    page.click('[data-add="pinned"]')
    page.fill('[data-row="pinned"] >> nth=-1 >> input', "Smoke pinned fact")
    page.click('[data-tab="long"]')
    page.click('[data-add="fact"]')
    page.fill('[data-row="fact"] >> nth=-1 >> input', "Smoke long-term fact")
    page.screenshot(path=str(SHOTS / "memory.png"))
    page.click("[data-save]")
    page.wait_for_timeout(800)
    page.click('[data-menu="head"]')
    page.click(".wapyt-cmenu-item:has-text('Memory')")
    page.wait_for_selector('[data-row="pinned"]', timeout=10_000)
    vals = page.eval_on_selector_all('[data-row] input', "els => els.map(e => e.value)")
    assert "Smoke pinned fact" in vals and "Smoke long-term fact" in vals, vals
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.click('[data-menu="head"]')
    assert page.locator(".wapyt-cmenu-item:has-text('Relationship')").count() == 0
    page.keyboard.press("Escape")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        # A fake microphone, granted without a prompt, and audio allowed to start
        # without a gesture: hands-free and push-to-talk really open a device.
        args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required"]
        if os.environ.get("PANTHEON_FAKE_AUDIO"):
            args.append(f"--use-file-for-fake-audio-capture={os.environ['PANTHEON_FAKE_AUDIO']}%noloop")
        browser = p.chromium.launch(executable_path=CHROME or None, args=args)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror {e}"))

        try:
            run_steps(browser, page, errors)
        except Exception:
            import traceback

            traceback.print_exc()
            page.screenshot(path=str(SHOTS / "failure.png"))
            for e in errors:
                print("CONSOLE", e[:2000])
            print(f"FAILED; screenshot {SHOTS / 'failure.png'}")
            return 1
        browser.close()

    real = [e for e in errors if "favicon" not in e]
    for e in real:
        print("ERROR", e)
    print(f"{len(real)} console error(s); screenshots in {SHOTS}")
    return 1 if real else 0


def run_steps(browser, page, errors: list) -> None:
    if True:
        page.goto(f"{URL}/pantheon/")
        page.wait_for_selector("input[name=email]", timeout=30_000)
        page.screenshot(path=str(SHOTS / "login.png"))
        page.fill("input[name=email]", USER)
        page.fill("input[name=password]", PASS)
        page.keyboard.press("Enter")
        page.wait_for_selector(".pt-citem, .pt-empty-side", timeout=120_000)
        page.wait_for_timeout(1500)
        original = page.evaluate("document.documentElement.getAttribute('data-ptheme')")

        for route in ROUTES:
            page.evaluate(f"location.hash = '#/{route}'")
            page.wait_for_timeout(700)
            assert page.locator(f".pt-tile.on[data-tile={route}]").count() == 1, route
            page.screenshot(path=str(SHOTS / f"{route}.png"))

        # --- Phase 2: Discover, profile, start chat ------------------------------
        page.evaluate("location.hash = '#/discover'")
        page.wait_for_selector(".pt-card", timeout=15_000)
        page.screenshot(path=str(SHOTS / "discover-grid.png"), full_page=True)
        cards = page.locator(".pt-card").count()
        page.fill("#pt-q", "zzzz-no-such-character")
        page.wait_for_timeout(300)
        assert page.locator(".pt-card").count() == 0, "search should filter the grid"
        page.fill("#pt-q", "")
        page.dispatch_event("#pt-q", "input")
        page.wait_for_timeout(300)
        assert page.locator(".pt-card").count() == cards
        page.locator(".pt-card").first.click()
        page.wait_for_selector(".pt-prof-head h2", timeout=15_000)
        assert "#/character/" in page.evaluate("location.hash")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOTS / "profile.png"))
        if page.locator("[data-greet='1']").count():
            before = page.inner_text(".pt-greet")
            page.click("[data-greet='1']")
            assert page.inner_text(".pt-greet") != before, "greeting picker should switch text"
        assert page.locator("[data-act=scenario]").count() == 0, "no scenario wizard here"
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        assert page.locator(".pt-prof").count() == 0, "Esc should close the profile"
        assert page.evaluate("location.hash") == "#/discover"
        # deep link survives a reload
        first_id = page.locator(".pt-card").first.get_attribute("data-open")
        page.goto(f"{URL}/pantheon/#/character/{first_id}")
        page.wait_for_selector(".pt-prof-head h2", timeout=120_000)
        before_chats = page.locator("[data-chat]").count()
        page.click("[data-act=start]")
        page.wait_for_function("location.hash.startsWith('#/chat/')", timeout=15_000)
        page.wait_for_timeout(800)
        assert page.locator(".pt-prof").count() == 0
        assert page.locator("[data-chat]").count() == min(before_chats + 1, 30), "new chat in the rail"
        page.screenshot(path=str(SHOTS / "started-chat.png"))

        if not os.environ.get("PANTHEON_SKIP_CHAT"):
            chat_steps(page)
        smoke_card = studio_steps(page)
        phase5_steps(page)
        phase6_steps(page, smoke_card)
        switchover_steps(browser, page, smoke_card, errors)

        page.click("[data-theme=ember]")
        page.wait_for_timeout(800)
        assert page.evaluate("document.documentElement.getAttribute('data-ptheme')") == "ember"
        page.screenshot(path=str(SHOTS / "theme-ember.png"))
        page.click(f"[data-theme={original}]")
        page.wait_for_timeout(500)

        page.click("#pt-persona")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "persona-menu.png"))
        page.keyboard.press("Escape")

        mobile = browser.new_page(viewport={"width": 400, "height": 860},
                                  storage_state=page.context.storage_state())
        mobile.on("console", lambda m: errors.append(f"[mobile] {m.text}") if m.type == "error" else None)
        mobile.goto(f"{URL}/pantheon/#/discover")
        mobile.wait_for_selector(".pt-citem, .pt-empty-side", timeout=120_000)
        mobile.wait_for_timeout(1000)
        mobile.screenshot(path=str(SHOTS / "mobile.png"))
        mobile.goto(f"{URL}/pantheon/#/character/{first_id}")
        mobile.wait_for_selector(".pt-prof-head h2", timeout=120_000)
        mobile.wait_for_timeout(600)
        mobile.screenshot(path=str(SHOTS / "mobile-profile.png"))
        mobile.goto(f"{URL}/pantheon/#/discover")
        mobile.wait_for_selector(".pt-card", timeout=60_000)
        mobile.click("#pt-burger")
        mobile.wait_for_timeout(500)
        mobile.screenshot(path=str(SHOTS / "mobile-drawer.png"))


if __name__ == "__main__":
    raise SystemExit(main())
