// Pantheon — vanilla JS frontend. No framework, no build step.
const $ = (s) => document.querySelector(s);
const api = (p, o) => fetch("/api" + p, o).then(r => {
  if (r.status === 401) { showLogin(); return Promise.reject({ detail: "not authenticated" }); }
  return r.ok ? r.json() : r.json().then(e => Promise.reject(e));
});

const state = { cards: [], sessions: [], personas: [], presets: [], persona: null, session: null, streaming: false };

// Bipolar power-dynamic readout: name the side that holds power (− = user, + = char).
const powerLabel = (v) => { v = Number(v); return v === 0 ? "Balanced" : (v < 0 ? "User " : "Char ") + Math.abs(v); };

// ---- rendering helpers ----------------------------------------------------
function md(t) {  // minimal: *italics* for actions/emotes
  const esc = t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(/\*([^*]+)\*/g, "<em>$1</em>");
}
function avatar(card) {
  return card && card.image
    ? `<img class="avatar" src="data:image/png;base64,${card.image}">`
    : `<span class="avatar"></span>`;
}
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

function makeMsgEl(role, content, whoName) {
  const el = document.createElement("div");
  el.className = "msg " + (role === "char" ? "char" : "user");
  el.innerHTML = `<div class="col"><div class="who">${whoName || ""}</div>` +
                 `<div class="bubble">${md(content)}</div></div>`;
  return el;
}

// Append a bare bubble (used for the live streaming placeholder). Returns the bubble.
function addMsg(role, content, whoName) {
  const el = makeMsgEl(role, content, whoName);
  $("#messages").appendChild(el);
  scrollDown();
  return el.querySelector(".bubble");
}

// Swipe navigation footer for the last char message.
function swipeFooter(m) {
  const f = document.createElement("div");
  f.className = "swipes";
  const total = m.swipes.length || 1;
  f.innerHTML =
    `<button class="swipe-btn" data-d="-1" ${m.swipe_index <= 0 ? "disabled" : ""}>◀</button>` +
    `<span class="swipe-count">${m.swipe_index + 1} / ${total}</span>` +
    `<button class="swipe-btn" data-d="1" title="Next alternate (generates a new one at the end)">▶</button>`;
  f.querySelector('[data-d="-1"]').onclick = () => gotoSwipe(-1);
  f.querySelector('[data-d="1"]').onclick = () => gotoSwipe(1);
  return f;
}

function renderMessages() {
  const card = currentCard();
  const box = $("#messages"); box.innerHTML = "";
  const msgs = state.session.messages;
  const lastCharIdx = msgs.map(m => m.role).lastIndexOf("char");
  msgs.forEach((m, i) => {
    const content = (m.role === "char" && m.swipes.length) ? m.swipes[m.swipe_index] : m.content;
    const who = m.role === "char" ? (card ? card.name : "") : (state.persona?.name || "You");
    const el = makeMsgEl(m.role, content, who);
    if (m.role === "char" && m.expression) {
      const tag = document.createElement("div");
      tag.className = "emo-tag";
      tag.textContent = "🎭 " + m.expression;
      el.querySelector(".col").appendChild(tag);
    }
    if (m.role === "char" && i === lastCharIdx && msgs.length > 1) {
      el.querySelector(".col").appendChild(swipeFooter(m));
    }
    box.appendChild(el);
  });
  updatePortrait();
  scrollDown();
}

// ---- avatar bridge (Phase 3a) ---------------------------------------------
// static/js/vtuber.mjs is an ESM module and this file is a classic script, so the
// two never import each other — they exchange CustomEvents on window. When the
// avatar takes over, the sprite portrait hides; if it fails or the card is on the
// sprite tier, it stays exactly as it always was.
let avatarActive = false;
let avatarCardId = null, avatarCardKey = null;

window.addEventListener("avatar:active", (e) => {
  avatarActive = !!(e.detail && e.detail.active);
  const cv = $("#avatar-canvas"); if (cv) cv.hidden = !avatarActive;
  updatePortrait();
});

// Tell the renderer which card is open. Cheap and idempotent, so it is safe to
// call from every path that can change the current card.
function syncAvatarCard() {
  const card = currentCard();
  const id = card ? card.id : null;
  // Key on the MODEL, not just the character. Tracking the card id alone meant
  // assigning a different .vrm to the same character never reloaded anything --
  // the id had not changed, so the renderer was never told. The mode is in the
  // key too, since switching a card between sprites and 3D changes what shows
  // without touching either the id or the filename.
  const key = [id, (card && card.vrm) || "", (card && card.avatar_mode) || ""].join("|");
  if (key === avatarCardKey) return;
  avatarCardKey = key;
  avatarCardId = id;
  window.dispatchEvent(new CustomEvent("avatar:card", {
    detail: { id, hasVrm: !!(card && card.has_vrm), rev: (card && card.vrm) || "" },
  }));
}

// Swap the floating portrait to the current character's active emotion sprite.
function updatePortrait() {
  const card = currentCard();
  const img = $("#portrait");
  syncAvatarCard();
  // Avatar tier owns the corner — the sprite would draw on top of the canvas.
  if (avatarActive) { img.hidden = true; updateMood(); return; }
  if (!card || !card.has_expressions) { img.hidden = true; updateMood(); return; }
  const msgs = state.session.messages;
  const lastChar = [...msgs].reverse().find(m => m.role === "char");
  // Portrait reflects the running MOOD (persists through neutral); falls back to the last reply's emotion.
  const emotion = (state.session.mood && state.session.mood.mood)
                  || (lastChar && lastChar.expression) || "neutral";
  img.src = `/api/cards/${card.id}/expression/${emotion}?t=${Date.now()}`;
  img.hidden = false;
  updateMood();
}

// Relationship strip: stage + 4 axis bars (persists across chats).
const REL_AXES = [["affection", "Affection", "#e0557a"], ["trust", "Trust", "#5aa9e0"],
                  ["attraction", "Attraction", "#c86ce0"], ["tension", "Tension", "#e0a24a"]];

function renderRelationship(rel) {
  if (rel && rel.stage)
    window.dispatchEvent(new CustomEvent("avatar:mood", { detail: {
      mood: (state.session && state.session.mood && state.session.mood.mood) || "",
      valence: (state.session && state.session.mood && state.session.mood.valence) || 0,
      stage: rel.stage,
    }}));
  const strip = $("#rel-strip");
  if (!rel || !currentCard()) { strip.hidden = true; return; }
  strip.hidden = false;
  $("#rel-stage").textContent = rel.stage || "Stranger";
  const box = $("#rel-axes"); box.innerHTML = "";
  for (const [key, label, color] of REL_AXES) {
    const v = Math.max(0, Math.min(100, rel[key] || 0));
    const el = document.createElement("span");
    el.className = "rel-axis";
    el.innerHTML = `<label>${label}</label><span class="rel-bar">` +
      `<i style="width:${v}%;background:${color}"></i></span><b>${Math.round(v)}</b>`;
    box.appendChild(el);
  }
  $("#rel-intimacy").textContent =
    `Emotional: ${rel.emotional_level || "guarded"} · Comfort: ${rel.physical_tier_label || "none"}`;
}

function showToast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  $("#toasts").appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 400); }, 6000);
}

async function loadRelationship() {
  const card = currentCard();
  if (!card) { $("#rel-strip").hidden = true; return; }
  if (state.session && state.session.relationship_summary) {   // scenario session -> its own snapshot
    renderRelationship(state.session.relationship_summary); return;
  }
  if (!state.persona) { $("#rel-strip").hidden = true; return; }
  try {
    renderRelationship(await api(`/relationship?character_id=${card.id}&persona_id=${state.persona.id}`));
  } catch { $("#rel-strip").hidden = true; }
}

// Per-relationship context override (power / social).
async function openRelCtx() {
  const card = currentCard(); if (!card || !state.persona) return;
  const r = await api(`/relationship?character_id=${card.id}&persona_id=${state.persona.id}`);
  $("#rc-power").value = r.power_dynamic ?? 0;
  $("#rc-social").value = r.social_acceptance ?? 50;
  $("#rc-power-val").textContent = powerLabel($("#rc-power").value);
  $("#rc-social-val").textContent = $("#rc-social").value;
  $("#rel-ctx").hidden = false;
}
async function saveRelCtx(clear) {
  const card = currentCard(); if (!card || !state.persona) return;
  const body = { character_id: card.id, persona_id: state.persona.id };
  if (clear) { body.power_dynamic = null; body.social_acceptance = null; }
  else { body.power_dynamic = Number($("#rc-power").value); body.social_acceptance = Number($("#rc-social").value); }
  renderRelationship(await api("/relationship", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
  $("#rel-ctx").hidden = true;
}
$("#rel-ctx-btn").addEventListener("click", openRelCtx);
$("#rel-ctx-close").addEventListener("click", () => { $("#rel-ctx").hidden = true; });
$("#rel-ctx").addEventListener("click", (e) => { if (e.target.id === "rel-ctx") $("#rel-ctx").hidden = true; });
$("#rc-save").addEventListener("click", () => saveRelCtx(false));
$("#rc-clear").addEventListener("click", () => saveRelCtx(true));
$("#rc-power").addEventListener("input", (e) => { $("#rc-power-val").textContent = powerLabel(e.target.value); });
$("#rc-social").addEventListener("input", (e) => { $("#rc-social-val").textContent = e.target.value; });

// Header mood meter: label + a valence bar filling from the center.
function updateMood() {
  const el = $("#mood-meter");
  const card = currentCard();
  const m = state.session && state.session.mood;
  if (!m || !card || !card.has_expressions) { el.hidden = true; return; }
  el.hidden = false;
  const v = Math.max(-1, Math.min(1, m.valence || 0));
  $("#mood-label").textContent = m.mood || "neutral";
  const fill = $("#mood-fill");
  if (v >= 0) { fill.style.left = "50%"; fill.style.width = (v * 50) + "%"; fill.style.background = "#5ad16b"; }
  else { fill.style.left = (50 + v * 50) + "%"; fill.style.width = (-v * 50) + "%"; fill.style.background = "#e06666"; }
}

// Browse alternates on the last char message; ▶ past the end generates a new one.
async function gotoSwipe(delta) {
  if (state.streaming || !state.session) return;
  const msgs = state.session.messages;
  const idx = msgs.map(m => m.role).lastIndexOf("char");
  if (idx < 0) return;
  const m = msgs[idx];
  const target = m.swipe_index + delta;
  if (target < 0) return;
  if (target >= m.swipes.length) { send("", true); return; }  // generate a new alternate
  m.swipe_index = target;
  m.content = m.swipes[target];
  renderMessages();                       // show the swipe text immediately
  try {
    const res = await api(`/sessions/${state.session.id}/swipe`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ swipe_index: target })
    });
    if (res && res.expression) { m.expression = res.expression; updatePortrait(); }
  } catch (e) { /* portrait just stays put */ }
}

// ---- data loading ---------------------------------------------------------
async function refreshStatus() {
  try {
    const s = await api("/status");
    const dot = $("#status");
    dot.className = "status " + (s.online ? "online" : "offline");
    dot.title = s.online ? `Online — ${s.model || "model"}` : (s.error || "KoboldCPP offline");
  } catch { $("#status").className = "status offline"; }
}

async function loadCards() {
  state.cards = await api("/cards");
  const ul = $("#card-list"); ul.innerHTML = "";
  for (const c of state.cards) {
    const li = document.createElement("li");
    li.innerHTML = `${avatar(c)}<span class="li-name">${c.name}</span>` +
                   `<button class="li-edit" title="Edit character">✎</button>`;
    li.onclick = () => openNewChat(c.id);
    li.querySelector(".li-edit").onclick = (e) => { e.stopPropagation(); openCardEditor(c); };
    ul.appendChild(li);
  }
}

async function loadSessions() {
  state.sessions = await api("/sessions");
  state.sessions.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const ul = $("#session-list"); ul.innerHTML = "";
  for (const s of state.sessions) {
    const li = document.createElement("li");
    if (state.session && s.id === state.session.id) li.classList.add("active");
    li.innerHTML = `<span class="li-name">${s.title}</span>` +
                   `<span class="meta">${s.messages}</span>` +
                   `<button class="li-del" title="Delete chat">✕</button>`;
    li.onclick = () => openSession(s.id);
    li.querySelector(".li-del").onclick = (e) => { e.stopPropagation(); deleteSession(s.id, s.title); };
    ul.appendChild(li);
  }
}

async function loadPersonas() {
  state.personas = await api("/personas");
  state.persona = state.personas.find(p => p.is_default) || state.personas[0] || null;
  const ul = $("#persona-list"); ul.innerHTML = "";
  for (const p of state.personas) {
    const li = document.createElement("li");
    if (state.persona && p.id === state.persona.id) li.classList.add("active");
    li.innerHTML = `${avatar(p)}<span class="li-name">${p.name}${p.title ? " · " + p.title : ""}</span>` +
                   `<button class="li-edit" title="Edit persona">✎</button>`;
    li.onclick = () => activatePersona(p.id);
    li.querySelector(".li-edit").onclick = (e) => { e.stopPropagation(); openPersonaEditor(p); };
    ul.appendChild(li);
  }
}

async function activatePersona(id) {
  const p = state.personas.find(x => x.id === id); if (!p) return;
  await api("/personas/" + id, { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...p, is_default: true }) });
  await loadPersonas();
  if (state.session) loadRelationship();   // relationship is keyed to the active persona
}

// ---- session flow ---------------------------------------------------------
function currentCard() { return state.cards.find(c => c.id === (state.session || {}).character_id); }

