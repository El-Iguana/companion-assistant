"""``/ca`` and ``/ca.crt``: set a device up for HTTPS (#7). Public and plain HTTP on purpose:
a device that doesn't trust Cantinero's CA yet can't open the HTTPS address to fetch it.
The certificate is public by nature; the CA's key never leaves ``data/tls/``."""
from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter()


def _state():
    """(the CA certificate or b"", HTTPS port or 0, HTTP port). Read from the environment,
    as service.py reads them: importing service from here would build a second app (it
    runs as __main__)."""
    import os

    import main
    from pantheon_web import tls

    try:
        pem = tls.ca_pem(main.DATA)
    except OSError:
        pem = b""
    return (pem, int(os.environ.get("PANTHEON_HTTPS_PORT", "0") or 0),
            int(os.environ.get("PANTHEON_PORT", "7867")))


@router.get("/ca.crt", include_in_schema=False)
def ca_cert() -> Response:
    pem, _, _ = _state()
    if not pem:
        return Response("HTTPS is off on this Cantinero.", status_code=404, media_type="text/plain")
    return Response(pem, media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="cantinero-ca.crt"'})


_STEPS = [
    ("Android", "Download the certificate. Then Settings → Security &amp; privacy → More security "
                "settings → Encryption &amp; credentials → <b>Install a certificate → CA certificate</b>, "
                "and pick <i>cantinero-ca.crt</i>. (The menu names vary a little by phone; search "
                "Settings for “CA certificate”.)"),
    ("iPhone / iPad", "Open this page in <b>Safari</b> and download the certificate; allow the profile. "
                      "Then Settings → General → <b>VPN &amp; Device Management</b> → Cantinero local CA → "
                      "Install. Last, Settings → General → About → <b>Certificate Trust Settings</b> → "
                      "turn on Cantinero local CA."),
    ("Windows", "Open the downloaded file → <b>Install Certificate</b> → Current User → “Place all "
                "certificates in the following store” → <b>Trusted Root Certification Authorities</b> "
                "→ Finish. Restart the browser. (Firefox keeps its own list: Settings → Privacy &amp; "
                "Security → Certificates → View Certificates → Authorities → Import.)"),
    ("Mac", "Open the downloaded file; Keychain Access adds it. Double-click <b>Cantinero local CA</b> "
            "→ Trust → “When using this certificate”: <b>Always Trust</b>."),
    ("Linux", "Chrome: Settings → Privacy and security → Security → Manage certificates → "
              "<b>Authorities → Import</b>, and tick “Trust this certificate for identifying "
              "websites”. Firefox: Settings → Privacy &amp; Security → Certificates → View "
              "Certificates → Authorities → Import."),
]


@router.get("/ca", include_in_schema=False)
def ca_page(request: Request) -> HTMLResponse:
    from pantheon_web import tls

    pem, https_port, http_port = _state()
    host = (request.url.hostname or "this-computer").lower()
    if not pem or not https_port:
        body = ("<p>HTTPS is off on this Cantinero, so the microphone works only on the computer "
                f"running it, at <a href='http://127.0.0.1:{http_port}/'>http://127.0.0.1:{http_port}/</a>.</p>")
    else:
        https = f"https://{html.escape(host)}:{https_port}/"
        steps = "".join(f"<details><summary>{name}</summary><p>{text}</p></details>" for name, text in _STEPS)
        body = (
            "<p>Browsers allow the microphone only over HTTPS. Cantinero has its own small "
            "certificate authority for that: trust it once on this device, then use the HTTPS "
            "address. It can only vouch for local names (<i>.local</i>, this computer's name) and "
            "home-network addresses, never for real websites.</p>"
            "<h2>1. Download the certificate</h2>"
            "<p><a class='btn' href='/ca.crt'>Download cantinero-ca.crt</a></p>"
            "<p class='dim'>If your device shows a fingerprint (SHA-256), it should be:<br>"
            f"<code>{tls.fingerprint(pem)}</code></p>"
            f"<h2>2. Trust it on this device</h2>{steps}"
            "<h2>3. Open Cantinero over HTTPS</h2>"
            f"<p><a class='btn' href='{https}'>{https}</a></p>"
            "<p class='dim'>Sign in again there. On the computer running Cantinero none of this is "
            f"needed: <a href='http://127.0.0.1:{http_port}/'>http://127.0.0.1:{http_port}/</a> "
            "already has the microphone.</p>")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cantinero · voice on this device</title>
<link rel="icon" href="/static/brand/favicon.png">
<style>
:root {{ color-scheme: dark light; --bg:#14110f; --fg:#f2ece4; --mut:#b3a99c; --acc:#e0a458; --line:#3a322b; }}
@media (prefers-color-scheme: light) {{ :root {{ --bg:#faf7f2; --fg:#221d18; --mut:#6b6157; --line:#e2d9cc; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui, sans-serif; }}
main {{ max-width:640px; margin:0 auto; padding:28px 16px 48px; }}
h1 {{ font-size:24px; margin:8px 0 4px; }} h2 {{ font-size:17px; margin:26px 0 8px; }}
.logo {{ width:56px; height:56px; border-radius:14px; }}
.btn {{ display:inline-block; padding:10px 16px; border-radius:12px; background:var(--acc); color:#1b140d;
        font-weight:700; text-decoration:none; word-break:break-all; }}
details {{ border:1px solid var(--line); border-radius:12px; padding:8px 14px; margin:8px 0; }}
summary {{ cursor:pointer; font-weight:600; }}
code {{ font-size:12.5px; word-break:break-all; }} .dim {{ color:var(--mut); font-size:14px; }}
a {{ color:var(--acc); }}
</style></head><body><main>
<img class="logo" src="/static/brand/cantinero-logo.png" alt="">
<h1>Talk to Cantinero from this device</h1>{body}
</main></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
