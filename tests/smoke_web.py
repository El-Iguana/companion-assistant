"""Smoke test for the pytincture page: every screen, zero console errors, screenshots.

    PANTHEON_URL=http://127.0.0.1:7866 PANTHEON_USER=… PANTHEON_PASS=… \\
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

URL = os.environ.get("PANTHEON_URL", "http://127.0.0.1:7866").rstrip("/")
USER = os.environ["PANTHEON_USER"]
PASS = os.environ["PANTHEON_PASS"]
CHROME = os.path.expanduser(os.environ.get("CHROME", ""))
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/shots")
ROUTES = ["discover", "create", "chats", "personas", "presets", "settings"]


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


def _confirm(page, label):
    page.wait_for_selector(".wapyt-msg-dialog", timeout=5_000)
    page.click(f".wapyt-msg-dialog .wapyt-msg-btn:has-text('{label}')")


def phase5_steps(page) -> None:
    """Personas, presets, settings, password, users, guide, chat memory + context."""
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

    # presets: create with a slider, then delete
    page.evaluate("location.hash = '#/presets'")
    page.wait_for_selector("[data-new]", timeout=20_000)
    page.click("[data-new]")
    page.fill("#pr-name", "Smoke Preset")
    page.fill("#pr-note", "The test begins.")
    page.eval_on_selector('[data-key="trust"]', "el => { el.value = 40; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.screenshot(path=str(SHOTS / "preset-editor.png"))
    page.click("[data-save]")
    page.wait_for_selector(".pt-crow:has-text('Smoke Preset')", timeout=10_000)
    assert "trust 40" in page.locator(".pt-crow:has-text('Smoke Preset')").last.inner_text()
    n = page.locator(".pt-crow:has-text('Smoke Preset')").count()
    page.locator(".pt-crow:has-text('Smoke Preset')").first.click()
    page.click(".pt-modal [data-delete]")
    _confirm(page, "Delete")
    page.wait_for_function(f"[...document.querySelectorAll('.pt-crow')].filter(c => c.innerText.includes('Smoke Preset')).length === {n - 1}",
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

    # chat memory + relationship context, in the first roleplay chat in the list
    page.evaluate("location.hash = '#/chats'")
    page.wait_for_selector(".pt-crow:not(:has(.pt-tag))", timeout=20_000)
    page.locator(".pt-crow:has-text('Iris')").first.click()
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
    page.click('[data-menu="head"]')
    page.click(".wapyt-cmenu-item:has-text('Relationship context')")
    page.wait_for_selector("#cx-power", timeout=10_000)
    page.eval_on_selector("#cx-power", "el => { el.value = 30; el.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.screenshot(path=str(SHOTS / "context.png"))
    page.click("[data-save]")
    page.wait_for_timeout(1000)
    page.click('[data-menu="head"]')
    page.click(".wapyt-cmenu-item:has-text('Relationship context')")
    page.wait_for_selector("#cx-power", timeout=10_000)
    assert page.input_value("#cx-power") == "30"
    page.click("[data-clear]")
    page.wait_for_timeout(800)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME or None)
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

        # --- Phase 2: Discover, profile, scenario, start chat -------------------
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
        if page.locator("[data-act=scenario]").count():
            page.click("[data-act=scenario]")
            page.wait_for_selector("#sc-preset", timeout=5_000)
            page.wait_for_timeout(500)                      # let the fade-in finish
            page.screenshot(path=str(SHOTS / "scenario.png"))
            page.click(".pt-narrow [data-close]")
            page.wait_for_timeout(300)
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
        studio_steps(page)
        phase5_steps(page)

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