function renderSession(session) {
  state.session = session;
  state.persona = state.personas.find(p => p.id === session.persona_id) || state.persona;
  const card = state.cards.find(c => c.id === session.character_id);
  $("#chat-title").textContent = card ? card.name : session.title;
  // Expression controls
  $("#gallery-btn").hidden = !card;
  $("#tts-btn").hidden = !card;
  $("#hf-btn").hidden = !card;
  $("#voicecfg-btn").hidden = !card;
  $("#mem-btn").hidden = false;                  // memory is session-scoped, works with or without a card
  $("#voice-test").hidden = !(card && ttsEnabled);
  if (card) $("#expr-appearance").value = card.appearance || "";
  $("#expr-from-upload-row").hidden = !(card && card.image);   // only when a portrait exists
  $("#expr-from-upload").checked = !!(card && card.expr_from_upload);
  const dn = (card && card.expr_denoise) || 0.55;
  $("#expr-denoise").value = dn; $("#expr-denoise-val").textContent = Number(dn).toFixed(2);
  renderMessages();
  loadRelationship();
  $("#input").disabled = false; $("#send").disabled = false; $("#regen").disabled = false;
  $("#mic").disabled = false;
  $("#input").focus();
  loadSessions();
}

// New-chat wizard: pick persona + preset + (if preset) continue-vs-scenario.
let ncCardId = null;

function openNewChat(cardId) {
  ncCardId = cardId;
  const card = state.cards.find(c => c.id === cardId);
  $("#nc-title").textContent = "New chat" + (card ? " — " + card.name : "");
  $("#nc-persona").innerHTML = state.personas.map(p =>
    `<option value="${p.id}">${p.name}${p.title ? " · " + p.title : ""}</option>`).join("");
  if (state.persona) $("#nc-persona").value = state.persona.id;
  $("#nc-preset").innerHTML = `<option value="">— none —</option>` +
    state.presets.map(pr => `<option value="${pr.id}">${pr.name}</option>`).join("");
  const match = state.presets.find(pr => pr.character_id === cardId);   // suggest a matching preset
  $("#nc-preset").value = match ? match.id : "";
  ncPresetChanged();
  $("#newchat").hidden = false;
}

function ncPresetChanged() {
  const pr = state.presets.find(p => p.id === $("#nc-preset").value);
  $("#nc-mode").hidden = !pr;
  if (pr) {
    const scen = document.querySelector('input[name="ncmode"][value="scenario"]');
    if (scen) scen.checked = true;                 // picking a preset ⇒ apply its values by default
    if (pr.persona_id && state.personas.some(p => p.id === pr.persona_id))
      $("#nc-persona").value = pr.persona_id;      // preset suggests a persona
  }
}

async function createChatFromWizard() {
  const body = { character_id: ncCardId, persona_id: $("#nc-persona").value };
  const presetId = $("#nc-preset").value;
  if (presetId) {
    body.preset_id = presetId;
    body.mode = document.querySelector('input[name="ncmode"]:checked')?.value || "continue";
  }
  const created = await api("/sessions", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  $("#newchat").hidden = true;
  await openSession(created.id);      // GET includes relationship_summary for scenario sessions
}

$("#nc-close").addEventListener("click", () => { $("#newchat").hidden = true; });
$("#nc-cancel").addEventListener("click", () => { $("#newchat").hidden = true; });
$("#newchat").addEventListener("click", (e) => { if (e.target.id === "newchat") $("#newchat").hidden = true; });
$("#nc-preset").addEventListener("change", ncPresetChanged);
$("#nc-start").addEventListener("click", createChatFromWizard);

async function openSession(id) { renderSession(await api("/sessions/" + id)); }

async function deleteSession(id, title) {
  if (!confirm(`Delete chat "${title}"? This cannot be undone.`)) return;
  await api("/sessions/" + id, { method: "DELETE" });
  if (state.session && state.session.id === id) {
    state.session = null;
    $("#messages").innerHTML = "";
    $("#chat-title").textContent = "Select or import a character to begin";
    $("#input").disabled = true; $("#send").disabled = true; $("#regen").disabled = true;
    $("#mic").disabled = true;
    $("#portrait").hidden = true;
    $("#mem-btn").hidden = true;
  }
  await loadSessions();
}

// ---- streaming chat -------------------------------------------------------
let chatAbort = null;

// Barge-in: stop generation already in flight (client-side; the server-side
// generator is cancelled best-effort when the response stream is dropped).
function abortChat() {
  if (chatAbort) { try { chatAbort.abort(); } catch {} chatAbort = null; }
}

async function send(text, regenerate) {
  if (state.streaming || !state.session) return;
  state.streaming = true;
  stopSpeaking();                     // barge-in: cut any current speech
  chatAbort = new AbortController();
  const card = currentCard();
  if (!regenerate && text) addMsg("user", text, state.persona?.name || "You");
  const bubble = addMsg("char", "", card ? card.name : "");
  bubble.innerHTML = "<em>…</em>";

  let acc = "";
  try {
    const resp = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session.id, input_text: text, regenerate: !!regenerate }),
      signal: chatAbort.signal
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n"); buf = parts.pop();
      for (const p of parts) {
        const line = p.split("\n").find(l => l.startsWith("data:"));
        if (!line) continue;
        const evt = JSON.parse(line.slice(5).trim());
        if (evt.token) { acc += evt.token; bubble.innerHTML = md(acc); scrollDown(); feedSpeech(acc, false); }
        else if (evt.expr) { pendingExpr(evt.expr); }
        else if (evt.error) { bubble.innerHTML = `<em>⚠ ${evt.error}</em>`; }
        else if (evt.done) {
          if (evt.relationship) renderRelationship(evt.relationship);
          if (evt.milestones) evt.milestones.forEach(showToast);
          // Resting affect + posture for the avatar: the running mood biases the
          // face between beats, the relationship stage biases how she stands.
          window.dispatchEvent(new CustomEvent("avatar:mood", { detail: {
            mood: evt.mood, valence: evt.valence,
            stage: evt.relationship && evt.relationship.stage,
          }}));
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") { if (!acc) bubble.innerHTML = "<em>…</em>"; }  // barge-in: keep partial
    else bubble.innerHTML = `<em>⚠ ${e.message || e}</em>`;
  } finally {
    chatAbort = null;
    if (acc) bubble.innerHTML = md(acc);
    state.streaming = false;
    // Reload from server so swipe controls reflect the newly stored alternate.
    try {
      state.session = await api("/sessions/" + state.session.id);
      renderMessages();
    } catch { /* keep the live bubble if reload fails */ }
    loadSessions();
    feedSpeech(acc, true);            // flush any trailing sentence
    flushExpr();                      // a beat tagged after the last spoken line
    scheduleSweep();                  // arm the idle fold into long-term memory
  }
}

// ---- wiring ---------------------------------------------------------------
$("#import-input").addEventListener("change", async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const card = await api("/cards/import", { method: "POST", body: fd });
    await loadCards();
    openNewChat(card.id);
  } catch (err) { alert("Import failed: " + (err.detail || err)); }
  e.target.value = "";
});

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#input").value.trim(); if (!t) return;
  $("#input").value = ""; $("#input").style.height = "auto";
  send(t, false);
});
$("#regen").addEventListener("click", () => send("", true));

$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#input").addEventListener("input", (e) => {
  e.target.style.height = "auto"; e.target.style.height = e.target.scrollHeight + "px";
});

// ---- persona editor -------------------------------------------------------
let peEditingId = null, peBase = {}, peImage = undefined;

function openPersonaEditor(p) {
  peEditingId = p ? p.id : null;
  peImage = undefined;
  peBase = p || {};
  const v = (id, val) => { $("#pe-" + id).value = val || ""; };
  v("name", peBase.name); v("title-in", peBase.title);
  v("description", peBase.description); v("background", peBase.background);
  $("#pe-default").checked = p ? !!peBase.is_default : (state.personas.length === 0);
  const prev = $("#pe-preview");
  prev.src = peBase.image ? `data:image/png;base64,${peBase.image}` : "";
  prev.style.display = peBase.image ? "block" : "none";
  $("#pe-title").textContent = p ? `Edit — ${peBase.name}` : "New persona";
  $("#pe-delete").hidden = !p;
  $("#pe-status").textContent = "";
  $("#persona-editor").hidden = false;
}
const closePersonaEditor = () => { $("#persona-editor").hidden = true; };

async function savePersona() {
  const g = id => $("#pe-" + id).value.trim();
  const payload = { ...peBase, name: g("name") || "User", title: g("title-in"),
    description: g("description"), background: g("background"),
    is_default: $("#pe-default").checked,
    image: peImage !== undefined ? peImage : (peBase.image || null) };
  $("#pe-save").disabled = true; $("#pe-status").textContent = "Saving…";
  try {
    const opts = { method: peEditingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
    await api(peEditingId ? "/personas/" + peEditingId : "/personas", opts);
    await loadPersonas();
    closePersonaEditor();
  } catch (e) { $("#pe-status").textContent = "Failed: " + (e.detail || e.message || e); }
  finally { $("#pe-save").disabled = false; }
}

async function deletePersona() {
  if (!peEditingId || !confirm("Delete this persona?")) return;
  await api("/personas/" + peEditingId, { method: "DELETE" });
  await loadPersonas();
  closePersonaEditor();
}

$("#new-persona").addEventListener("click", () => openPersonaEditor(null));
$("#pe-close").addEventListener("click", closePersonaEditor);
$("#pe-cancel").addEventListener("click", closePersonaEditor);
$("#pe-save").addEventListener("click", savePersona);
$("#pe-delete").addEventListener("click", deletePersona);
$("#persona-editor").addEventListener("click", (e) => { if (e.target.id === "persona-editor") closePersonaEditor(); });
$("#pe-image").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => { peImage = String(r.result).split(",")[1];
    const p = $("#pe-preview"); p.src = r.result; p.style.display = "block"; };
  r.readAsDataURL(f);
});

// ---- expression sprites ---------------------------------------------------
let checkpointsLoaded = false;
async function loadCheckpoints() {
  if (checkpointsLoaded) return;
  const sel = $("#expr-checkpoint");
  try {
    const { checkpoints } = await api("/comfyui/checkpoints");
    sel.innerHTML = checkpoints.map(c => `<option value="${c}">${c}</option>`).join("");
    checkpointsLoaded = true;
  } catch (e) {
    sel.innerHTML = `<option value="">ComfyUI unreachable</option>`;
    $("#expr-status").textContent = "Start ComfyUI on :8188 to generate sprites.";
  }
}

// ---- expression preview (renders each sprite at true in-chat portrait sizing) ----
let previewEmotions = [], previewIdx = 0;

async function refreshExprPreview() {
  const card = exprCard();
  const img = $("#expr-preview-img"), empty = $("#expr-preview-empty"), label = $("#expr-preview-label");
  let present = [];
  if (card) { try { present = (await api(`/cards/${card.id}/expressions`)).emotions || []; } catch {} }
  previewEmotions = present.slice().sort((a, b) =>            // neutral first, then A–Z
    a === "neutral" ? -1 : b === "neutral" ? 1 : a.localeCompare(b));
  if (!previewEmotions.length) {
    img.hidden = true; empty.hidden = false; label.textContent = "";
    return;
  }
  empty.hidden = true;
  if (previewIdx >= previewEmotions.length) previewIdx = 0;
  showExprPreview();
}

function showExprPreview() {
  const card = exprCard(); if (!card || !previewEmotions.length) return;
  const emo = previewEmotions[previewIdx];
  const img = $("#expr-preview-img"), label = $("#expr-preview-label");
  const tag = `${emo} (${previewIdx + 1}/${previewEmotions.length})`;
  img.onload = () => { label.textContent = `${tag} · ${img.naturalWidth}×${img.naturalHeight}px`; };
  img.src = `/api/cards/${card.id}/expression/${emo}?t=${Date.now()}`;
  img.hidden = false;
  label.textContent = tag;
}

function cycleExprPreview(d) {
  if (!previewEmotions.length) return;
  previewIdx = (previewIdx + d + previewEmotions.length) % previewEmotions.length;
  showExprPreview();
}
$("#expr-prev").addEventListener("click", () => cycleExprPreview(-1));
$("#expr-next").addEventListener("click", () => cycleExprPreview(1));

