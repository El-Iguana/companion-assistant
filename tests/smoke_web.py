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
