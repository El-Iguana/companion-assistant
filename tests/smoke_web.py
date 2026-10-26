"""Smoke test for the pytincture page: every screen, zero console errors, screenshots.

    PANTHEON_URL=http://127.0.0.1:7866 PANTHEON_USER=… PANTHEON_PASS=… \\
    CHROME=~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome \\
      uv run --with playwright python tests/smoke_web.py [shots_dir]

Changes nothing except the account's theme (it switches to Ember and back to what it was).
Run it against a scratch library (PANTHEON_DATA_DIR) — it signs in as a real account.
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


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME or None)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror {e}"))

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

        chat = page.locator("[data-chat]").first
        if chat.count():
            chat.click()
            page.wait_for_timeout(700)
            page.screenshot(path=str(SHOTS / "chat.png"))

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
        mobile.click("#pt-burger")
        mobile.wait_for_timeout(500)
        mobile.screenshot(path=str(SHOTS / "mobile-drawer.png"))
        browser.close()

    real = [e for e in errors if "favicon" not in e]
    for e in real:
        print("ERROR", e)
    print(f"{len(real)} console error(s); screenshots in {SHOTS}")
    return 1 if real else 0


if __name__ == "__main__":
    raise SystemExit(main())