$("#expr-denoise").addEventListener("input", (e) => { $("#expr-denoise-val").textContent = Number(e.target.value).toFixed(2); });
$("#expr-generate").addEventListener("click", async () => {
  const card = exprCard(); if (!card) return;
  const checkpoint = $("#expr-checkpoint").value;
  if (!checkpoint) { $("#expr-status").textContent = "Pick a checkpoint first."; return; }
  const preset = $("#expr-preset").value;
  const btn = $("#expr-generate");
  btn.disabled = true;
  $("#expr-status").textContent = preset === "full"
    ? "Generating 28 sprites… (this can take a couple of minutes)"
    : "Generating 8 sprites… (about a minute)";
  try {
    const res = await api(`/cards/${card.id}/expressions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        appearance: $("#expr-appearance").value,
        checkpoint, style: $("#expr-style").value, preset,
        transparent: $("#expr-transparent").checked,
        from_upload: $("#expr-from-upload").checked,
        denoise: Number($("#expr-denoise").value)
      })
    });
    $("#expr-status").textContent = `Done — ${res.generated.length} sprites.`;
    card.has_expressions = true;
    card.expression_labels = res.generated.slice();
    card.appearance = $("#expr-appearance").value;
    card.expr_from_upload = $("#expr-from-upload").checked;
    card.expr_denoise = Number($("#expr-denoise").value);
    updatePortrait();
    previewIdx = 0;
    await refreshExprPreview();     // show the freshly generated set at in-chat size
  } catch (e) {
    $("#expr-status").textContent = "Failed: " + (e.detail || e.message || e);
  } finally {
    btn.disabled = false;
  }
});

// ---- expression gallery ---------------------------------------------------
let galCheckpointsLoaded = false;
async function loadGalCheckpoints() {
  if (galCheckpointsLoaded) return;
  const sel = $("#gal-checkpoint");
  try {
    const { checkpoints } = await api("/comfyui/checkpoints");
    sel.innerHTML = checkpoints.map(c => `<option value="${c}">${c}</option>`).join("");
    galCheckpointsLoaded = true;
  } catch { sel.innerHTML = `<option value="">ComfyUI unreachable</option>`; }
}

async function openGallery() {
  const card = exprCard(); if (!card) return;
  $("#gal-title").textContent = `Expressions — ${card.name}`;
  $("#gallery").hidden = false;
  await loadGalCheckpoints();
  await refreshGallery();
}

async function refreshGallery() {
  const card = exprCard(); if (!card) return;
  const [sets, present] = await Promise.all([
    api("/expressions/emotions"),
    api(`/cards/${card.id}/expressions`)
  ]);
  const have = new Set(present.emotions);
  const isFull = present.emotions.some(e => !sets.core.includes(e));
  const target = isFull ? sets.full : sets.core;
  card.has_expressions = present.emotions.length > 0;   // keep portrait state in sync
  card.expression_labels = present.emotions.slice();
  $("#gal-count").textContent =
    `${present.emotions.length} generated · showing ${isFull ? "Full 28" : "Core 8"}`;
  const grid = $("#gal-grid"); grid.innerHTML = "";
  for (const emo of target) {
    const has = have.has(emo);
    const cell = document.createElement("div");
    cell.className = "gal-cell" + (has ? "" : " empty");
    cell.innerHTML =
      `<div class="gal-img">` +
        (has ? `<img src="/api/cards/${card.id}/expression/${emo}?t=${Date.now()}" alt="${emo}">`
             : `<div class="gal-empty">—</div>`) +
      `</div>` +
      `<div class="gal-label">${emo}</div>` +
      `<div class="gal-actions">` +
        `<button class="btn-mini gal-regen" title="${has ? "Regenerate" : "Generate"}">↻</button>` +
        (has ? `<button class="btn-mini danger gal-del" title="Delete">✕</button>` : ``) +
      `</div>`;
    cell.querySelector(".gal-regen").onclick = () => regenOne(emo);
    const del = cell.querySelector(".gal-del");
    if (del) del.onclick = () => delOne(emo);
    grid.appendChild(cell);
  }
}

async function regenOne(emo) {
  const card = exprCard(); if (!card) return;
  const checkpoint = $("#gal-checkpoint").value;
  if (!checkpoint) { $("#gal-count").textContent = "Pick a checkpoint (ComfyUI must be running)."; return; }
  $("#gal-count").textContent = `Generating ${emo}…`;
  try {
    await api(`/cards/${card.id}/expression/${emo}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ checkpoint, style: $("#gal-style").value })
    });
    await refreshGallery();
    updatePortrait();
  } catch (e) { $("#gal-count").textContent = "Failed: " + (e.detail || e.message || e); }
}

async function delOne(emo) {
  const card = exprCard(); if (!card) return;
  if (!confirm(`Delete the "${emo}" sprite?`)) return;
  await api(`/cards/${card.id}/expression/${emo}`, { method: "DELETE" });
  await refreshGallery();
  updatePortrait();
}

$("#gallery-btn").addEventListener("click", openGallery);
$("#gal-close").addEventListener("click", () => { $("#gallery").hidden = true; });
$("#gallery").addEventListener("click", (e) => { if (e.target.id === "gallery") $("#gallery").hidden = true; });

// ---- card editor ----------------------------------------------------------
let ceEditingId = null, ceBase = {}, ceImage = undefined, ceEntries = [];

async function openCardEditor(card) {
  ceEditingId = card ? card.id : null;
  ceImage = undefined;
  // Fetch the full card (the list is trimmed) so we preserve expressions/lorebook on save.
  ceBase = card ? await api("/cards/" + card.id) : {};
  const v = (id, val) => { $("#ce-" + id).value = val || ""; };
  v("name", ceBase.name); v("nickname", ceBase.nickname); v("creator", ceBase.creator);
  v("version", ceBase.character_version);
  v("description", ceBase.description); v("personality", ceBase.personality);
  v("scenario", ceBase.scenario); v("appearance", ceBase.appearance);
  v("first_mes", ceBase.first_mes); v("mes_example", ceBase.mes_example);
  v("system_prompt", ceBase.system_prompt); v("phi", ceBase.post_history_instructions);
  $("#ce-chat_mode").value = ceBase.chat_mode || "roleplay";
  v("greetings", (ceBase.alternate_greetings || []).join("\n"));
  v("tags", (ceBase.tags || []).join(", "));
  $("#ce-power").value = ceBase.power_dynamic ?? 0;
  $("#ce-social").value = ceBase.social_acceptance ?? 50;
  $("#ce-power-val").textContent = powerLabel($("#ce-power").value);
  $("#ce-social-val").textContent = $("#ce-social").value;
  $("#ce-speed").value = ceBase.tts_speed ?? 1;
  $("#ce-speed-val").textContent = Number($("#ce-speed").value).toFixed(2);
  await loadVoices();
  fillVoiceSelect($("#ce-voice"), ceBase.tts_voice);
  await refreshVrmSelect(ceBase.vrm || "");
  $("#ce-wake").value = ceBase.wake_word || "";
  $("#ce-sleep").value = ceBase.sleep_phrase || "";
  $("#ce-farewell").value = ceBase.farewell || "";
  const mode = ceBase.avatar_mode || (ceBase.vrm ? "vrm" : "sprite");
  $("#ce-mode-vrm").checked = mode === "vrm";
  $("#ce-mode-sprite").checked = mode !== "vrm";
  syncAvatarMode();
  // The sprite panel used to be opened deliberately from the chat screen; here it
  // is just part of the form, so its contents have to be ready when it appears.
  loadCheckpoints().catch(() => {});
  previewIdx = 0;
  refreshExprPreview().catch(() => {});
  // Deep-copy so edits don't mutate ceBase until saved.
  ceEntries = JSON.parse(JSON.stringify((ceBase.character_book || {}).entries || []));
  renderLore();
  const prev = $("#ce-preview");
  prev.src = ceBase.image ? `data:image/png;base64,${ceBase.image}` : "";
  prev.style.display = ceBase.image ? "block" : "none";
  $("#ce-title").textContent = card ? `Edit — ${ceBase.name}` : "New character";
  $("#ce-delete").hidden = !card;
  $("#ce-duplicate").hidden = !card;
  $("#ce-status").textContent = "";
  $("#card-editor").hidden = false;
}

const closeCardEditor = () => { $("#card-editor").hidden = true; };

// ---- avatar model (VRM) picker + import -----------------------------------
// A .vrm is opaque until something reads it: the same file can be a complete
// avatar or a mesh with no rig, and the difference only shows up as "the model
// never appeared". So every model is audited server-side and the verdict is put
// on screen here, next to the choice it informs.
let vrmModels = [];

async function refreshVrmSelect(selected) {
  const sel = $("#ce-vrm");
  try {
    vrmModels = await api("/vrm");
  } catch { vrmModels = []; }
  sel.innerHTML = '<option value="">— none (use expression sprites) —</option>';
  for (const m of vrmModels) {
    const o = document.createElement("option");
    o.value = m.name;
    o.textContent = `${m.name} (${(m.size / 1048576).toFixed(0)} MB${m.usable === false ? " — unusable" : ""})`;
    sel.appendChild(o);
  }
  sel.value = selected && vrmModels.some(m => m.name === selected) ? selected : "";
  renderVrmInfo();
}

function renderVrmInfo() {
  const box = $("#ce-vrm-info");
  const m = vrmModels.find(x => x.name === $("#ce-vrm").value);
  if (!m) {
    box.innerHTML = "This character uses generated expression sprites. " +
      "Assign a model to give her a 3D avatar instead.";
    $("#ce-vrm-delete").disabled = true;
    return;
  }
  $("#ce-vrm-delete").disabled = false;
  if (m.error) { box.innerHTML = `<span class="bad">unreadable: ${m.error}</span>`; return; }
  const L = m.license || {};
  const yes = (v) => v === true || v === "Allow" || v === "allow" || v === "everyone";
  const flag = (label, v) =>
    `<span class="${yes(v) ? "ok" : "warn"}">${label}: ${v === undefined || v === null ? "?" : v}</span>`;
  const bits = [
    `<code>${m.spec}</code> ${m.title || "untitled"}${m.author ? " — " + m.author : ""}`,
    `${(m.triangles || 0).toLocaleString()} tris · ${m.springs} spring group(s) · ` +
      `${m.emotive.length}/5 expressions · ${m.visemes.length}/5 visemes` +
      `${m.blink ? " · blink" : ""}${m.look_at ? " · gaze" : ""}`,
    [flag("redistribution", L.redistribution), flag("sexual use", L.sexual),
     flag("users", L.allowed_user)].join(" &nbsp;·&nbsp; "),
  ];
  if (m.problems && m.problems.length)
    bits.push(`<span class="bad">${m.problems.join("; ")}</span>`);
  if (m.notes && m.notes.length)
    bits.push(`<span class="warn">${m.notes.join("; ")}</span>`);
  box.innerHTML = bits.join("<br>");
}

$("#ce-vrm").addEventListener("change", renderVrmInfo);

// Show one mode's controls at a time. Both keep their data: switching to sprites
// does not clear an assigned model, so flipping back and forth is free.
function syncAvatarMode() {
  const vrm = $("#ce-mode-vrm").checked;
  $("#ce-avatar-vrm").hidden = !vrm;
  $("#ce-avatar-sprite").hidden = vrm;
}
$("#ce-mode-vrm").addEventListener("change", syncAvatarMode);
$("#ce-mode-sprite").addEventListener("change", syncAvatarMode);

$("#ce-vrm-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const box = $("#ce-vrm-info");
  box.textContent = `Importing ${f.name}…`;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const res = await fetch("/api/vrm", { method: "POST", body: fd });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    await refreshVrmSelect(body.name);          // select what was just imported
  } catch (err) {
    box.innerHTML = `<span class="bad">Import failed: ${err.message}</span>`;
  }
});

