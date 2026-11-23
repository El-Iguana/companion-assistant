// ===========================================================================
// desktop.js — the desktop avatar's page (/desktop), on top of voice.js.
//
// She sits in the corner of the screen in a transparent WebKitGTK window
// (desktop/pantheon-desktop.py). There is nothing to type into: you talk to her
// (hands-free or the HUD), she answers out loud, and her latest line shows
// briefly as a caption. Everything voice — the TTS queue, lip-sync, the mic,
// VAD, wake word, barge-in, the 3D avatar — is voice.js, the same island the
// main page uses, so the two cannot drift apart. This file only does what the
// chat view does there: pick a chat, stream replies into the island, and keep
// the HUD and the tray hooks in step.
//
// Routes: pantheon_web/desktop.py (/pdesk/*). The tray menu reaches this page
// through the window.__desktop* hooks at the bottom; it never calls the API.
// ===========================================================================
(() => {
  const $ = (s) => document.querySelector(s);
  const V = window.PantheonVoice;

  function csrf() {
    const parts = document.cookie.split(";").map(s => s.trim());
    const hit = parts.find(p => /csrf/i.test(p.split("=")[0]) && /pantheon/i.test(p.split("=")[0]))
             || parts.find(p => /csrf/i.test(p.split("=")[0]));
    return hit ? decodeURIComponent(hit.slice(hit.indexOf("=") + 1)) : "";
  }

  async function api(path, body) {
    const opts = { credentials: "same-origin" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf() };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    if (r.status === 401) { location.reload(); throw new Error("signed out"); }
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  }

  let characters = [], recent = [], settings = {};
  let card = null, sessionId = "", mood = {};
  let streaming = false, controller = null, live = "";

  // A chat ended (another bartender opened, the sleep phrase, the window closing): save
  // it to long-term memory now. The server also sweeps a chat that has gone quiet, so a
  // save missed here is late, not lost. keepalive lets the request outlive the page.
  function endChat(sid) {
    if (!sid) return;
    fetch("/pdesk/sweep", {
      method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify({ session_id: sid }),
    }).catch(() => {});
  }

  // ---- caption + HUD ----------------------------------------------------------
  // Her latest line, briefly. A face alone gives you no way to catch a missed word,
  // but a permanent transcript is clutter on a desktop.
  const cap = $("#dt-caption");
  let capTimer = null;
  function caption(text, holdMs) {
    clearTimeout(capTimer);
    if (!text) { cap.classList.remove("show"); return; }
    // Older replies can contain sound tags (<laugh> …); never show them.
    text = text.replace(/[ \t]*<(?:laugh|chuckle|sigh|gasp|cough|sniffle|groan|yawn)>/g, "");
    cap.textContent = text.length > 320 ? "…" + text.slice(-320) : text;
    cap.classList.add("show");
    if (holdMs) capTimer = setTimeout(() => cap.classList.remove("show"), holdMs);
  }
  const visible = (t) => String(t || "").replace(/\[expr:[^\]]*\]/gi, "").trim();

  function paint() {
    const st = V.state();
    $("#dt-tts").textContent = st.speaker ? "🔊" : "🔇";
    $("#dt-tts").classList.toggle("active", st.speaker);
    $("#dt-mic").classList.toggle("active", st.hfActive);
    $("#dt-mic").classList.toggle("awake", st.awake);
    $("#dt-state").textContent = streaming ? "thinking…"
      : (st.listening || st.recording) ? "listening…"
      : st.speaking ? "speaking…"
      : st.hfActive ? (st.awake ? "in conversation" : "ready") : "idle";
  }

  // ---- without a 3D avatar: the portrait --------------------------------------
  // The same corner as the 3D canvas; hidden under a loaded avatar. (Expression sprites
  // were removed on 2026-11-23.)
  function portrait() {
    const img = $("#ch-portrait");
    if (!card || !card.has_portrait || document.body.classList.contains("pt-avatar-on")) {
      img.hidden = true; return;
    }
    img.src = `/pmedia/card/${encodeURIComponent(card.id)}.png?v=${encodeURIComponent(card.updated_at || "")}`;
    img.hidden = false;
  }

  // ---- opening a chat -------------------------------------------------------
  async function openCharacter(id) {
    const r = await api("/pdesk/open", { character_id: id });
    if (controller) controller.abort();
    V.stop();
    if (sessionId && sessionId !== r.session_id) endChat(sessionId);
    card = r.card; sessionId = r.session_id; mood = r.mood || {};
    V.attach(card, settings);
    V.mood(mood.mood || "", mood.valence || 0, "");
    portrait();
    paint();
    return true;
  }

  // ?card= override, then the configured desktop character, then the most recent chat.
  async function pickCharacter() {
    const want = new URLSearchParams(location.search).get("card") || settings.desktop_card_id || "";
    if (want && characters.some(c => c.id === want)) return openCharacter(want);
    if (want) console.warn("[desktop] configured character not found; falling back");
    const last = recent.find(s => characters.some(c => c.id === s.character_id));
    if (last) return openCharacter(last.character_id);
    if (characters.length) return openCharacter(characters[0].id);
    caption("No characters yet — make one in the full window.", 0);
    return false;
  }

  async function refreshState() {
    const s = await api("/pdesk/state");
    characters = s.characters || []; recent = s.recent || [];
    settings = Object.assign(settings, s.settings || {});
    return s;
  }

  // ---- a turn -------------------------------------------------------------------
  async function send(text) {
    if (streaming || !sessionId) return;
    streaming = true; V.setStreaming(true); paint();
    V.beginReply();
    controller = new AbortController();
    let acc = "", done = null;
    caption("…", 0);
    try {
      const r = await fetch("/pdesk/chat", {
        method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ session_id: sessionId, text }),
      });
      if (!r.ok) throw new Error("chat: " + r.status);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done: end } = await reader.read();
        if (end) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          for (const line of block.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            const ev = JSON.parse(line.slice(6));
            if (ev.token !== undefined) {
              acc += ev.token;
              V.feed(acc, false);
              caption(visible(acc), 0);
            } else if (ev.expr) {
              V.expr(ev.expr);
            } else if (ev.error) {
              caption(`${card.name} could not answer: ${ev.error}`, 8000);
            } else if (ev.done) {
              done = ev;
            }
          }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") { console.warn("[desktop] chat failed:", e); caption("Could not reach her — is Cantinero running?", 8000); }
    } finally {
      streaming = false; V.setStreaming(false); controller = null;
    }
    if (done) {
      V.feed(acc, true);
      V.flushExpr();
      mood = { mood: done.mood, valence: done.valence };
      V.mood(done.mood || "", done.valence || 0, "");
      portrait();
      caption(visible(acc), 9000);
    } else if (!acc) {
      caption("", 0);
    }
    paint();
  }

  // ---- the voice island's events -------------------------------------------------
  window.addEventListener("pvoice:send", (e) => send(String(e.detail.text || "")));
  window.addEventListener("pvoice:abort", () => { if (controller) controller.abort(); });
  window.addEventListener("pvoice:asleep", () => endChat(sessionId));
  window.addEventListener("pagehide", () => endChat(sessionId));
  window.addEventListener("pvoice:state", paint);
  window.addEventListener("pvoice:persist", (e) => {
    Object.assign(settings, e.detail.patch || {});
    api("/pdesk/settings", e.detail.patch || {}).catch((err) => console.warn("[desktop] save failed:", err));
  });
  // There is no toast surface here; the caption is the only place words can go.
  window.addEventListener("pvoice:toast", (e) => { console.log("[desktop]", e.detail.msg); caption(e.detail.msg, 7000); });
  window.addEventListener("avatar:active", () => portrait());

  $("#dt-tts").addEventListener("click", () => V.speaker());
  $("#dt-mic").addEventListener("click", () => V.handsFree());

  // ---- hooks for the native shell's tray menu -----------------------------------
  // The page holds the session, so the shell asks it rather than the API. Re-read in
  // the background so a character added in the main window turns up without a restart.
  window.__desktopCharacters = () => {
    refreshState().catch(() => {});
    return characters.map(c => ({ id: c.id, name: c.name, avatar: c.avatar,
                                  current: !!(card && card.id === c.id) }));
  };
  // Listening is a saved setting, never switched on just because the window opened.
  window.__desktopListening = () => {
    const st = V.state();
    return { on: !!st.handsFree,
             wake: ((card && card.wake_word) || settings.wake_word || "").trim(),
             sleep: ((card && card.sleep_phrase) || settings.sleep_phrase || "").trim(),
             mode: settings.wake_mode || "phrase", awake: !!st.awake, duplex: !!settings.full_duplex };
  };
  window.__desktopSetListening = async (on) => { await V.handsFree(!!on); return !!V.state().handsFree; };
  window.__desktopSetCharacter = async (id) => {
    if (!characters.some(c => c.id === id)) return false;
    // Saved first, so a restart comes back as whoever was last chosen.
    settings.desktop_card_id = id;
    api("/pdesk/settings", { desktop_card_id: id }).catch(() => {});
    return openCharacter(id);
  };

  // ---- boot ---------------------------------------------------------------------
  (async () => {
    // Voice on by default here: a face in the corner that types silently is just a worse
    // chat window. The shell allows autoplay, so audio can start without a click.
    V.speaker(true);
    try {
      await refreshState();
      await pickCharacter();
    } catch (e) {
      console.warn("[desktop] boot failed:", e);
      caption("Could not load — is Cantinero running?", 0);
    }
    setInterval(paint, 500);
    window.__desktopReady = true;
  })();
})();