$("#ce-vrm-delete").addEventListener("click", async () => {
  const name = $("#ce-vrm").value;
  if (!name) return;
  if (!confirm(`Delete ${name}? The file is removed from disk.`)) return;
  const box = $("#ce-vrm-info");
  try {
    const res = await fetch("/api/vrm/" + encodeURIComponent(name), { method: "DELETE" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || res.statusText);
    await refreshVrmSelect("");
  } catch (err) {
    box.innerHTML = `<span class="bad">${err.message}</span>`;
  }
});

// The sprite workflow moved from the chat screen into the card editor, so it has
// to act on the card being EDITED, not the one open in chat -- those are usually
// different, and generating sprites onto the wrong character is not a mistake you
// notice quickly. Falls back to the chat card so the gallery still works when
// opened from there.
function exprCard() {
  return ($("#card-editor") && !$("#card-editor").hidden && ceBase && ceBase.id)
    ? ceBase : currentCard();
}

function buildCardPayload() {
  const g = id => $("#ce-" + id).value.trim();
  const lines = s => s.split("\n").map(x => x.trim()).filter(Boolean);
  const csv = s => s.split(",").map(x => x.trim()).filter(Boolean);
  return {
    ...ceBase,
    name: g("name") || "Companion", nickname: g("nickname"), creator: g("creator"),
    character_version: g("version"),
    description: g("description"), personality: g("personality"),
    scenario: g("scenario"), appearance: g("appearance"),
    first_mes: g("first_mes"), mes_example: g("mes_example"),
    alternate_greetings: lines($("#ce-greetings").value),
    system_prompt: g("system_prompt"), chat_mode: $("#ce-chat_mode").value,
    post_history_instructions: g("phi"),
    tags: csv($("#ce-tags").value),
    power_dynamic: Number($("#ce-power").value),
    social_acceptance: Number($("#ce-social").value),
    tts_voice: $("#ce-voice").value,
    tts_speed: Number($("#ce-speed").value),
    vrm: $("#ce-vrm").value,
    avatar_mode: $("#ce-mode-vrm").checked ? "vrm" : "sprite",
    wake_word: $("#ce-wake").value.trim(),
    sleep_phrase: $("#ce-sleep").value.trim(),
    farewell: $("#ce-farewell").value.trim(),
    image: ceImage !== undefined ? ceImage : (ceBase.image || null),
    character_book: ceEntries.length
      ? { ...(ceBase.character_book || {}),
          name: (ceBase.character_book && ceBase.character_book.name) || (g("name") || "Companion"),
          entries: ceEntries }
      : null,
  };
}

async function duplicateCard() {
  const payload = buildCardPayload();
  delete payload.id; delete payload.created_at; delete payload.updated_at;
  payload.name = (payload.name || "Character") + " (copy)";
  $("#ce-status").textContent = "Duplicating…";
  try {
    const nc = await api("/cards", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    await loadCards();
    openCardEditor(nc);                 // open the new copy for editing
  } catch (e) { $("#ce-status").textContent = "Failed: " + (e.detail || e.message || e); }
}

async function saveCard() {
  const payload = buildCardPayload();
  $("#ce-save").disabled = true; $("#ce-status").textContent = "Saving…";
  try {
    const opts = { method: ceEditingId ? "PUT" : "POST",
                   headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
    await api(ceEditingId ? "/cards/" + ceEditingId : "/cards", opts);
    await loadCards();
    if (state.session && ceEditingId === state.session.character_id) {
      renderMessages();
      // Re-evaluate the avatar. Changing a character's model is a change to the
      // card she is already displaying, so nothing else would notice.
      syncAvatarCard();
      updatePortrait();
    }
    closeCardEditor();
  } catch (e) {
    $("#ce-status").textContent = "Failed: " + (e.detail || e.message || e);
  } finally { $("#ce-save").disabled = false; }
}

async function deleteCard() {
  if (!ceEditingId || !confirm("Delete this character permanently?")) return;
  await api("/cards/" + ceEditingId, { method: "DELETE" });
  await loadCards();
  closeCardEditor();
}

function renderLore() {
  const box = $("#ce-lore-list"); box.innerHTML = "";
  ceEntries.forEach((e, i) => {
    const row = document.createElement("div");
    row.className = "lore-entry";
    row.innerHTML =
      `<div class="lore-top">` +
        `<input class="field lore-keys" placeholder="Trigger keys (comma separated)">` +
        `<label class="check"><input type="checkbox" class="lore-const"> Always</label>` +
        `<select class="field lore-pos">` +
          `<option value="before_char">Before</option><option value="after_char">After</option></select>` +
        `<select class="field lore-stage" title="Unlock only at/above this relationship stage">` +
          `<option value="">Any stage</option><option value="Acquaintance">Acquaintance+</option>` +
          `<option value="Friend">Friend+</option><option value="Close">Close+</option>` +
          `<option value="Romantic">Romantic+</option><option value="Intimate">Intimate+</option></select>` +
        `<button class="btn-mini danger lore-del" type="button" title="Remove entry">✕</button>` +
      `</div>` +
      `<textarea class="field lore-content" rows="2" placeholder="Text injected when a key matches…"></textarea>`;
    const keys = row.querySelector(".lore-keys"), cont = row.querySelector(".lore-content");
    const cst = row.querySelector(".lore-const"), pos = row.querySelector(".lore-pos");
    const stg = row.querySelector(".lore-stage");
    keys.value = (e.keys || []).join(", ");
    cont.value = e.content || "";
    cst.checked = !!e.constant;
    pos.value = e.position || "before_char";
    stg.value = e.min_stage || "";
    keys.oninput = () => e.keys = keys.value.split(",").map(x => x.trim()).filter(Boolean);
    cont.oninput = () => e.content = cont.value;
    cst.onchange = () => e.constant = cst.checked;
    pos.onchange = () => e.position = pos.value;
    stg.onchange = () => e.min_stage = stg.value;
    row.querySelector(".lore-del").onclick = () => { ceEntries.splice(i, 1); renderLore(); };
    box.appendChild(row);
  });
}

$("#ce-add-lore").addEventListener("click", () => {
  ceEntries.push({ keys: [], content: "", constant: false, position: "before_char", enabled: true });
  renderLore();
});

$("#new-card").addEventListener("click", () => openCardEditor(null));
$("#ce-close").addEventListener("click", closeCardEditor);
$("#ce-cancel").addEventListener("click", closeCardEditor);
$("#ce-save").addEventListener("click", saveCard);
$("#ce-delete").addEventListener("click", deleteCard);
$("#ce-duplicate").addEventListener("click", duplicateCard);
$("#card-editor").addEventListener("click", (e) => { if (e.target.id === "card-editor") closeCardEditor(); });
$("#ce-speed").addEventListener("input", (e) => { $("#ce-speed-val").textContent = Number(e.target.value).toFixed(2); });
// Test with the pace being edited, not the saved one — the point is to hear the change.
$("#ce-voice-test").addEventListener("click", () => playSample($("#ce-voice").value, Number($("#ce-speed").value)));
$("#ce-power").addEventListener("input", (e) => { $("#ce-power-val").textContent = powerLabel(e.target.value); });
$("#ce-social").addEventListener("input", (e) => { $("#ce-social-val").textContent = e.target.value; });
$("#ce-image").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => {
    ceImage = String(r.result).split(",")[1];      // strip the data: URL prefix
    const p = $("#ce-preview"); p.src = r.result; p.style.display = "block";
  };
  r.readAsDataURL(f);
});

// ---- relationship presets -------------------------------------------------
const PRESET_SLIDERS = [
  ["power_dynamic", "Power dynamic", -100, 100],
  ["social_acceptance", "Social acceptance", 0, 100],
  ["affection", "Affection", 0, 100],
  ["trust", "Trust", 0, 100],
  ["attraction", "Attraction", 0, 100],
  ["tension", "Tension", 0, 100],
  ["emotional_intimacy", "Emotional intimacy", 0, 100],
  ["physical_willingness", "Physical willingness", 0, 100],
];
let prEditingId = null, prBase = {};

async function loadPresets() {
  state.presets = await api("/presets");
  const ul = $("#preset-list"); ul.innerHTML = "";
  for (const p of state.presets) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="li-name">${p.name}</span><button class="li-edit" title="Edit preset">✎</button>`;
    li.onclick = () => openPresetEditor(p);
    li.querySelector(".li-edit").onclick = (e) => { e.stopPropagation(); openPresetEditor(p); };
    ul.appendChild(li);
  }
}

function fillSelect(sel, items, current, anyLabel) {
  sel.innerHTML = `<option value="">${anyLabel}</option>` +
    items.map(i => `<option value="${i.id}">${i.name}</option>`).join("");
  sel.value = current || "";
}

function renderPresetSliders(p) {
  const box = $("#pr-sliders"); box.innerHTML = "";
  for (const [key, label, min, max] of PRESET_SLIDERS) {
    const val = p[key] ?? (key === "social_acceptance" ? 50 : 0);
    const row = document.createElement("label");
    row.className = "pr-slider";
    row.innerHTML = `<span>${label}</span><input type="range" min="${min}" max="${max}" step="5" ` +
      `data-key="${key}" value="${val}"><output>${val}</output>`;
    const inp = row.querySelector("input"), out = row.querySelector("output");
    inp.addEventListener("input", () => { out.textContent = inp.value; });
    box.appendChild(row);
  }
}

function openPresetEditor(p) {
  prEditingId = p ? p.id : null;
  prBase = p || {};
  $("#pr-name").value = prBase.name || "";
  $("#pr-description").value = prBase.description || "";
  $("#pr-scenario").value = prBase.scenario_note || "";
  fillSelect($("#pr-character"), state.cards, prBase.character_id, "— any character —");
  fillSelect($("#pr-persona"), state.personas, prBase.persona_id, "— any persona —");
  renderPresetSliders(prBase);
  $("#pr-title").textContent = p ? `Edit — ${prBase.name}` : "New preset";
  $("#pr-delete").hidden = !p;
  $("#pr-status").textContent = "";
  $("#preset-editor").hidden = false;
}
const closePresetEditor = () => { $("#preset-editor").hidden = true; };

async function savePreset() {
  const payload = { ...prBase, name: $("#pr-name").value.trim() || "New preset",
    description: $("#pr-description").value, scenario_note: $("#pr-scenario").value,
    character_id: $("#pr-character").value, persona_id: $("#pr-persona").value };
  $("#pr-sliders").querySelectorAll("input[data-key]").forEach(inp => payload[inp.dataset.key] = Number(inp.value));
  $("#pr-save").disabled = true; $("#pr-status").textContent = "Saving…";
  try {
    const opts = { method: prEditingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
    await api(prEditingId ? "/presets/" + prEditingId : "/presets", opts);
    await loadPresets();
    closePresetEditor();
  } catch (e) { $("#pr-status").textContent = "Failed: " + (e.detail || e.message || e); }
  finally { $("#pr-save").disabled = false; }
}

async function deletePreset() {
  if (!prEditingId || !confirm("Delete this preset?")) return;
  await api("/presets/" + prEditingId, { method: "DELETE" });
  await loadPresets();
  closePresetEditor();
}

$("#new-preset").addEventListener("click", () => openPresetEditor(null));
$("#pr-close").addEventListener("click", closePresetEditor);
$("#pr-cancel").addEventListener("click", closePresetEditor);
$("#pr-save").addEventListener("click", savePreset);
$("#pr-delete").addEventListener("click", deletePreset);
$("#preset-editor").addEventListener("click", (e) => { if (e.target.id === "preset-editor") closePresetEditor(); });

// ---- voice (local TTS + amplitude lip-sync) -------------------------------
let ttsEnabled = false, audioCtx = null, ttsRun = 0, ttsSource = null, mouthRAF = null;
let ttsLevel = 0, ttsHangoverUntil = 0;   // her live output RMS + a short tail — used to ignore herself
let speechQueue = [], speechConsuming = false, speechFedLen = 0, anyQuote = false;
// Inline [expr:x] beat state. Declared here, beside the speech queue it rides on, so it
// is initialised before stopSpeaking() (below) can touch it.
// What actually reached the speakers, for the self-hearing check below.
let spokenRecent = [];
let selfHeardCount = 0;
let exprPending = null;      // beat waiting for a segment to attach to
let exprLive = null;         // beat currently shown, so we don't re-fetch the same sprite
let ttsVoicesList = [];

async function loadVoices() {
  if (ttsVoicesList.length) return ttsVoicesList;
  try { ttsVoicesList = (await api("/tts/voices")).voices || []; } catch { ttsVoicesList = []; }
  return ttsVoicesList;
}

function fillVoiceSelect(sel, current) {
  sel.innerHTML = ttsVoicesList.map(v => `<option value="${v}">${v}</option>`).join("");
  sel.value = ttsVoicesList.includes(current) ? current
    : (ttsVoicesList.includes("af_sarah") ? "af_sarah" : (ttsVoicesList[0] || ""));
}

function currentVoice() { return (currentCard() && currentCard().tts_voice) || "af_sarah"; }
// Per-character speaking pace. Kokoro takes it as a speed multiplier, Piper inverts it onto
// length_scale (services/tts.py) — either way 1.0 is the voice's own tempo.
function currentSpeed() { const c = currentCard(); return (c && c.tts_speed) || 1; }

function ensureAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

function stopSpeaking() {
  ttsRun++;                                   // invalidate any in-flight queue
  speechQueue = []; speechConsuming = false; speechFedLen = 0; anyQuote = false;
  // Beats belong to the reply that was speaking. Called at the top of every reply too,
  // so this is also the per-reply reset: a repeated label still re-applies next time.
  exprPending = null; exprLive = null;
  try { if (ttsSource) ttsSource.stop(); } catch {}
  ttsSource = null; ttsLevel = 0;
  if (mouthRAF) { cancelAnimationFrame(mouthRAF); mouthRAF = null; }
  const img = $("#portrait"); if (img) img.style.transform = "";
}

async function fetchTTS(sentence, voice, speed) {
  try {
    const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sentence, voice: voice || currentVoice(),
                             speed: speed || currentSpeed() }) });
    return r.ok ? await r.arrayBuffer() : null;
  } catch { return null; }
}

function playBuffer(ctx, arrayBuf, valid) {
  return new Promise(async (resolve) => {
    let audio;
    try { audio = await ctx.decodeAudioData(arrayBuf.slice(0)); } catch { return resolve(); }
    if (!valid()) return resolve();
    const src = ctx.createBufferSource(); src.buffer = audio;
    const analyser = ctx.createAnalyser(); analyser.fftSize = 256;
    src.connect(analyser); analyser.connect(ctx.destination);
    ttsSource = src;
    const data = new Uint8Array(analyser.frequencyBinCount);
    const img = $("#portrait");
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0; for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
      const amp = Math.sqrt(sum / data.length);
      ttsLevel = amp;                     // publish her output level for the mic's self-suppression
      if (img && !img.hidden) img.style.transform = `scaleY(${1 + Math.min(amp * 0.5, 0.06)})`;
      // Same signal the sprite mouth uses; 3c drives the jaw bone with it.
      if (avatarActive)
        window.dispatchEvent(new CustomEvent("avatar:speak", { detail: { amp } }));
      mouthRAF = requestAnimationFrame(tick);
    };
    tick();
    src.onended = () => {
      if (mouthRAF) { cancelAnimationFrame(mouthRAF); mouthRAF = null; }
      if (img) img.style.transform = "";
      ttsSource = null; ttsLevel = 0; ttsHangoverUntil = performance.now() + 400;  // tail: ignore reverb/echo after she stops
      resolve();
    };
    src.start();
  });
}

// Prose the synthesizer can actually say. DEFAULT_ASSISTANT tells assistant characters not
// to emit markdown at all, but that is steering, not a gate — the 9B fine-tune answered a
// question about Dunbar's number with a table, which reads aloud as a hedge of pipes and
// dashes. So the layout is unwrapped here as well: table rows become their cells, list and
// heading markers are dropped while their text survives, and fenced code — which has no
// sensible spoken form — goes entirely.
function cleanForTTS(t) {
  return t
    .replace(/```[\s\S]*?```/g, " ")            // fenced code: nothing to say
    .replace(/^[ \t]*\|[\s|:-]*\|[ \t]*$/gm, " ")  // table rule row (|---|:--|)
    .replace(/^[ \t]*\|(.*)\|[ \t]*$/gm, (_, row) => {   // table row -> "cell, cell, cell."
      const line = row.split("|").map(c => c.trim()).filter(Boolean).join(", ");
      return /[.!?]$/.test(line) ? line : line + ".";     // cells often end in one already
    })
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")   // heading marker, keep the heading
    .replace(/^[ \t]*[-*+][ \t]+/gm, "")        // bullet marker, keep the item
    .replace(/^[ \t]*\d+[.)][ \t]+/gm, "")      // numbered marker, keep the item
    .replace(/~(?=\d)/g, "about ")              // "~150" -> "about 150"
    .replace(/\*[^*]*\*/g, " ")                 // *action* narration
    .replace(/\[expr:[^\]]*\]/gi, " ")          // stray [expr:x] beat — never say it aloud
    .replace(/[`"_]/g, "")
    .replace(/\s+/g, " ").trim();
}

// ---- live expression beats ([expr:x] from the stream) ----------------------
// The model tags beats mid-reply, but tokens run well ahead of sound: consumeSpeech()
// queues AND prefetches audio, so applying a beat when its event arrives would change
// the face seconds before the matching line is heard. Instead a beat waits here, rides
// the next speech segment, and is applied when that segment actually starts playing.
function applyExpr(label) {
  const card = currentCard();
  if (!card || !label || label === exprLive) return;
  if (avatarActive) {            // 3d will drive the avatar's expression track with this
    exprLive = label;
    window.dispatchEvent(new CustomEvent("avatar:expr", { detail: { label } }));
    return;
  }
  if (!card.has_expressions) return;
  // /api/cards trims the sprite blobs, so labels come from expression_labels.
  const have = card.expression_labels || Object.keys(card.expressions || {});
  if (have.length && !have.includes(label)) return;        // no sprite for it — leave the portrait
  exprLive = label;
  const img = $("#portrait");
  if (img) { img.src = `/api/cards/${card.id}/expression/${label}?t=${Date.now()}`; img.hidden = false; }
}

function pendingExpr(label) {
  // Voice off (or nothing will be spoken): no playback clock to sync to, so show it now.
  if (!ttsEnabled) { applyExpr(label); return; }
  exprPending = label;
}

// End of reply: a beat tagged after the last spoken segment never got attached.
function flushExpr() {
  if (exprPending) { applyExpr(exprPending); exprPending = null; }
}

function enqueueSpeech(segment) {
  if (!ttsEnabled) return;
  const clean = cleanForTTS(segment);
  if (!clean) return;
  spokenRecent.push({ text: clean.toLowerCase(), t: performance.now() });
  if (spokenRecent.length > 40) spokenRecent.shift();
  speechQueue.push({ text: clean, run: ttsRun, expr: exprPending });
  exprPending = null;                      // consumed by this segment
  consumeSpeech();
}

async function consumeSpeech() {
  if (speechConsuming) return;
  speechConsuming = true;
  const ctx = ensureAudioCtx();
  let prefetch = null;                          // { item, promise }
  while (speechQueue.length) {
    const item = speechQueue.shift();
    if (item.run !== ttsRun) { prefetch = null; continue; }
    const buf = (prefetch && prefetch.item === item) ? await prefetch.promise : await fetchTTS(item.text);
    const nxt = speechQueue[0];                 // prefetch next while this one plays
    prefetch = nxt ? { item: nxt, promise: fetchTTS(nxt.text) } : null;
    if (item.expr && item.run === ttsRun) applyExpr(item.expr);   // in sync with the audio
    if (buf && item.run === ttsRun) await playBuffer(ctx, buf, () => item.run === ttsRun);
  }
  speechConsuming = false;
  if (speechQueue.some(i => i.run === ttsRun)) consumeSpeech();   // race: items queued as we exited
}

// Feed the growing reply; enqueue only the QUOTED dialogue (skip narration + *actions*).
// Fallback: if a whole reply has no quotes at all, speak it all (actions stripped).
//
// Assistant-mode cards are the exception: they have no scene, so the whole reply IS her
// speaking and quotes mark a term, a title, or something being cited — not dialogue.
// Feeding only the quoted spans there speaks the citations and drops the answer.
function feedSpeech(fullText, final) {
  if (!ttsEnabled) return;
  if (((currentCard() || {}).chat_mode || "roleplay") === "assistant")
    return feedSpeechWhole(fullText, final);
  const norm = fullText.replace(/[“”]/g, '"');   // normalize smart double-quotes
  while (true) {
    const open = norm.indexOf('"', speechFedLen);
    if (open < 0) break;
    const close = norm.indexOf('"', open + 1);
    if (close < 0) break;                                  // quote still open — wait for more tokens
    const line = norm.slice(open + 1, close).trim();
    if (line) { enqueueSpeech(line); anyQuote = true; }
    speechFedLen = close + 1;
  }
  if (final) {
    if (!anyQuote) enqueueSpeech(cleanForTTS(fullText));    // no dialogue quotes -> speak the whole reply
    speechFedLen = fullText.length;
  }
}

// Assistant mode: speak everything, flushed a sentence at a time so playback starts
// while the reply is still streaming instead of waiting for the whole thing. The
// terminator must be followed by real whitespace, not end-of-buffer — mid-stream the
// text ends wherever the last token landed, and "$" there would cut "3." out of "3.14"
// and speak it as its own sentence.
function feedSpeechWhole(fullText, final) {
  const SENT = /[.!?…]["')\]]*\s/g;
  SENT.lastIndex = speechFedLen;
  let end = -1, m;
  while ((m = SENT.exec(fullText))) end = m.index + m[0].length;
  if (end > speechFedLen) {
    enqueueSpeech(fullText.slice(speechFedLen, end));
    speechFedLen = end;
  }
  if (final) {
    if (speechFedLen < fullText.length) enqueueSpeech(fullText.slice(speechFedLen));
    speechFedLen = fullText.length;
  }
}

$("#tts-btn").addEventListener("click", async () => {
  ttsEnabled = !ttsEnabled;
  $("#tts-btn").textContent = ttsEnabled ? "🔊" : "🔇";
  $("#voice-test").hidden = !ttsEnabled;
  if (ttsEnabled) { ensureAudioCtx(); await loadVoices(); } else stopSpeaking();
});
async function playSample(voice, speed) {
  const ctx = ensureAudioCtx();
  stopSpeaking();                       // cut anything playing; new run id
  const run = ttsRun;
  const buf = await fetchTTS("Hi there. This is how I sound to you.", voice, speed);
  if (buf && run === ttsRun) await playBuffer(ctx, buf, () => run === ttsRun);
}
$("#voice-test").addEventListener("click", () => playSample(currentVoice()));

// ---- speech input (push-to-talk STT + barge-in) ---------------------------
let micStream = null, mediaRec = null, recChunks = [], recording = false;

async function ensureMic() {
  if (micStream) return micStream;
  micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  return micStream;
}

async function startRecording() {
  if (recording || $("#mic").disabled) return;
  stopSpeaking();        // barge-in: cut her voice…
  abortChat();           // …and stop any in-flight generation
  let stream;
  try { stream = await ensureMic(); }
  catch { alert("Microphone access denied."); return; }
  recChunks = [];
  mediaRec = new MediaRecorder(stream);
  mediaRec.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
  mediaRec.onstop = onRecordingStop;
  mediaRec.start();
  recording = true;
  $("#mic").classList.add("recording");
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  $("#mic").classList.remove("recording");
  try { if (mediaRec && mediaRec.state !== "inactive") mediaRec.stop(); } catch {}
}

async function transcribeBlob(blob) {
  try {
    const fd = new FormData();
    fd.append("file", blob, "speech.webm");
    const r = await fetch("/api/stt", { method: "POST", body: fd });
    return r.ok ? ((await r.json()).text || "") : "";
  } catch { return ""; }
}

async function onRecordingStop() {
  const blob = new Blob(recChunks, { type: (mediaRec && mediaRec.mimeType) || "audio/webm" });
  recChunks = [];
  if (!blob.size) return;
  $("#mic").classList.add("busy");
  try {
    const t = (await transcribeBlob(blob)).trim();   // push-to-talk: no wake-word gate (explicit press)
    if (t && !state.streaming) send(t, false);
  } finally { $("#mic").classList.remove("busy"); }
}

const mic = $("#mic");
mic.addEventListener("pointerdown", (e) => { e.preventDefault(); startRecording(); });
mic.addEventListener("pointerup",   (e) => { e.preventDefault(); stopRecording(); });
mic.addEventListener("pointerleave", () => stopRecording());
mic.addEventListener("pointercancel", () => stopRecording());

// ---- hands-free (continuous VAD) + voice settings -------------------------
// Separate switch from push-to-talk: the 🎙 toggle arms an open-mic loop that
// uses an in-browser RMS energy gate (VAD) to segment utterances, transcribes
// each via /api/stt, and — if a wake word is set — only sends speech that
// starts with it (which also barges in). Fully local; no extra model. A true
// wake-word engine (openWakeWord) + Silero-VAD are the documented upgrades.
let voiceSettings = { hands_free: false, wake_word: "", vad_silence_ms: 800, vad_threshold: 0.015,
  full_duplex: false, mic_device_id: "", mic_device_label: "", desktop_card_id: "",
  wake_mode: "phrase", sleep_phrase: "that will be all", sleep_after_s: 180,
  farewell: "Goodbye.",
  summarize_after: 24, keep_recent: 12, summary_max_tokens: 400,
  recall_enabled: true, recall_top_k: 4, auto_facts_enabled: true,
  auto_facts_max_new: 3 };
let hfStream = null, hfCtx = null, hfAnalyser = null, hfRAF = null;
let hfSpeaking = false, hfSilenceStart = 0, hfActive = false;

// ---- self-hearing guard ---------------------------------------------------
// full_duplex is only safe when her voice cannot reach the mic. If it is on
// with speakers and no echo canceller, she hears herself, transcribes it, and
// answers it -- a loop that looks like the app malfunctioning rather than like
// a misconfiguration. Rather than trust the setup, check the symptom: does what
// we just heard match what we just said?
function looksSelfHeard(text) {
  if (!voiceSettings.full_duplex) return false;           // half-duplex cannot self-hear
  const now = performance.now();
  const recent = spokenRecent.filter(s => now - s.t < 20000);
  if (!recent.length) return false;
  const said = recent.map(s => s.text).join(" ");
  const words = String(text).toLowerCase().match(/[a-z']{3,}/g) || [];
  if (words.length < 3) return false;                     // too short to judge
  const hits = words.filter(w => said.includes(w)).length;
  return hits / words.length > 0.7;                       // most of it came from her
}

function noteSelfHeard() {
  selfHeardCount++;
  if (selfHeardCount < 2) return;                         // one may be coincidence
  // Two in a row is a configuration fault, not bad luck. Stop the loop, say why.
  voiceSettings.full_duplex = false;
  persistSettings({ full_duplex: false });
  selfHeardCount = 0;
  showToast("She was hearing herself — interruption turned off. " +
            "Use headphones, or run scripts/setup-aec.sh --persist and pick " +
            "aec_source in ⚙ Settings.");
}

// The character's own wake word wins over the global one. With several
// characters a single global name is the wrong shape -- you address whoever you
// are talking to, and in desktop mode she is the only one listening.
function activeWakeWord() {
  const card = currentCard();
  return ((card && card.wake_word) || voiceSettings.wake_word || "").trim();
}

function activeFarewell() {
  const card = currentCard();
  const v = (card && card.farewell !== undefined && card.farewell !== "")
    ? card.farewell : voiceSettings.farewell;
  return (v === undefined ? "Goodbye." : v).trim();
}

// Say one line now, outside the reply queue. Silence after the sleep phrase is
// ambiguous -- it looks the same whether she heard you and closed the
// conversation or simply did not hear you at all.
//
// Deliberately not routed through the model: this is a confirmation, and it has
// to be immediate and predictable rather than a generated turn that takes
// seconds and might ramble. It is not added to the transcript either, for the
// same reason -- it is UI feedback, not something she said in the conversation.
async function speakLine(text) {
  if (!text || !ttsEnabled) return;
  try {
    const ctx = ensureAudioCtx();
    stopSpeaking();                       // nothing else should be mid-sentence
    const run = ttsRun;
    const buf = await fetchTTS(text);
    if (buf && run === ttsRun) await playBuffer(ctx, buf, () => run === ttsRun);
  } catch (e) { console.warn("[tts] farewell failed:", e); }
}

function activeSleepPhrase() {
  const card = currentCard();
  return ((card && card.sleep_phrase) || voiceSettings.sleep_phrase || "").trim();
}

// ---- conversation state ----------------------------------------------------
// In "phrase" mode every utterance must start with the wake word. That is fine
// for one-shot commands and wrong for a conversation: answering a question she
// just asked meant saying her name again.
//
// In "continuous" mode the wake word opens a conversation and follow-ups need no
// prefix, until the sleep phrase or the idle timeout closes it. The timeout is
// not a nicety -- an open mic that never closes keeps transcribing the room
// after you walk away.
let awake = false, awakeUntil = 0;

function setAwake(on, why) {
  if (awake === on) {
    if (on) awakeUntil = Date.now() + wakeTimeoutMs();
    return;
  }
  awake = on;
  awakeUntil = on ? Date.now() + wakeTimeoutMs() : 0;
  console.log(`[mic] ${on ? "awake" : "asleep"} (${why})`);
  // Only when the conversation ends, and not when the mic is switched off --
  // saying goodbye to someone who just muted you is talking to an empty room.
  if (!on && why !== "mic off") speakLine(activeFarewell());
  const dot = $("#hf-btn");
  if (dot) dot.classList.toggle("awake", on);
  if (on) showToast("Listening — say \u201c" + (activeSleepPhrase() || "stop") + "\u201d when done");
}

function wakeTimeoutMs() {
  const s = voiceSettings.sleep_after_s;
  return (s === 0 || s === undefined) ? Infinity : s * 1000;
}

// Compare on words alone. Both sides carry punctuation nobody controls: Whisper
// decides whether to end a sentence with a full stop, and a phrase typed into a
// settings box may well be "That will be all." -- which would never prefix-match
// a transcript of "That will be all". Contractions are expanded because the
// transcriber picks between "that will" and "that'll" on its own.
const CONTRACTIONS = [
  [/\bthat'?ll\b/g, "that will"], [/\bthat'?s\b/g, "that is"],
  [/\bit'?s\b/g, "it is"],        [/\bwe'?re\b/g, "we are"],
  [/\bi'?m\b/g, "i am"],          [/\bdon'?t\b/g, "do not"],
  [/\bwon'?t\b/g, "will not"],    [/\bcan'?t\b/g, "cannot"],
  [/\byou'?re\b/g, "you are"],    [/\blet'?s\b/g, "let us"],
];

function normPhrase(t) {
  let x = String(t || "").toLowerCase().replace(/[\u2018\u2019]/g, "'");
  for (const [re, to] of CONTRACTIONS) x = x.replace(re, to);
  return x.replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
}

function startsWith(text, phrase) {
  const p = normPhrase(phrase);
  if (!p) return false;
  const t = normPhrase(text);
  return t === p || t.startsWith(p + " ");
}

// Drop a leading phrase, working on the ORIGINAL text so the reply keeps its
// punctuation and capitals -- only the comparison is normalised.
function stripPrefix(text, phrase) {
  const words = normPhrase(phrase).split(" ").filter(Boolean).length;
  let t = text.trim().replace(/^[^\w]+/, "");
  for (let i = 0; i < words; i++)
    t = t.replace(/^[^\s]+\s*/, "");
  return t.replace(/^[\s,.:!?-]+/, "").trim();
}

// Returns the text to send, or "" to ignore this utterance.
function applyWakeWord(text) {
  const w = activeWakeWord();
  const sleepPhrase = activeSleepPhrase();
  const continuous = (voiceSettings.wake_mode || "phrase") === "continuous";

  // No wake word at all: pure open-mic, nothing to gate and nothing to sleep.
  if (!w) return text.trim();

  // The sleep phrase ends a conversation, with or without her name in front --
  // "Jarvis, that will be all" and "that will be all" are the same instruction.
  if (continuous && awake && sleepPhrase) {
    const bare = startsWith(text, w) ? stripPrefix(text, w) : text.trim();
    if (startsWith(bare, sleepPhrase) || startsWith(text, sleepPhrase)) {
      setAwake(false, "sleep phrase");
      return "";
    }
  }

  if (startsWith(text, w)) {
    const rest = stripPrefix(text, w);
    if (continuous) setAwake(true, "wake word");
    // "Jarvis" alone is a summons, not a message; opening the conversation is
    // the whole response. In phrase mode there is nothing to open, so it drops.
    if (!rest) return "";
    return rest;
  }

  // No wake word. Heard only if a conversation is already open and still fresh.
  if (continuous && awake) {
    if (Date.now() > awakeUntil) { setAwake(false, "idle timeout"); return ""; }
    awakeUntil = Date.now() + wakeTimeoutMs();      // each turn extends it
    return text.trim();
  }
  return "";
}

function hfStartUtterance() {
  if (!hfStream) { console.warn("[mic] no stream to record"); return; }
  hfPcm = hfPre.slice();          // keep what was said just before the gate opened
  hfCapturing = true;
  $("#hf-btn").classList.add("listening");
  console.log("[mic] capturing (pre-roll " + hfPre.length + " blocks)");
}

function hfStopUtterance() {
  $("#hf-btn").classList.remove("listening");
  if (!hfCapturing) return;
  hfCapturing = false;
  hfOnUtterance();          // no recorder to stop; the buffer is already complete
}

async function hfOnUtterance() {
  const rate = (hfCtx && hfCtx.sampleRate) || 48000;
  const blob = hfWav(hfPcm, rate);
  console.log(`[mic] utterance: ${hfPcm.length} block(s), ${blob.size} bytes @${rate}Hz, ` +
              `active=${hfActive}`);
  hfPcm = []; hfPre = [];
  if (!blob.size || !hfActive) {
    console.warn("[mic] dropped: " + (!blob.size ? "recorder produced no audio"
                                                 : "hands-free disengaged"));
    return;
  }
  const raw = (await transcribeBlob(blob)).trim();
  console.log("[mic] transcript: " + JSON.stringify(raw));
  if (awake) awakeUntil = Date.now() + wakeTimeoutMs();   // any speech keeps it open
  if (!raw) return;
  if (looksSelfHeard(raw)) { noteSelfHeard(); return; }   // never answer her own voice
  selfHeardCount = 0;                                     // a genuine turn clears it
  const msg = applyWakeWord(raw);
  if (msg && !state.streaming) send(msg, false);
}

async function fillDesktopCardSelect() {
  const sel = $("#vc-desktop-card");
  if (!sel) return;
  // Re-fetch rather than trusting state.cards. That list is only refreshed at
  // boot and after a card edit, so a tab left open since before a character was
  // created will not have it -- which is exactly when you go looking for a
  // newly added character in this dropdown.
  try { state.cards = await api("/cards"); } catch { /* keep what we have */ }
  sel.innerHTML = '<option value="">— most recent chat —</option>';
  for (const c of (state.cards || [])) {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.name + (c.has_vrm ? "  (3D avatar)" : "  (sprites)");
    sel.appendChild(o);
  }
  sel.value = voiceSettings.desktop_card_id || "";
}

// Resolve which microphone to open.
//
// deviceId is the obvious key and the wrong one to rely on alone: it is
// per-origin, rotates when permissions are reset, and cannot be set from
// outside the browser -- so a device chosen here could not be configured any
// other way. The label is stable and human-meaningful, so it is the fallback.
//
// With full duplex on and nothing chosen, prefer an echo-cancelled source
// automatically. That combination has exactly one correct answer, and expecting
// someone to know the name `aec_source` to get it is a poor trade.
async function pickMicDeviceId() {
  let devices = [];
  try {
    devices = (await navigator.mediaDevices.enumerateDevices())
      .filter(d => d.kind === "audioinput");
  } catch { return voiceSettings.mic_device_id || ""; }

  const byId = devices.find(d => d.deviceId === voiceSettings.mic_device_id);
  if (byId) return byId.deviceId;

  const label = (voiceSettings.mic_device_label || "").trim().toLowerCase();
  if (label) {
    const byLabel = devices.find(d => (d.label || "").toLowerCase().includes(label));
    if (byLabel) {
      console.log("[mic] matched saved device by label:", byLabel.label);
      return byLabel.deviceId;
    }
  }
  if (voiceSettings.full_duplex) {
    const aec = devices.find(d => /aec|echo|cancel/i.test(d.label || ""));
    if (aec) {
      console.log("[mic] full duplex: auto-selected echo-cancelled source:", aec.label);
      return aec.deviceId;
    }
    console.warn("[mic] full duplex is on but no echo-cancelled source was found — " +
                 "she may hear herself. Run scripts/setup-aec.sh --persist");
  }
  return "";
}

// ---- microphone selection (Phase 5) ---------------------------------------
// Device labels are only exposed after a getUserMedia permission grant, so an
// unprompted enumerate returns anonymous entries. Ask once, then list.
async function fillMicSelect() {
  const sel = $("#vc-mic");
  sel.innerHTML = '<option value="">Browser default</option>';
  let devices = [];
  try {
    devices = (await navigator.mediaDevices.enumerateDevices())
      .filter(d => d.kind === "audioinput");
    if (devices.length && !devices[0].label) {         // no permission yet
      const tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
      tmp.getTracks().forEach(t => t.stop());
      devices = (await navigator.mediaDevices.enumerateDevices())
        .filter(d => d.kind === "audioinput");
    }
  } catch { /* denied or unavailable — leave the default option only */ }
  for (const d of devices) {
    const o = document.createElement("option");
    o.value = d.deviceId;
    o.textContent = d.label || d.deviceId.slice(0, 12);
    if (/aec|echo|cancel/i.test(o.textContent)) o.textContent += "  ✅ echo-cancelled";
    sel.appendChild(o);
  }
  const want = voiceSettings.mic_device_id;
  sel.value = want && devices.some(d => d.deviceId === want) ? want : "";
  if (want && sel.value !== want)                       // saved device is gone
    sel.insertAdjacentHTML("beforeend",
      `<option value="" disabled>(saved device "${voiceSettings.mic_device_label}" not found)</option>`);
}

// Warn at the moment of choosing, not only after she starts answering herself.
function checkDuplexSanity() {
  const box = $("#vc-duplex-warn");
  if (!box) return;
  const on = $("#vc-duplex").checked;
  const label = ($("#vc-mic").selectedOptions[0] || {}).textContent || "";
  const cancelled = /aec|echo|cancel/i.test(label);
  if (on && !cancelled) {
    box.innerHTML = "⚠ Interruption is on but the selected microphone is not an " +
      "echo-cancelled one. On <b>headphones</b> that is fine. On <b>speakers</b> she " +
      "will hear herself and reply to her own voice — run " +
      "<code>scripts/setup-aec.sh --persist</code> and pick <code>aec_source</code>.";
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}
// The sleep phrase and timeout only mean anything in continuous mode.
function syncWakeMode() {
  const box = $("#vc-continuous");
  if (box) box.hidden = $("#vc-wakemode").value !== "continuous";
}
$("#vc-wakemode").addEventListener("change", syncWakeMode);
$("#vc-sleepafter").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  $("#vc-sleepafter-val").textContent = v ? v + " s" : "never";
});
$("#vc-duplex").addEventListener("change", checkDuplexSanity);
$("#vc-mic").addEventListener("change", checkDuplexSanity);

$("#vc-mic-help").addEventListener("click", (e) => {
  e.preventDefault();
  const g = $("#vc-mic-guide");
  g.hidden = !g.hidden;
  if (g.innerHTML) return;
  // Measured on the reference box: raw mic hears her at +26 dB over the noise
  // floor, this AEC source at -39 dB. Level tricks cannot bridge that; a real
  // canceller can.
  g.innerHTML = `
    <p><b>Interrupting her over speakers</b> needs an echo-cancelling capture source,
    otherwise the mic hears her far louder than it hears you.</p>
    <p>On PipeWire (Fedora / Bazzite / most modern Linux), create one:</p>
    <pre>pactl load-module module-echo-cancel \
  source_name=aec_source sink_name=aec_sink \
  aec_method=webrtc</pre>
    <p>Then pick <code>aec_source</code> above <b>and play audio through
    <code>aec_sink</code></b> — the canceller needs to hear what is being played to
    subtract it. Easiest is to set <code>aec_sink</code> as your default output.</p>
    <p>To undo: <code>pactl unload-module module-echo-cancel</code>.
    On <b>headphones</b> you need none of this — just tick the box.</p>`;
});

function hfTick() {
  if (!hfActive) return;
  const data = new Uint8Array(hfAnalyser.frequencyBinCount);
  hfAnalyser.getByteTimeDomainData(data);
  let sum = 0; for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
  const rms = Math.sqrt(sum / data.length);
  const now = performance.now();
  const base = voiceSettings.vad_threshold ?? 0.015;
  // Peak level once a second. Without this, "nothing happens when I talk" is
  // indistinguishable between a dead mic, a suspended context and too high a gate.
  hfPeak = Math.max(hfPeak, rms);
  if (now - hfPeakAt > 1000) {
    if (hfPeak > 0.002)
      console.log(`[mic] peak ${hfPeak.toFixed(4)} / gate ${base}` +
                  (hfPeak > base ? " OPEN" : " (below gate)"));
    else console.log("[mic] silence — analyser reading 0");
    hfPeak = 0; hfPeakAt = now;
  }

  if (!hfSpeaking) {
    // Phase 5. Half-duplex (default) refuses to listen while she speaks: no
    // self-hearing, but no barge-in either. Full duplex lifts that gate — only
    // correct when her voice cannot reach the mic at all, i.e. headphones or a
    // system echo canceller. Measured on the reference box, the raw webcam mic
    // hears her at +26 dB over the floor while a PipeWire WebRTC AEC source sits
    // at -39 dB, which is why this is a hard switch and not another level knob:
    // no threshold can separate a signal that dominates by 26 dB.
    const selfActive = (!!ttsSource || now < ttsHangoverUntil) && !voiceSettings.full_duplex;
    if (!selfActive && rms > base) {
      hfStartUtterance();
      hfSpeaking = true; hfSilenceStart = 0;
      // Barge-in: speaking over her cuts the reply, exactly as push-to-talk does.
      if (ttsSource || state.streaming) { stopSpeaking(); abortChat(); }
    }
  } else {
    // Already capturing your speech: track end-of-turn with the plain threshold
    // (she's muted now, so no echo to fight).
    if (rms > base) hfSilenceStart = 0;
    else if (!hfSilenceStart) hfSilenceStart = now;
    else if (now - hfSilenceStart > (voiceSettings.vad_silence_ms ?? 800)) {
      hfSpeaking = false; hfSilenceStart = 0;
      hfStopUtterance();
    }
  }
  hfRAF = requestAnimationFrame(hfTick);
}

let hfLastError = "";
let hfPeak = 0, hfPeakAt = 0, hfWakeTimer = null;

// ---- PCM capture ----------------------------------------------------------
// Not MediaRecorder. WebKitGTK's implementation constructs fine, reports
// state="recording" and advertises audio/mp4, then never fires ondataavailable
// -- measured: 19 utterances, 0 bytes each. Since the desktop shell IS
// WebKitGTK, recording has to come from somewhere that actually works.
//
// The AudioContext is already open for the VAD, so tapping raw PCM off it costs
// one node and removes every codec question. /api/stt takes WAV, which is what
// Phase 1 was originally verified with.
//
// ScriptProcessorNode is deprecated in favour of AudioWorklet, but it needs no
// separate module file and is supported everywhere including here; the buffer is
// small and this only runs while the mic is on.
let hfProc = null, hfPcm = [], hfCapturing = false, hfPre = [];
const HF_PRE_ROLL = 8;          // ~0.7 s of 4096-frame blocks kept before the gate

function hfWav(chunks, rate) {
  let n = 0;
  for (const c of chunks) n += c.length;
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const c of chunks)
    for (let i = 0; i < c.length; i++, o += 2) {
      const x = Math.max(-1, Math.min(1, c[i]));
      v.setInt16(o, x < 0 ? x * 0x8000 : x * 0x7fff, true);
    }
  return new Blob([buf], { type: "audio/wav" });
}

async function engageHandsFree() {
  if (hfActive) return true;
  hfLastError = "";
  try {
    const a = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    // Open the default device first. enumerateDevices() returns blank labels
    // until a getUserMedia grant exists, so choosing by label before this point
    // is choosing between empty strings -- which silently fell back to the raw
    // microphone and defeated the whole echo-cancelled setup.
    hfStream = await navigator.mediaDevices.getUserMedia({ audio: a });
    const want = await pickMicDeviceId();
    const open = (hfStream.getAudioTracks()[0] || {}).getSettings
      ? hfStream.getAudioTracks()[0].getSettings().deviceId : null;
    if (want && want !== open) {
      try {
        const better = await navigator.mediaDevices.getUserMedia(
          { audio: { ...a, deviceId: { exact: want } } });
        hfStream.getTracks().forEach(t => t.stop());     // release the first one
        hfStream = better;
      } catch (e) {
        console.warn("[mic] could not switch device, keeping the default:", e && e.name);
      }
    }
  } catch (e) {
    hfLastError = (e && (e.name + ": " + e.message)) || String(e);
    return false;
  }
  hfCtx = new (window.AudioContext || window.webkitAudioContext)();
  // A context created outside a user gesture starts SUSPENDED, and a suspended
  // AnalyserNode returns silence -- so the VAD saw rms 0 forever and no
  // utterance was ever captured, while the mic stream sat open and looked fine.
  // Resuming is separate from the TTS context; they are two AudioContexts.
  if (hfCtx.state === "suspended") {
    try { await hfCtx.resume(); } catch (e) { /* reported below */ }
  }
  if (hfCtx.state !== "running") {
    hfLastError = "audio context " + hfCtx.state + " (needs a user gesture)";
    try { hfStream.getTracks().forEach(t => t.stop()); } catch {}
    hfStream = null;
    return false;                       // fail loudly rather than listen to silence
  }
  hfAnalyser = hfCtx.createAnalyser(); hfAnalyser.fftSize = 512;
  const src = hfCtx.createMediaStreamSource(hfStream);
  src.connect(hfAnalyser);
  hfProc = hfCtx.createScriptProcessor(4096, 1, 1);
  hfPcm = []; hfPre = []; hfCapturing = false;
  hfProc.onaudioprocess = (e) => {
    const block = new Float32Array(e.inputBuffer.getChannelData(0));   // copy: reused
    if (hfCapturing) { hfPcm.push(block); return; }
    // Rolling pre-roll. The gate only opens once speech is already loud enough,
    // so without this the front of the utterance is clipped -- and the wake word
    // is the very first thing said, which is exactly the part that was lost.
    hfPre.push(block);
    if (hfPre.length > HF_PRE_ROLL) hfPre.shift();
  };
  src.connect(hfProc);
  // A ScriptProcessor only runs while connected to a destination. Route it
  // through a silent gain so nothing is played back.
  const mute = hfCtx.createGain(); mute.gain.value = 0;
  hfProc.connect(mute); mute.connect(hfCtx.destination);
  hfActive = true; hfSpeaking = false; hfSilenceStart = 0;
  // Expire on a timer rather than on the next utterance. Checking only when
  // someone speaks means she stays awake indefinitely in an empty room, which
  // is exactly the case the timeout exists for.
  if (!hfWakeTimer) hfWakeTimer = setInterval(() => {
    if (awake && Date.now() > awakeUntil) setAwake(false, "idle timeout");
  }, 2000);
  console.log("[mic] hands-free engaged; ctx=" + hfCtx.state +
              " device=" + ((hfStream.getAudioTracks()[0] || {}).label || "?"));
  hfTick();
  return true;
}

function disengageHandsFree() {
  hfActive = false; hfSpeaking = false; hfCapturing = false;
  if (hfWakeTimer) { clearInterval(hfWakeTimer); hfWakeTimer = null; }
  setAwake(false, "mic off");
  if (hfRAF) { cancelAnimationFrame(hfRAF); hfRAF = null; }
  // The ScriptProcessor keeps firing until it is disconnected, so dropping the
  // reference alone would leave an audio callback running against a closed
  // context every 4096 frames.
  if (hfProc) {
    hfProc.onaudioprocess = null;
    try { hfProc.disconnect(); } catch {}
    hfProc = null;
  }
  hfPcm = []; hfPre = [];
  $("#hf-btn").classList.remove("listening");
  if (hfStream) { hfStream.getTracks().forEach(t => t.stop()); hfStream = null; }
  if (hfCtx) { try { hfCtx.close(); } catch {} hfCtx = null; }
}

async function persistSettings(patch) {
  Object.assign(voiceSettings, patch);
  try {
    await api("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  } catch {}
}

async function setHandsFree(on) {
  $("#hf-btn").classList.toggle("active", on);
  if (on) {
    if (!await engageHandsFree()) {
      // alert() is invisible in the desktop shell (WebKitGTK drops script
      // dialogs when nothing handles them), so the real reason has to go
      // somewhere it can be read.
      console.error("[mic] hands-free failed:", hfLastError || "(no detail)");
      showToast("Microphone unavailable: " + (hfLastError || "permission denied"));
      $("#hf-btn").classList.remove("active");
      on = false;
    }
  } else {
    disengageHandsFree();
  }
  await persistSettings({ hands_free: on });
}

$("#hf-btn").addEventListener("click", () => setHandsFree(!voiceSettings.hands_free));

// voice settings modal (wake word + VAD tuning)
$("#voicecfg-btn").addEventListener("click", async () => {
  $("#vc-wake").value = voiceSettings.wake_word || "";
  $("#vc-thresh").value = voiceSettings.vad_threshold ?? 0.015;
  $("#vc-thresh-val").textContent = Number(voiceSettings.vad_threshold ?? 0.015).toFixed(3);
  $("#vc-silence").value = voiceSettings.vad_silence_ms ?? 800;
  $("#vc-silence-val").textContent = (voiceSettings.vad_silence_ms ?? 800) + " ms";
  $("#vc-wakemode").value = voiceSettings.wake_mode || "phrase";
  $("#vc-sleep").value = voiceSettings.sleep_phrase ?? "that will be all";
  $("#vc-farewell").value = voiceSettings.farewell ?? "Goodbye.";
  const slp = voiceSettings.sleep_after_s ?? 180;
  $("#vc-sleepafter").value = slp;
  $("#vc-sleepafter-val").textContent = slp ? slp + " s" : "never";
  syncWakeMode();
  $("#vc-duplex").checked = !!voiceSettings.full_duplex;
  await fillMicSelect();
  checkDuplexSanity();
  await fillDesktopCardSelect();
  $("#vc-nothink").checked = !!voiceSettings.disable_thinking;
  const sa = voiceSettings.summarize_after ?? 24, kr = voiceSettings.keep_recent ?? 12,
        sc = voiceSettings.summary_max_tokens ?? 400;
  $("#vc-sumafter").value = sa;      $("#vc-sumafter-val").textContent = sa + " msgs";
  $("#vc-keeprecent").value = kr;    $("#vc-keeprecent-val").textContent = kr + " msgs";
  $("#vc-sumcap").value = sc;        $("#vc-sumcap-val").textContent = sc + " tokens";
  const rk = voiceSettings.recall_top_k ?? 4;
  $("#vc-recall").checked = voiceSettings.recall_enabled !== false;
  $("#vc-autofacts").checked = voiceSettings.auto_facts_enabled !== false;
  const afn = voiceSettings.auto_facts_max_new ?? 3;
  $("#vc-autofactsn").value = afn; $("#vc-autofactsn-val").textContent = afn + " max";
  $("#vc-recallk").value = rk;       $("#vc-recallk-val").textContent = rk + " snippets";
  $("#voice-cfg").hidden = false;
});
$("#vc-sumafter").addEventListener("input", (e) => { $("#vc-sumafter-val").textContent = e.target.value + " msgs"; });
$("#vc-keeprecent").addEventListener("input", (e) => { $("#vc-keeprecent-val").textContent = e.target.value + " msgs"; });
$("#vc-sumcap").addEventListener("input", (e) => { $("#vc-sumcap-val").textContent = e.target.value + " tokens"; });
$("#vc-recallk").addEventListener("input", (e) => { $("#vc-recallk-val").textContent = e.target.value + " snippets"; });
$("#vc-autofactsn").addEventListener("input", (e) => { $("#vc-autofactsn-val").textContent = e.target.value + " max"; });
$("#vc-close").addEventListener("click", () => { $("#voice-cfg").hidden = true; });
$("#voice-cfg").addEventListener("click", (e) => { if (e.target.id === "voice-cfg") $("#voice-cfg").hidden = true; });

// ---- Memory (this chat: pinned + rolling summary; long-term: per char x persona)
// Long-term memory is scoped per (character, persona) like the relationship, so the
// tab is hidden for sessions that have no character, and for scenario sessions —
// those run on a session-scoped premise the main timeline would contradict.
let ltMem = null;                                             // loaded CharacterMemory, or null

function addPinnedRow(text = "") {
  const li = document.createElement("li");
  li.innerHTML = `<input class="field mem-fact" type="text" value="" placeholder="e.g. Lives on the starship Meridian; afraid of deep water">` +
    `<button class="btn-mini danger mem-del" title="Remove">✕</button>`;
  li.querySelector(".mem-fact").value = text;                 // set via property so quotes/HTML are safe
  li.querySelector(".mem-del").onclick = () => li.remove();
  $("#mem-pinned").appendChild(li);
}
function addLtFactRow(fact = {}) {
  const li = document.createElement("li");
  const auto = fact.origin === "auto";
  li.innerHTML = `<input class="field mem-fact" type="text" value="" placeholder="e.g. Her sister's name is Nadia; she hates being called 'kid'">` +
    (auto ? `<span class="mem-origin" title="Written automatically from a chat — edit or remove it">auto</span>` : "") +
    `<button class="btn-mini danger mem-del" title="Remove">✕</button>`;
  const input = li.querySelector(".mem-fact");
  input.value = fact.text || "";
  input.dataset.factId = fact.id || "";                       // round-trips origin/created_at server-side
  li.querySelector(".mem-del").onclick = () => li.remove();
  $("#mem-lt-facts").appendChild(li);
}
function renderMemory() {
  const mem = (state.session && state.session.memory) || { pinned: [], summary: "" };
  $("#mem-pinned").innerHTML = "";
  (mem.pinned || []).forEach(addPinnedRow);
  $("#mem-summary").value = mem.summary || "";
}
function renderLongterm() {
  $("#mem-lt-facts").innerHTML = "";
  ((ltMem && ltMem.facts) || []).forEach(addLtFactRow);
  $("#mem-lt-summary").value = (ltMem && ltMem.summary) || "";
}
function memTab(which) {
  const long = which === "long";
  $("#mem-pane-session").hidden = long;
  $("#mem-pane-long").hidden = !long;
  $("#mem-tab-session").classList.toggle("active", !long);
  $("#mem-tab-long").classList.toggle("active", long);
}
function ltPairing() {                                        // {cid, pid} or null if not applicable
  const s = state.session;
  if (!s || !s.character_id || s.relationship) return null;   // no card, or a scenario session
  const pid = s.persona_id || (state.persona && state.persona.id) || "";
  return pid ? { cid: s.character_id, pid } : null;
}
$("#mem-btn").addEventListener("click", async () => {
  if (!state.session) return;
  renderMemory();
  memTab("session");
  ltMem = null;
  const pair = ltPairing();
  $("#mem-tab-long").hidden = !pair;
  $("#memory").hidden = false;
  if (!pair) return;
  try {
    ltMem = await api(`/memories?character_id=${encodeURIComponent(pair.cid)}&persona_id=${encodeURIComponent(pair.pid)}`);
    renderLongterm();
  } catch (e) {
    $("#mem-tab-long").hidden = true;                         // couldn't load it -> don't offer to save over it
  }
});
$("#mem-tab-session").addEventListener("click", () => memTab("session"));
$("#mem-tab-long").addEventListener("click", () => memTab("long"));
$("#mem-close").addEventListener("click", () => { $("#memory").hidden = true; });
$("#memory").addEventListener("click", (e) => { if (e.target.id === "memory") $("#memory").hidden = true; });
$("#mem-add").addEventListener("click", () => addPinnedRow(""));
$("#mem-lt-add").addEventListener("click", () => addLtFactRow({}));
$("#mem-save").addEventListener("click", async () => {
  if (!state.session) return;
  const pinned = [...document.querySelectorAll("#mem-pinned .mem-fact")]
    .map(i => i.value.trim()).filter(Boolean);
  const summary = $("#mem-summary").value.trim();
  const mem = await api(`/sessions/${state.session.id}/memory`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pinned, summary }),
  });
  state.session.memory = mem;                                 // keep local state in sync
  // Only write long-term if it actually loaded — otherwise a failed GET would
  // let an empty form overwrite real remembered history.
  const pair = ltPairing();
  if (ltMem && pair) {
    const facts = [...document.querySelectorAll("#mem-lt-facts .mem-fact")]
      .map(i => ({ id: i.dataset.factId || "", text: i.value.trim() }))
      .filter(f => f.text);
    ltMem = await api("/memories", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: pair.cid, persona_id: pair.pid,
                             facts, summary: $("#mem-lt-summary").value.trim() }),
    });
  }
  $("#memory").hidden = true;
});

// ---- Idle sweep: fold a finished conversation into long-term memory --------
// Fires once per quiet period, not per message. The endpoint is idempotent, so a
// redundant call is harmless; the timer only exists to avoid firing mid-scene.
const SWEEP_IDLE_MS = 5 * 60 * 1000;
let sweepTimer = null;
function scheduleSweep() {
  clearTimeout(sweepTimer);
  const s = state.session;
  if (!s || !s.character_id || s.relationship) return;        // same scoping as the Long-term tab
  const id = s.id;
  sweepTimer = setTimeout(() => {
    api(`/sessions/${id}/sweep`, { method: "POST" }).catch(() => {});
  }, SWEEP_IDLE_MS);
}
$("#vc-thresh").addEventListener("input", (e) => { $("#vc-thresh-val").textContent = Number(e.target.value).toFixed(3); });
$("#vc-silence").addEventListener("input", (e) => { $("#vc-silence-val").textContent = e.target.value + " ms"; });
$("#vc-save").addEventListener("click", async () => {
  await persistSettings({
    wake_word: $("#vc-wake").value.trim(),
    vad_threshold: Number($("#vc-thresh").value),
    vad_silence_ms: Number($("#vc-silence").value),
    wake_mode: $("#vc-wakemode").value,
    sleep_phrase: $("#vc-sleep").value.trim(),
    farewell: $("#vc-farewell").value.trim(),
    sleep_after_s: Number($("#vc-sleepafter").value),
    full_duplex: $("#vc-duplex").checked,
    mic_device_id: $("#vc-mic").value,
    mic_device_label: ($("#vc-mic").selectedOptions[0] || {}).textContent || "",
    desktop_card_id: $("#vc-desktop-card").value,
    disable_thinking: $("#vc-nothink").checked,
    summarize_after: Number($("#vc-sumafter").value),
    keep_recent: Number($("#vc-keeprecent").value),
    summary_max_tokens: Number($("#vc-sumcap").value),
    recall_enabled: $("#vc-recall").checked,
    recall_top_k: Number($("#vc-recallk").value),
    auto_facts_enabled: $("#vc-autofacts").checked,
    auto_facts_max_new: Number($("#vc-autofactsn").value),
  });
  $("#voice-cfg").hidden = true;
});

async function loadSettings() {
  try { voiceSettings = Object.assign(voiceSettings, await api("/settings")); } catch {}
  if (voiceSettings.hands_free) {
    $("#hf-btn").classList.add("active");
    // Try to open the mic straight away, and only fall back to waiting for a
    // click if that is refused.
    //
    // This used to wait for a pointerdown unconditionally, which is correct in a
    // browser tab -- getUserMedia there is gated on a user gesture -- but wrong
    // in the desktop shell, where the avatar is click-through and every control
    // lives in the tray. Nobody ever clicks the page, so the mic was never
    // opened: the button showed active, the setting said on, and speaking did
    // nothing. Attempting first costs one rejected promise in the browser case.
    (async () => {
      if (hfActive || await engageHandsFree()) return;
      console.warn("[mic] deferred until a click:", hfLastError || "(gesture required)");
      window.addEventListener("pointerdown", async function resume() {
        if (voiceSettings.hands_free && !hfActive && !await engageHandsFree()) {
          $("#hf-btn").classList.remove("active");
          showToast("Microphone unavailable: " + (hfLastError || "permission denied"));
        }
      }, { once: true });
    })();
  }
}

// ---- help modal -----------------------------------------------------------
$("#help-btn").addEventListener("click", () => { $("#help").hidden = false; });
$("#help-close").addEventListener("click", () => { $("#help").hidden = true; });
$("#help").addEventListener("click", (e) => {
  if (e.target.id === "help") { $("#help").hidden = true; return; }   // backdrop
  const btn = e.target.closest(".help-copy");
  if (!btn) return;
  const pre = btn.closest(".help-example").querySelector("pre");
  navigator.clipboard.writeText(pre.textContent).then(() => {
    const orig = btn.textContent;
    btn.textContent = "Copied ✓"; btn.classList.add("help-copied");
    setTimeout(() => { btn.textContent = orig; btn.classList.remove("help-copied"); }, 1200);
  }).catch(() => {});
});

// ---- auth -----------------------------------------------------------------
let currentUser = null;

function showLogin() { $("#login").hidden = false; setTimeout(() => $("#login-user").focus(), 0); }
function hideLogin() { $("#login").hidden = true; }

async function onAuthed(user) {
  currentUser = user;
  hideLogin();
  $("#user-name").textContent = `${user.username} · ${user.role}`;
  $("#users-btn").hidden = user.role !== "admin";
  await init();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("#login-user").value.trim(), password = $("#login-pass").value;
  $("#login-err").textContent = "";
  try {
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }) });
    if (!r.ok) { $("#login-err").textContent = "Invalid username or password."; return; }
    $("#login-pass").value = "";
    await onAuthed(await r.json());
  } catch { $("#login-err").textContent = "Login failed — is the server running?"; }
});

$("#logout-btn").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST" }); } catch {}
  location.reload();
});

// ---- users admin (admin only) ---------------------------------------------
$("#users-btn").addEventListener("click", async () => { $("#users-err").textContent = ""; $("#users").hidden = false; await renderUsers(); });
$("#users-close").addEventListener("click", () => { $("#users").hidden = true; });
$("#users").addEventListener("click", (e) => { if (e.target.id === "users") $("#users").hidden = true; });
$("#nu-add").addEventListener("click", addUser);

async function renderUsers() {
  const ul = $("#users-list"); ul.innerHTML = "";
  let users = [];
  try { users = await api("/users"); } catch { return; }
  for (const u of users) {
    const li = document.createElement("li");
    const isSelf = currentUser && u.username === currentUser.username;
    li.innerHTML = `<span class="u-name">${u.username}</span><span class="u-role">${u.role}</span>` +
      `<button class="btn-mini u-pw" title="Reset password">Reset PW</button>` +
      (isSelf ? "" : `<button class="btn-mini danger u-del" title="Delete user">✕</button>`);
    li.querySelector(".u-pw").onclick = () => resetUserPw(u);
    const del = li.querySelector(".u-del");
    if (del) del.onclick = () => delUser(u);
    ul.appendChild(li);
  }
}

async function addUser() {
  const username = $("#nu-name").value.trim(), password = $("#nu-pass").value, role = $("#nu-role").value;
  $("#users-err").textContent = "";
  try {
    await api("/users", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }) });
    $("#nu-name").value = ""; $("#nu-pass").value = "";
    await renderUsers();
  } catch (e) { $("#users-err").textContent = e.detail || "Could not add user."; }
}

async function delUser(u) {
  if (!confirm(`Delete user "${u.username}" and their entire private library? This cannot be undone.`)) return;
  try { await api("/users/" + u.id, { method: "DELETE" }); await renderUsers(); }
  catch (e) { $("#users-err").textContent = e.detail || "Delete failed."; }
}

async function resetUserPw(u) {
  const pw = prompt(`New password for "${u.username}" (min 4 chars):`);
  if (!pw) return;
  try { await api(`/users/${u.id}/password`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }) }); alert("Password updated."); }
  catch (e) { alert(e.detail || "Reset failed."); }
}

// ---- boot -----------------------------------------------------------------
async function init() {
  await Promise.all([loadCards(), loadPersonas(), loadPresets(), refreshStatus(), loadSettings()]);
  await loadSessions();
  if (!init._statusTimer) init._statusTimer = setInterval(refreshStatus, 15000);
}

(async function boot() {
  let user = null;
  try { user = (await (await fetch("/api/me")).json()).user; } catch {}
  if (user) await onAuthed(user); else showLogin();
})();

// ===========================================================================
// Phase 6 — desktop mode HUD.
//
// Deliberately a thin skin over the controls that already exist: it clicks
// #tts-btn and #hf-btn rather than reimplementing the voice loop, so desktop
// mode cannot drift out of sync with the main window. Everything below is
// inert unless the body carries .desktop-mode.
// ===========================================================================
(function desktopMode() {
  if (!document.body.classList.contains("desktop-mode")) return;

  const cap = $("#dt-caption"), st = $("#dt-state");
  const btnMic = $("#dt-mic"), btnTts = $("#dt-tts");
  let capTimer = null;

  // Proxy the real buttons so behaviour, persistence and error handling are
  // shared with the full window.
  btnTts.addEventListener("click", () => $("#tts-btn").click());
  btnMic.addEventListener("click", () => $("#hf-btn").click());

  function caption(text, holdMs) {
    clearTimeout(capTimer);
    if (!text) { cap.classList.remove("show"); return; }
    cap.textContent = text;
    cap.classList.add("show");
    if (holdMs) capTimer = setTimeout(() => cap.classList.remove("show"), holdMs);
  }

  // Reflect state rather than track it: the source of truth stays in app.js.
  setInterval(() => {
    btnTts.textContent = ttsEnabled ? "🔊" : "🔇";
    btnTts.classList.toggle("active", ttsEnabled);
    btnMic.classList.toggle("active", !!voiceSettings.hands_free);
    st.textContent = state.streaming ? "thinking…"
                   : hfSpeaking      ? "listening…"
                   : ttsSource       ? "speaking…"
                   : voiceSettings.hands_free ? "ready" : "idle";
  }, 250);

  // Her latest line, briefly. A face alone gives you no way to catch a missed
  // word, but a permanent transcript is clutter on a desktop. Read from the real
  // message list as it streams, then fade -- no hooks into the chat code.
  const bubbleObserver = new MutationObserver(() => {
    const msgs = $("#messages");
    const last = msgs && msgs.lastElementChild;
    if (!last) return;
    const txt = (last.textContent || "").trim();
    if (txt && txt !== "…") caption(txt.slice(-320), state.streaming ? 0 : 9000);
  });
  const msgs = $("#messages");
  if (msgs) bubbleObserver.observe(msgs, { childList: true, subtree: true, characterData: true });

  // The shell just opens a URL, so the character has to be chosen here rather
  // than by the user at launch. Order: ?card= override, then the configured
  // desktop character, then the most recent chat as a last resort.
  async function pickCharacter() {
    if (state.session) return;
    const want = new URLSearchParams(location.search).get("card")
              || voiceSettings.desktop_card_id || "";
    const sessions = state.sessions || [];
    if (want) {
      // Continue her most recent conversation rather than starting cold every
      // launch -- a desktop companion that forgets on every boot is worse.
      const mine = sessions.filter(s => s.character_id === want);
      if (mine.length) return openSession(mine[0].id);
      const card = (state.cards || []).find(c => c.id === want);
      if (card) {
        try {
          const s = await api("/sessions", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ character_id: want }),
          });
          await loadSessions();
          return openSession(s.id);
        } catch (e) { console.warn("[desktop] could not start a chat:", e); }
      }
      console.warn("[desktop] configured character not found; falling back");
    }
    if (sessions.length) openSession(sessions[0].id);
  }
  // Voice on by default here. In the browser 🔊 is opt-in because you are sitting
  // in front of a transcript you can read; a face in the corner of the screen that
  // types at you silently is just a worse chat window. The shell allows autoplay
  // (media-playback-requires-user-gesture=false), so the AudioContext can start
  // without a click -- in a plain browser tab it may stay suspended until you
  // interact, which is a browser policy we cannot and should not defeat.
  async function enableVoice() {
    if (ttsEnabled) return;
    $("#tts-btn").click();                       // same path as the button
    try {
      const ctx = ensureAudioCtx();
      if (ctx && ctx.state === "suspended") await ctx.resume();
    } catch (e) { console.warn("[desktop] audio context blocked:", e); }
  }

  window.addEventListener("load", () => setTimeout(pickCharacter, 900));
  window.addEventListener("load", () => setTimeout(enableVoice, 1200));

  // Hooks for the native shell's tray menu. The page is the authenticated side --
  // it holds the session cookie -- so the shell asks it for the character list and
  // tells it to switch, rather than trying to reach the API itself.
  // Reads through to the API rather than returning the cached state.cards: the
  // shell polls this every 30s precisely so a character added in the main window
  // turns up without a restart, and a stale cache would defeat that. Kept cheap
  // by only re-fetching when the count changed is not possible here (the count
  // is what we are checking), so this is one small GET twice a minute.
  window.__desktopCharacters = () => {
    api("/cards").then(cards => { state.cards = cards; }).catch(() => {});
    return (state.cards || []).map(c => ({
      id: c.id, name: c.name, avatar: !!c.has_vrm,
      current: !!(state.session && state.session.character_id === c.id),
    }));
  };

  // Listening is a persisted setting, not something desktop mode turns on for
  // you -- an assistant that starts recording because you opened it would be
  // the wrong default. Exposed here so the tray can toggle it, since the HUD
  // only appears on hover inside the click-through strip.
  window.__desktopListening = () => ({
    on: !!voiceSettings.hands_free,
    wake: activeWakeWord(),          // whoever is loaded, not the global default
    sleep: activeSleepPhrase(),
    mode: voiceSettings.wake_mode || "phrase",
    awake: !!awake,
    duplex: !!voiceSettings.full_duplex,
  });

  window.__desktopSetListening = async (on) => {
    await setHandsFree(!!on);
    return !!voiceSettings.hands_free;
  };

  window.__desktopSetCharacter = async (id) => {
    const card = (state.cards || []).find(c => c.id === id);
    if (!card) return false;
    // Persist first, so a restart comes back as whoever was last chosen.
    voiceSettings.desktop_card_id = id;
    persistSettings({ desktop_card_id: id });
    const mine = (state.sessions || []).filter(s => s.character_id === id);
    if (mine.length) { await openSession(mine[0].id); return true; }
    const s = await api("/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character_id: id }),
    });
    await loadSessions();
    await openSession(s.id);
    return true;
  };
})();
