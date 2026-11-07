// ===========================================================================
// voice.js — the new page's voice island: read-aloud, lip-sync, expression
// beats, push-to-talk, hands-free (VAD + wake word), barge-in, and the 3D
// avatar's loader.
//
// It is a port of the voice loop in app.js (the classic page), with the same
// measured behaviour and the reasons kept beside the code. What changed is only
// the seam: app.js reads its own globals (state, currentCard(), send()), while
// this file is driven by the Python chat view through one object and answers
// with events, so it never reaches into the page.
//
//   Python  -> window.PantheonVoice.attach(card, settings) / detach()
//              .beginReply() .feed(text, final) .expr(label) .flushExpr()
//              .setStreaming(on) .readAloud(text) .stop() .speaker(on)
//              .pttStart() .pttStop() .handsFree(on) .setSettings(patch)
//              .mood(mood, valence, stage) .mics(ask) .state()
//   this    -> window "pvoice:send"    {text}          a transcript to send
//              window "pvoice:abort"   {}              stop the reply streaming
//              window "pvoice:expr"    {label}         show this sprite now
//              window "pvoice:state"   {...state()}    buttons should repaint
//              window "pvoice:persist" {patch}         save these settings
//              window "pvoice:toast"   {msg, kind}
//              window "pvoice:ready"   {}              loaded (once)
//
// The avatar (vtuber.mjs) is loaded on demand — only when a card on the 3D tier
// is opened — and talks over its own avatar:* events exactly as on the classic
// page. Its canvas is one element that moves into whichever chat is open,
// because the renderer binds to it once at boot.
// ===========================================================================
(() => {
  if (window.PantheonVoice) return;

  // ---- plumbing --------------------------------------------------------------
  const emit = (name, detail) =>
    window.dispatchEvent(new CustomEvent("pvoice:" + name, { detail: detail || {} }));
  const toast = (msg, kind) => emit("toast", { msg, kind: kind || "warning" });

  function csrf() {
    const parts = document.cookie.split(";").map(s => s.trim());
    const hit = parts.find(p => /csrf/i.test(p.split("=")[0]) && /pantheon/i.test(p.split("=")[0]))
             || parts.find(p => /csrf/i.test(p.split("=")[0]));
    return hit ? decodeURIComponent(hit.slice(hit.indexOf("=") + 1)) : "";
  }

  let card = null;                  // {id, name, mode, tts_voice, tts_speed, wake_word, sleep_phrase, farewell, has_vrm, vrm}
  let attached = false;             // a chat is open and listening for our events
  let streaming = false;            // a reply is streaming (the chat view tells us)
  let settings = { hands_free: false, wake_word: "", vad_silence_ms: 800, vad_threshold: 0.015,
    full_duplex: false, mic_device_id: "", mic_device_label: "",
    wake_mode: "phrase", sleep_phrase: "that will be all", sleep_after_s: 180, farewell: "Goodbye." };

  // Read-aloud is a per-viewer preference, so it lives in this browser.
  let ttsEnabled = false;
  try { ttsEnabled = localStorage.getItem("pantheon.speaker") === "1"; } catch {}

  function state() {
    return { speaker: ttsEnabled, handsFree: !!settings.hands_free, hfActive, listening: hfCapturing,
             awake, recording, busy: pttBusy, speaking: !!ttsSource, avatar: avatarActive };
  }
  const repaint = () => { syncEngine(); emit("state", state()); };

  // ---- on-demand Orpheus voice -------------------------------------------------------
  // The Orpheus model takes ~3 GB of VRAM, so it is loaded only while voice is on for a
  // card that uses it (speaker or hands-free), and released when that stops. Checked on
  // every repaint, which follows every state change. Turning off waits: a quick toggle
  // or switching chats should not unload and reload 3 GB. The service also unloads by
  // itself when idle, which covers a closed tab (services/orpheus.py).
  const ENGINE_OFF_DELAY = 20000;
  let engineOn = false, engineTimer = null, engineWarned = false;
  function syncEngine() {
    const want = attached && !!card && String(card.tts_voice || "").startsWith("orpheus:")
      && (ttsEnabled || !!settings.hands_free);
    clearTimeout(engineTimer); engineTimer = null;
    if (want === engineOn) return;
    engineTimer = setTimeout(() => {
      engineTimer = null; engineOn = want; postEngine(want);
    }, want ? 0 : ENGINE_OFF_DELAY);
  }
  async function postEngine(on) {
    try {
      const r = await fetch("/pmedia/tts/engine", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ on }) });
      const s = r.ok ? await r.json() : { state: "unavailable" };
      console.log("[voice] orpheus " + (on ? "load" : "release") + ": " + s.state);
      if (on && s.state === "unavailable" && !engineWarned) {
        engineWarned = true;
        toast("The Orpheus voice could not load (not enough GPU memory, or the service is " +
              "down) — speaking with a standard voice instead.", "warning");
      }
    } catch (e) { console.warn("[voice] orpheus engine call failed:", e); }
  }

  // ---- TTS --------------------------------------------------------------------
  let audioCtx = null, ttsSource = null, ttsLevel = 0, ttsHangoverUntil = 0, mouthRAF = null;
  let ttsRun = 0, speechQueue = [], speechConsuming = false, speechFedLen = 0;
  let firstPiece = true;            // the next segment opens a reply (see enqueueSpeech)
  const liveStreams = new Set();    // AbortControllers of streamed sentences in flight
  let genQueue = [], genBusy = false; // Orpheus sentences waiting to be generated, in order
  let exprPending = null, exprLive = null, forced = false;
  const spokenRecent = [];          // what she said lately, for the self-hearing guard
  let selfHeardCount = 0;

  const currentVoice = () => (card && card.tts_voice) || "af_sarah";
  // Kokoro takes pace as a multiplier, Piper inverts it onto length_scale — 1.0 is the voice's own.
  const currentSpeed = () => (card && card.tts_speed) || 1;
  const speakingOn = () => ttsEnabled || forced;

  function ensureAudioCtx() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  function portraitEl() { return document.getElementById("ch-portrait"); }

  function stopSpeaking() {
    ttsRun++;                                   // invalidate any in-flight queue
    speechQueue = []; speechConsuming = false; speechFedLen = 0;
    // Beats belong to the reply that was speaking. Called at the top of every reply too,
    // so this is also the per-reply reset: a repeated label still re-applies next time.
    exprPending = null; exprLive = null;
    firstPiece = true;
    for (const c of liveStreams) { try { c.abort(); } catch {} }
    liveStreams.clear();
    // Sentences that never started: mark them finished so a player waiting on one returns.
    for (const c of genQueue) { c.done = true; const w = c.wake; c.wake = null; if (w) w(); }
    genQueue = []; genBusy = false;
    try { if (ttsSource) ttsSource.stop(); } catch {}
    ttsSource = null; ttsLevel = 0;
    if (mouthRAF) { cancelAnimationFrame(mouthRAF); mouthRAF = null; }
    const img = portraitEl(); if (img) img.style.transform = "";
  }

  async function fetchTTS(sentence, voice, speed) {
    try {
      const r = await fetch("/pmedia/tts", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
        body: JSON.stringify({ text: sentence, voice: voice || currentVoice(), speed: speed || currentSpeed() }) });
      return r.ok ? await r.arrayBuffer() : null;
    } catch { return null; }
  }

  // ---- streamed Orpheus speech ------------------------------------------------------
  // Orpheus voices arrive as raw PCM (s16le, 24 kHz) while the server is still generating
  // them (/pmedia/tts/stream), so a sentence starts playing ~0.4 s in rather than after
  // all of it exists — 1.8–8 s for one sentence, measured. The request is read into memory
  // as it arrives, so a prefetched sentence keeps generating while the previous one plays.
  const usesStream = () => String(currentVoice()).startsWith("orpheus:");
  // Start playing once what is buffered covers the rest of the sentence at the rate audio
  // is arriving, so playback never catches up with generation. Orpheus runs ~1.5x real
  // time alone but 0.65–1.0x while the chat model is still writing the reply on the same
  // GPU; a fixed 0.3 s buffer stalled up to 48 times in one sentence there (measured).
  // With this rule: 0 stalls in 6 contended runs, starting 1.1–2.8 s into a 7–15 word
  // piece — the old whole-sentence wait was 3–8 s. Speech length is estimated from the
  // words (0.42 s/word, measured on Orpheus "tara").
  const PREBUFFER = 0.3;            // s, the floor
  const SEC_PER_WORD = 0.42;
  function readyToPlay(clip, have) {
    if (!clip.started || have < PREBUFFER) return false;
    const elapsed = (performance.now() - clip.t0) / 1000;
    const rate = have / Math.max(elapsed, 0.01);          // s of audio per s, so far
    if (rate >= 1) return true;
    const est = Math.max(1, (clip.text.match(/\S+/g) || []).length * SEC_PER_WORD);
    return have >= Math.max(0, est - have) * (1 / rate - 1) * 1.15;
  }
  function makeClip(sentence) {
    return { chunks: [], done: false, rate: 24000, wake: null, text: sentence,
             t0: Infinity, started: false };
  }
  const streamTTS = (sentence) => { const c = makeClip(sentence); startClip(c); return c; };

  // Each sentence is generated as soon as its text exists and the one before it has
  // finished generating — never waiting on playback. Fetching only one sentence ahead of
  // what was playing left Orpheus idle and turned any slowdown into a pause before the
  // next sentence; on a 195-word reply that cost 1.3–1.9 s of silence, pipelined 0 s.
  // One at a time and in order, so sentence 2 never competes with sentence 1 for the GPU.
  function queueGeneration(clip) {
    genQueue.push(clip);
    pumpGeneration();
  }
  async function pumpGeneration() {
    if (genBusy) return;
    const clip = genQueue.shift();
    if (!clip) return;
    genBusy = true;
    const run = ttsRun;
    await startClip(clip);
    if (run !== ttsRun) return;              // stopSpeaking reset the queue meanwhile
    genBusy = false;
    pumpGeneration();
  }

  function startClip(clip) {
    if (clip.started) return Promise.resolve();
    clip.started = true;
    clip.t0 = performance.now();
    const sentence = clip.text;
    const ctl = new AbortController();
    liveStreams.add(ctl);
    const notify = () => { const w = clip.wake; clip.wake = null; if (w) w(); };
    return (async () => {
      try {
        const r = await fetch("/pmedia/tts/stream", { method: "POST", credentials: "same-origin",
          signal: ctl.signal,
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
          body: JSON.stringify({ text: sentence, voice: currentVoice() }) });
        if (!r.ok || !r.body) throw new Error("status " + r.status);
        clip.rate = Number(r.headers.get("X-Sample-Rate")) || 24000;
        const reader = r.body.getReader();
        let carry = null;                       // an odd byte split across reads
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          let b = value;
          if (carry) { const j = new Uint8Array(carry.length + b.length); j.set(carry); j.set(b, carry.length); b = j; carry = null; }
          const even = b.length & ~1;
          if (even < b.length) carry = b.slice(even);
          if (!even) continue;
          const v = new DataView(b.buffer, b.byteOffset, even), f = new Float32Array(even / 2);
          for (let i = 0; i < f.length; i++) f[i] = v.getInt16(i * 2, true) / 32768;
          clip.chunks.push(f);
          notify();
        }
      } catch (e) { if (e.name !== "AbortError") console.warn("[tts] stream failed:", e); }
      liveStreams.delete(ctl);
      clip.done = true;
      notify();
    })();
  }

  // Lip-sync off the playback level — shared by whole clips and streams.
  function startMouth(analyser) {
    const data = new Uint8Array(analyser.frequencyBinCount);
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0; for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
      const amp = Math.sqrt(sum / data.length);
      ttsLevel = amp;
      const img = portraitEl();
      if (img && !img.hidden) img.style.transform = `scaleY(${1 + Math.min(amp * 0.5, 0.06)})`;
      if (avatarActive) window.dispatchEvent(new CustomEvent("avatar:speak", { detail: { amp } }));
      mouthRAF = requestAnimationFrame(tick);
    };
    tick();
  }
  function endMouth() {
    if (mouthRAF) { cancelAnimationFrame(mouthRAF); mouthRAF = null; }
    const img = portraitEl(); if (img) img.style.transform = "";
    // Tail: ignore reverb/echo for a moment after she stops.
    ttsSource = null; ttsLevel = 0; ttsHangoverUntil = performance.now() + 400;
    repaint();
  }

  // Schedules the chunks back to back as they arrive. `ttsSource` is a handle whose
  // stop() silences everything scheduled, which is all barge-in and stopSpeaking need.
  function playStream(ctx, clip, valid) {
    return new Promise(async (resolve) => {
      const buffered = (from) => clip.chunks.slice(from).reduce((n, c) => n + c.length, 0) / clip.rate;
      while (!clip.done && !readyToPlay(clip, buffered(0))) {
        await new Promise(r => { clip.wake = r; });
        if (!valid()) return resolve();
      }
      if (!valid() || !clip.chunks.length) return resolve();
      const analyser = ctx.createAnalyser(); analyser.fftSize = 256;
      analyser.connect(ctx.destination);
      const playing = new Set();
      let next = 0, idx = 0, over = false;
      const finish = () => { if (over) return; over = true; endMouth(); resolve(); };
      ttsSource = { stop() { for (const s of playing) { try { s.stop(); } catch {} } playing.clear(); finish(); } };
      repaint();
      startMouth(analyser);
      for (;;) {
        while (idx < clip.chunks.length) {
          const pcm = clip.chunks[idx++];
          const buf = ctx.createBuffer(1, pcm.length, clip.rate);
          buf.getChannelData(0).set(pcm);
          const src = ctx.createBufferSource(); src.buffer = buf; src.connect(analyser);
          // Fell behind (generation slower than playback): leave a short gap rather
          // than scheduling in the past, which would garble the audio.
          if (next < ctx.currentTime + 0.02) next = ctx.currentTime + 0.05;
          src.start(next); next += buf.duration;
          playing.add(src);
          src.onended = () => {
            playing.delete(src);
            if (clip.done && idx >= clip.chunks.length && !playing.size) finish();
          };
        }
        if (over || !valid()) return finish();
        if (clip.done) { if (!playing.size) finish(); return; }
        await new Promise(r => { clip.wake = r; });
      }
    });
  }

  // Whole WAV for Kokoro/Piper; the streamed path for Orpheus.
  async function fetchAudio(text) {
    return usesStream() ? { clip: streamTTS(text) } : { buf: await fetchTTS(text) };
  }
  function playAudio(ctx, a, valid) {
    if (!a) return Promise.resolve();
    if (a.clip) return playStream(ctx, a.clip, valid);
    return a.buf ? playBuffer(ctx, a.buf, valid) : Promise.resolve();
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
      repaint();
      startMouth(analyser);
      src.onended = () => { endMouth(); resolve(); };
      src.start();
    });
  }

  // Prose the synthesizer can actually say. The assistant prompt asks for no markdown,
  // but that is steering, not a gate — a table reads aloud as a hedge of pipes. So table
  // rows become their cells, list/heading markers go while their text stays, and fenced
  // code (no sensible spoken form) goes entirely.
  function cleanForTTS(t) {
    return t
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/^[ \t]*\|[\s|:-]*\|[ \t]*$/gm, " ")
      .replace(/^[ \t]*\|(.*)\|[ \t]*$/gm, (_, row) => {
        const line = row.split("|").map(c => c.trim()).filter(Boolean).join(", ");
        return /[.!?]$/.test(line) ? line : line + ".";
      })
      .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
      .replace(/^[ \t]*[-*+][ \t]+/gm, "")
      .replace(/^[ \t]*\d+[.)][ \t]+/gm, "")
      .replace(/~(?=\d)/g, "about ")
      .replace(/\*[^*]*\*/g, " ")                 // *action* narration
      .replace(/\[expr:[^\]]*\]/gi, " ")          // a stray beat — never say it aloud
      .replace(/[`"_]/g, "")
      .replace(/\s+/g, " ").trim();
  }

  // ---- live expression beats ------------------------------------------------
  // Tokens run well ahead of sound (the queue prefetches audio), so a beat applied on
  // arrival would change the face seconds early. It rides the next speech segment and is
  // applied when that segment starts playing.
  function applyExpr(label) {
    if (!card || !label || label === exprLive) return;
    exprLive = label;
    if (avatarActive) {
      window.dispatchEvent(new CustomEvent("avatar:expr", { detail: { label } }));
      return;
    }
    emit("expr", { label });                    // the chat view swaps the sprite
  }

  function pendingExpr(label) {
    // Nothing will be spoken: no playback clock to sync to, so show it now.
    if (!speakingOn()) { applyExpr(label); return; }
    exprPending = label;
  }

  function flushExpr() {
    if (exprPending) { applyExpr(exprPending); exprPending = null; }
  }

  // The first thing she says decides how long you wait, so a reply's first piece is kept
  // short: a long opening sentence is split at its first clause break (after 6+ words),
  // and while it streams, a clause break after 8+ words is spoken without waiting for
  // the full stop. Later pieces stay whole sentences, which sound more natural.
  const CLAUSE = /[,;:\u2014\u2013](?=\s)|\s[-\u2013\u2014](?=\s)/g;
  const wordCount = (t) => (t.trim().match(/\S+/g) || []).length;
  function clauseCut(text, minWords) {
    CLAUSE.lastIndex = 0;
    let m;
    while ((m = CLAUSE.exec(text))) {
      const end = m.index + m[0].length;
      if (wordCount(text.slice(0, end)) >= minWords) return end;
    }
    return -1;
  }

  function enqueueSpeech(segment) {
    if (!speakingOn()) return;
    if (firstPiece) {
      firstPiece = false;
      if (wordCount(segment) > 14) {
        const cut = clauseCut(segment, 6);
        if (cut > 0 && wordCount(segment.slice(cut)) >= 4) {
          enqueueOne(segment.slice(0, cut));
          enqueueOne(segment.slice(cut));
          return;
        }
      }
    }
    enqueueOne(segment);
  }

  function enqueueOne(segment) {
    const clean = cleanForTTS(segment);
    if (!clean) return;
    spokenRecent.push({ text: clean.toLowerCase(), t: performance.now() });
    if (spokenRecent.length > 40) spokenRecent.shift();
    const item = { text: clean, run: ttsRun, expr: exprPending };
    if (usesStream()) { item.clip = makeClip(clean); queueGeneration(item.clip); }
    speechQueue.push(item);
    exprPending = null;
    consumeSpeech();
  }

  async function consumeSpeech() {
    if (speechConsuming) return;
    speechConsuming = true;
    const ctx = ensureAudioCtx();
    let prefetch = null;
    while (speechQueue.length) {
      const item = speechQueue.shift();
      if (item.run !== ttsRun) { prefetch = null; continue; }
      // Orpheus items are already generating (queueGeneration); whole-WAV voices
      // prefetch the next sentence while this one plays.
      const audio = item.clip ? { clip: item.clip }
        : (prefetch && prefetch.item === item) ? await prefetch.promise : await fetchAudio(item.text);
      const nxt = speechQueue[0];
      prefetch = (nxt && !nxt.clip) ? { item: nxt, promise: fetchAudio(nxt.text) } : null;
      if (item.expr && item.run === ttsRun) applyExpr(item.expr);
      if (item.run === ttsRun) await playAudio(ctx, audio, () => item.run === ttsRun);
    }
    speechConsuming = false;
    if (speechQueue.some(i => i.run === ttsRun)) consumeSpeech();
  }

  // Assistants speak everything: quotes in a reply mark a term or a citation, not dialogue.
  function feedSpeech(fullText, final) {
    if (!speakingOn()) return;
    feedSpeechWhole(fullText, final);
  }

  // A sentence at a time, so playback starts while the reply streams. The terminator must
  // be followed by whitespace, not end-of-buffer, or "3." would be cut out of "3.14".
  function feedSpeechWhole(fullText, final) {
    const SENT = /[.!?…]["')\]]*\s/g;
    SENT.lastIndex = speechFedLen;
    let end = -1, m;
    while ((m = SENT.exec(fullText))) {
      // An ellipsis followed by a lowercase word is a pause inside the thought, not its
      // end: "but yes... there's some truth" was cut in two, and the halves were spoken
      // as separate utterances with a gap between. Orpheus voices the "..." pause itself.
      const at = m.index, next = fullText.slice(at + m[0].length).match(/^\S/);
      const ellipsis = fullText[at] === "\u2026" || fullText.slice(Math.max(0, at - 2), at + 1) === "...";
      if (ellipsis && (!next || /[a-z]/.test(next[0]))) continue;
      end = at + m[0].length;
    }
    if (end > speechFedLen) {
      enqueueSpeech(fullText.slice(speechFedLen, end));
      speechFedLen = end;
    } else if (firstPiece && !final) {
      // No full stop yet: the opening clause can go now.
      const rest = fullText.slice(speechFedLen), cut = clauseCut(rest, 8);
      if (cut > 0) { enqueueSpeech(rest.slice(0, cut)); speechFedLen += cut; }
    }
    if (final) {
      if (speechFedLen < fullText.length) enqueueSpeech(fullText.slice(speechFedLen));
      speechFedLen = fullText.length;
    }
  }

  // One line now, outside the reply queue and not in the transcript: UI feedback
  // (the farewell), which must be immediate and predictable, not a generated turn.
  async function speakLine(text) {
    if (!text || !ttsEnabled) return;
    try {
      const ctx = ensureAudioCtx();
      stopSpeaking();
      const run = ttsRun;
      const audio = await fetchAudio(text);
      if (run === ttsRun) await playAudio(ctx, audio, () => run === ttsRun);
    } catch (e) { console.warn("[tts] line failed:", e); }
  }

  // ---- STT ------------------------------------------------------------------
  // `strict`: the clip overlapped her turn, so the server holds it to tighter
  // confidence limits (services/stt.py STRICT_*) — echo of her voice is the likely source.
  async function transcribeBlob(blob, strict = false) {
    try {
      const fd = new FormData();
      fd.append("file", blob, "speech.wav");
      if (strict) fd.append("strict", "1");
      const r = await fetch("/pmedia/stt", { method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf() }, body: fd });
      return r.ok ? ((await r.json()).text || "") : "";
    } catch { return ""; }
  }

  function wav(chunks, rate) {
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

  // Wait for the chat view to report the reply stopped: abort only signals the stream,
  // and a send while it is still winding down would be refused.
  async function abortAndWait() {
    stopSpeaking();
    if (!streaming) return;
    emit("abort");
    for (let i = 0; i < 60 && streaming; i++) await new Promise(r => setTimeout(r, 25));
  }

  function sendText(text) {
    if (!attached) { console.warn("[mic] no chat open — dropped:", text); return; }
    emit("send", { text });
  }

  // ---- microphone choice ------------------------------------------------------
  // deviceId is per-origin and rotates when permissions reset; the label is stable, so
  // it is the fallback. With full duplex on and nothing chosen, an echo-cancelled
  // source is the one correct answer, so it is picked automatically.
  async function pickMicDeviceId() {
    let devices = [];
    try {
      devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
    } catch { return settings.mic_device_id || ""; }
    const byId = devices.find(d => d.deviceId === settings.mic_device_id);
    if (byId) return byId.deviceId;
    const label = (settings.mic_device_label || "").trim().toLowerCase();
    if (label) {
      const byLabel = devices.find(d => (d.label || "").toLowerCase().includes(label));
      if (byLabel) return byLabel.deviceId;
    }
    if (settings.full_duplex) {
      const aec = devices.find(d => /aec|echo|cancel/i.test(d.label || ""));
      if (aec) return aec.deviceId;
      console.warn("[mic] full duplex is on but no echo-cancelled source was found — " +
                   "she may hear herself. Run scripts/setup-aec.sh --persist");
    }
    return "";
  }

  // Open the default device first: labels stay blank until a grant exists, so choosing
  // by label before it is choosing between empty strings.
  async function openMic() {
    const a = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
    let stream = await navigator.mediaDevices.getUserMedia({ audio: a });
    const want = await pickMicDeviceId();
    const t = stream.getAudioTracks()[0];
    const open = t && t.getSettings ? t.getSettings().deviceId : null;
    if (want && want !== open) {
      try {
        const better = await navigator.mediaDevices.getUserMedia({ audio: { ...a, deviceId: { exact: want } } });
        stream.getTracks().forEach(x => x.stop());
        stream = better;
      } catch (e) { console.warn("[mic] could not switch device, keeping the default:", e && e.name); }
    }
    return stream;
  }

  // ---- push-to-talk -------------------------------------------------------------
  // PCM off an AudioContext, not MediaRecorder: WebKitGTK's MediaRecorder reports
  // "recording" and never emits a byte (measured: 19 utterances, 0 bytes each).
  let micStream = null, recording = false, recCtx = null, recProc = null, recSrc = null, recPcm = [];
  let pttBusy = false;

  async function pttStart() {
    if (recording || pttBusy) return;
    await abortAndWait();                       // barge-in: cut her voice and the reply
    try {
      if (!micStream) micStream = await openMic();
    } catch (e) {
      toast("Microphone unavailable: " + ((e && e.name) || "permission denied"), "error");
      return;
    }
    recPcm = [];
    if (!recCtx) recCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (recCtx.state === "suspended") { try { await recCtx.resume(); } catch {} }
    recProc = recCtx.createScriptProcessor(4096, 1, 1);
    recProc.onaudioprocess = (e) => { if (recording) recPcm.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
    recSrc = recCtx.createMediaStreamSource(micStream);
    recSrc.connect(recProc);
    const mute = recCtx.createGain(); mute.gain.value = 0;     // must reach a destination to run
    recProc.connect(mute); mute.connect(recCtx.destination);
    recording = true;
    repaint();
  }

  async function pttStop() {
    if (!recording) return;
    recording = false;
    if (recProc) { recProc.onaudioprocess = null; try { recProc.disconnect(); } catch {} recProc = null; }
    if (recSrc) { try { recSrc.disconnect(); } catch {} recSrc = null; }
    const rate = (recCtx && recCtx.sampleRate) || 48000;
    const blob = wav(recPcm, rate);
    recPcm = [];
    repaint();
    if (blob.size <= 44) return;                // header only: nothing was captured
    pttBusy = true; repaint();
    try {
      const t = (await transcribeBlob(blob)).trim();   // an explicit press: no wake-word gate
      if (!t) toast("Didn't catch that.", "info");
      else if (!streaming) sendText(t);
    } finally { pttBusy = false; repaint(); }
  }

  // ---- self-hearing guard -----------------------------------------------------
  // full_duplex on speakers without an echo canceller makes her answer her own voice.
  // Check the symptom rather than trust the setup.
  function looksSelfHeard(text) {
    if (!settings.full_duplex) return false;
    const now = performance.now();
    const recent = spokenRecent.filter(s => now - s.t < 20000);
    if (!recent.length) return false;
    const said = recent.map(s => s.text).join(" ");
    const words = String(text).toLowerCase().match(/[a-z']{3,}/g) || [];
    if (words.length < 3) return false;
    return words.filter(w => said.includes(w)).length / words.length > 0.7;
  }

  function noteSelfHeard() {
    if (++selfHeardCount < 2) return;           // one may be coincidence; two is a fault
    selfHeardCount = 0;
    persist({ full_duplex: false });
    toast("She was hearing herself — interruption turned off. Use headphones, or run " +
          "scripts/setup-aec.sh --persist and pick aec_source in Settings.", "warning");
  }

  // ---- wake word + conversation state ---------------------------------------------
  // The character's own phrases win over the global ones.
  const activeWakeWord = () => ((card && card.wake_word) || settings.wake_word || "").trim();
  const activeSleepPhrase = () => ((card && card.sleep_phrase) || settings.sleep_phrase || "").trim();
  function activeFarewell() {
    const v = (card && card.farewell) ? card.farewell : settings.farewell;
    return (v === undefined ? "Goodbye." : v).trim();
  }

  // "continuous": the wake word opens a conversation, follow-ups need no prefix, until
  // the sleep phrase or the idle timeout. The timeout is not a nicety — an open mic that
  // never closes keeps transcribing the room after you walk away.
  let awake = false, awakeUntil = 0;
  const wakeTimeoutMs = () => {
    const s = settings.sleep_after_s;
    return (s === 0 || s === undefined) ? Infinity : s * 1000;
  };

  function setAwake(on, why) {
    if (awake === on) { if (on) awakeUntil = Date.now() + wakeTimeoutMs(); return; }
    awake = on;
    awakeUntil = on ? Date.now() + wakeTimeoutMs() : 0;
    console.log(`[mic] ${on ? "awake" : "asleep"} (${why})`);
    // Not when the mic is switched off: saying goodbye to someone who muted you is
    // talking to an empty room.
    if (!on && why !== "mic off") speakLine(activeFarewell());
    if (on) toast("Listening — say “" + (activeSleepPhrase() || "stop") + "” when done", "info");
    repaint();
  }

  // Compare on words alone: Whisper decides its own punctuation, and contractions are
  // the transcriber's choice too ("that will" vs "that'll").
  const CONTRACTIONS = [
    [/\bthat'?ll\b/g, "that will"], [/\bthat'?s\b/g, "that is"],
    [/\bit'?s\b/g, "it is"],        [/\bwe'?re\b/g, "we are"],
    [/\bi'?m\b/g, "i am"],          [/\bdon'?t\b/g, "do not"],
    [/\bwon'?t\b/g, "will not"],    [/\bcan'?t\b/g, "cannot"],
    [/\byou'?re\b/g, "you are"],    [/\blet'?s\b/g, "let us"],
  ];
  function normPhrase(t) {
    let x = String(t || "").toLowerCase().replace(/[‘’]/g, "'");
    for (const [re, to] of CONTRACTIONS) x = x.replace(re, to);
    return x.replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  }
  function startsWith(text, phrase) {
    const p = normPhrase(phrase);
    if (!p) return false;
    const t = normPhrase(text);
    return t === p || t.startsWith(p + " ");
  }
  // Works on the ORIGINAL text so the message keeps its punctuation and capitals.
  function stripPrefix(text, phrase) {
    const words = normPhrase(phrase).split(" ").filter(Boolean).length;
    let t = text.trim().replace(/^[^\w]+/, "");
    for (let i = 0; i < words; i++) t = t.replace(/^[^\s]+\s*/, "");
    return t.replace(/^[\s,.:!?-]+/, "").trim();
  }

  // The text to send, or "" to ignore this utterance.
  function applyWakeWord(text) {
    const w = activeWakeWord();
    const sleepPhrase = activeSleepPhrase();
    const continuous = (settings.wake_mode || "phrase") === "continuous";
    if (!w) return text.trim();                 // pure open-mic
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
      return rest;                              // her name alone is a summons, not a message
    }
    if (continuous && awake) {
      if (Date.now() > awakeUntil) { setAwake(false, "idle timeout"); return ""; }
      awakeUntil = Date.now() + wakeTimeoutMs();
      return text.trim();
    }
    return "";
  }

  // ---- hands-free (continuous VAD) --------------------------------------------------
  let hfStream = null, hfCtx = null, hfAnalyser = null, hfRAF = null, hfProc = null;
  let hfSpeaking = false, hfSilenceStart = 0, hfActive = false, hfLastError = "";
  let hfPcm = [], hfCapturing = false, hfPre = [], hfPeak = 0, hfPeakAt = 0, hfWakeTimer = null;
  // Set when an utterance opened while she was audible: the barge-in waits until the
  // transcript proves it was not her own voice. See hfTick / hfOnUtterance.
  let hfPendingBargeIn = false;
  // Set when any part of the utterance overlapped her turn. Whisper turns faint echo of
  // her voice into invented sentences that score like real speech under the normal
  // limits; while she is talking the transcript has to clear stricter ones.
  let hfStrict = false;
  // Her turn: the reply is streaming, a voice line is queued or playing, or she stopped
  // under 1.5 s ago. Wider than `audible`, which drops 0.4 s after each line, so a clip
  // that opens in the gap between two lines and then records the next one still counts.
  const herTurn = (now) => !!ttsSource || speechConsuming || speechQueue.length > 0 ||
    streaming || now < ttsHangoverUntil + 1500;
  const HF_PRE_ROLL = 8;            // ~0.7 s of 4096-frame blocks kept before the gate

  function hfStartUtterance() {
    hfPcm = hfPre.slice();          // keep what was said just before the gate opened
    hfCapturing = true;
    repaint();
  }

  function hfStopUtterance() {
    if (!hfCapturing) return;
    hfCapturing = false;
    repaint();
    hfOnUtterance();
  }

  async function hfOnUtterance() {
    // Read and clear together: no early return may leave a stale barge-in armed.
    const pendingBargeIn = hfPendingBargeIn, strict = hfStrict;
    hfPendingBargeIn = false; hfStrict = false;
    const blob = wav(hfPcm, (hfCtx && hfCtx.sampleRate) || 48000);
    hfPcm = []; hfPre = [];
    if (!blob.size || !hfActive) return;
    const raw = (await transcribeBlob(blob, strict)).trim();
    console.log("[mic] transcript" + (strict ? " (strict)" : "") + ": " + JSON.stringify(raw));
    if (awake) awakeUntil = Date.now() + wakeTimeoutMs();   // any speech keeps it open
    if (!raw) return;
    if (looksSelfHeard(raw)) { noteSelfHeard(); return; }   // never answer her own voice
    selfHeardCount = 0;
    const msg = applyWakeWord(raw);
    if (!msg) return;
    // Held back at the energy gate; the transcript has cleared both checks, so this
    // is a real interruption.
    if (pendingBargeIn) await abortAndWait();
    if (!streaming) sendText(msg);
  }

  function hfTick() {
    if (!hfActive) return;
    const data = new Uint8Array(hfAnalyser.frequencyBinCount);
    hfAnalyser.getByteTimeDomainData(data);
    let sum = 0; for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
    const rms = Math.sqrt(sum / data.length);
    const now = performance.now();
    const base = settings.vad_threshold ?? 0.015;
    // Peak once a second: tells a dead mic, a suspended context and too high a gate apart.
    hfPeak = Math.max(hfPeak, rms);
    if (now - hfPeakAt > 1000) {
      if (hfPeak > 0.002) console.debug(`[mic] peak ${hfPeak.toFixed(4)} / gate ${base}`);
      hfPeak = 0; hfPeakAt = now;
    }
    if (!hfSpeaking) {
      // Half-duplex (default) does not listen while she speaks. Full duplex lifts that —
      // only right when her voice cannot reach the mic (headphones, or an AEC source):
      // the raw mic hears her at +26 dB over the floor, the AEC source at -39 dB, so no
      // threshold can separate them.
      const audible = !!ttsSource || now < ttsHangoverUntil;
      const selfActive = audible && !settings.full_duplex;
      if (!selfActive && rms > base) {
        hfStartUtterance();
        hfStrict = herTurn(now);
        hfSpeaking = true; hfSilenceStart = 0;
        // Energy cannot tell your voice from her echo, and only the transcript can — so
        // while she is audible the abort waits for it (one STT round-trip of latency, and
        // only where echo is possible). When she is silent it still cuts instantly.
        if (audible && settings.full_duplex) hfPendingBargeIn = true;
        else if (ttsSource || streaming) abortAndWait();
      }
    } else {
      if (herTurn(now)) hfStrict = true;
      if (rms > base) hfSilenceStart = 0;
      else if (!hfSilenceStart) hfSilenceStart = now;
      else if (now - hfSilenceStart > (settings.vad_silence_ms ?? 800)) {
        hfSpeaking = false; hfSilenceStart = 0;
        hfStopUtterance();
      }
    }
    hfRAF = requestAnimationFrame(hfTick);
  }

  async function engageHandsFree() {
    if (hfActive) return true;
    hfLastError = "";
    try {
      hfStream = await openMic();
    } catch (e) {
      hfLastError = (e && (e.name + ": " + e.message)) || String(e);
      return false;
    }
    hfCtx = new (window.AudioContext || window.webkitAudioContext)();
    // A context made outside a gesture starts SUSPENDED and its analyser reads silence —
    // the VAD saw rms 0 forever while the mic looked fine. Fail loudly instead.
    if (hfCtx.state === "suspended") { try { await hfCtx.resume(); } catch {} }
    if (hfCtx.state !== "running") {
      hfLastError = "audio context " + hfCtx.state + " (needs a click)";
      try { hfStream.getTracks().forEach(t => t.stop()); } catch {}
      hfStream = null;
      try { hfCtx.close(); } catch {}
      hfCtx = null;
      return false;
    }
    hfAnalyser = hfCtx.createAnalyser(); hfAnalyser.fftSize = 512;
    const src = hfCtx.createMediaStreamSource(hfStream);
    src.connect(hfAnalyser);
    hfProc = hfCtx.createScriptProcessor(4096, 1, 1);
    hfPcm = []; hfPre = []; hfCapturing = false;
    hfProc.onaudioprocess = (e) => {
      const block = new Float32Array(e.inputBuffer.getChannelData(0));   // copy: reused
      if (hfCapturing) { hfPcm.push(block); return; }
      // Rolling pre-roll: the gate opens once speech is already loud, and the wake word
      // is the very first thing said — exactly the part that would be clipped.
      hfPre.push(block);
      if (hfPre.length > HF_PRE_ROLL) hfPre.shift();
    };
    src.connect(hfProc);
    const mute = hfCtx.createGain(); mute.gain.value = 0;
    hfProc.connect(mute); mute.connect(hfCtx.destination);
    hfActive = true; hfSpeaking = false; hfSilenceStart = 0; hfPendingBargeIn = false;
    // Expire on a timer, not on the next utterance: an empty room never speaks.
    if (!hfWakeTimer) hfWakeTimer = setInterval(() => {
      if (awake && Date.now() > awakeUntil) setAwake(false, "idle timeout");
    }, 2000);
    console.log("[mic] hands-free engaged; device=" + ((hfStream.getAudioTracks()[0] || {}).label || "?"));
    hfTick();
    repaint();
    return true;
  }

  function disengageHandsFree() {
    hfActive = false; hfSpeaking = false; hfCapturing = false; hfPendingBargeIn = false;
    if (hfWakeTimer) { clearInterval(hfWakeTimer); hfWakeTimer = null; }
    setAwake(false, "mic off");
    if (hfRAF) { cancelAnimationFrame(hfRAF); hfRAF = null; }
    // A ScriptProcessor keeps firing until disconnected.
    if (hfProc) { hfProc.onaudioprocess = null; try { hfProc.disconnect(); } catch {} hfProc = null; }
    hfPcm = []; hfPre = [];
    if (hfStream) { hfStream.getTracks().forEach(t => t.stop()); hfStream = null; }
    if (hfCtx) { try { hfCtx.close(); } catch {} hfCtx = null; }
    repaint();
  }

  function persist(patch) {
    Object.assign(settings, patch);
    emit("persist", { patch });
    repaint();
  }

  async function setHandsFree(on) {
    if (on) {
      if (!await engageHandsFree()) {
        console.error("[mic] hands-free failed:", hfLastError || "(no detail)");
        toast("Microphone unavailable: " + (hfLastError || "permission denied"), "error");
        on = false;
      }
    } else {
      disengageHandsFree();
    }
    persist({ hands_free: on });
  }

  // Saved as on: try now, and only wait for a click if the browser refuses
  // (getUserMedia and AudioContext want a gesture in a tab; the desktop shell has none).
  let resumeArmed = false;
  async function resumeHandsFree() {
    if (!settings.hands_free || hfActive) return;
    if (await engageHandsFree()) return;
    if (resumeArmed) return;
    resumeArmed = true;
    window.addEventListener("pointerdown", async () => {
      resumeArmed = false;
      if (attached && settings.hands_free && !hfActive && !await engageHandsFree()) {
        toast("Microphone unavailable: " + (hfLastError || "permission denied"), "error");
      }
    }, { once: true });
  }

  // ---- avatar ---------------------------------------------------------------------
  let avatarActive = false, avatarLoading = null, avatarKey = "";
  let canvas = null;

  window.addEventListener("avatar:active", (e) => {
    avatarActive = !!(e.detail && e.detail.active);
    if (canvas) canvas.hidden = !avatarActive;
    document.body.classList.toggle("pt-avatar-on", avatarActive);
    repaint();
  });
  window.addEventListener("avatar:error", (e) => {
    const d = e.detail || {};
    console.warn("[avatar]", d.where, d.message);
    if (d.where === "load") toast("The 3D model could not be loaded — showing the portrait instead.", "warning");
  });

  function ensureCanvas() {
    if (canvas) return canvas;
    canvas = document.createElement("canvas");
    canvas.id = "avatar-canvas";
    canvas.className = "pt-avatar";
    canvas.hidden = true;
    document.body.appendChild(canvas);
    return canvas;
  }

  // three.js and the VRM loader are ~1.5 MB and only a 3D-tier card needs them, so the
  // module is imported on first use. The import map is the classic page's; it has to
  // exist before the first bare "three" import resolves.
  function loadAvatarModule() {
    if (avatarLoading) return avatarLoading;
    ensureCanvas();
    return importAvatarModule();
  }

  // The module alone, without the live avatar's canvas — the Studio's profile still
  // needs the renderer but no avatar on screen.
  function importAvatarModule() {
    if (avatarLoading) return avatarLoading;
    if (!document.querySelector('script[type="importmap"][data-pantheon]')) {
      const map = document.createElement("script");
      map.type = "importmap";
      map.dataset.pantheon = "1";
      map.textContent = JSON.stringify({ imports: {
        "three": "/static/vendor/three.module.js",
        "three/addons/": "/static/vendor/",
        "@pixiv/three-vrm": "/static/vendor/three-vrm.module.js",
      } });
      document.head.appendChild(map);
    }
    avatarLoading = import("/static/js/vtuber.mjs").catch((e) => {
      console.warn("[avatar] renderer failed to load:", e);
      avatarLoading = null;
      throw e;
    });
    return avatarLoading;
  }

  // Move the canvas into the open chat's slot (or park it) and tell the renderer which
  // card is open. Keyed on the model, not just the character: assigning another .vrm to
  // the same character must reload.
  async function syncAvatar() {
    const want = attached && card && card.has_vrm;
    const key = want ? [card.id, card.vrm || ""].join("|") : "";
    const slot = document.getElementById("ch-avatar-slot");
    if (canvas) {
      if (want && slot && canvas.parentNode !== slot) slot.appendChild(canvas);
      else if (!want && canvas.parentNode !== document.body) document.body.appendChild(canvas);
    }
    if (key === avatarKey) return;
    avatarKey = key;
    if (!want) {
      if (avatarLoading) window.dispatchEvent(new CustomEvent("avatar:card", { detail: { id: null } }));
      return;
    }
    try { await loadAvatarModule(); } catch { toast("The 3D renderer could not start.", "warning"); return; }
    if (avatarKey !== key) return;              // switched while loading
    const s = document.getElementById("ch-avatar-slot");
    if (s && canvas.parentNode !== s) s.appendChild(canvas);
    const url = `/pmedia/card/${encodeURIComponent(card.id)}/avatar.vrm?v=${encodeURIComponent(card.vrm || "")}`;
    window.dispatchEvent(new CustomEvent("avatar:card", {
      detail: { id: card.id, hasVrm: true, url, rev: card.vrm || "" } }));
  }

  // ---- the API --------------------------------------------------------------------
  const V = window.PantheonVoice = {
    attach(c, s) {
      card = c || null;
      if (s) Object.assign(settings, s);
      attached = !!card;
      syncAvatar();
      if (settings.hands_free) resumeHandsFree();
      repaint();
    },
    detach() {
      attached = false;
      stopSpeaking();
      if (recording) { recording = false; recPcm = []; }
      // An open mic with no chat to send to is only a privacy cost. The saved setting
      // stays on, so the next chat picks listening back up.
      if (hfActive) disengageHandsFree();
      card = null;
      syncAvatar();
    },
    setSettings(patch) {
      const before = settings.mic_device_id;
      Object.assign(settings, patch || {});
      // A new microphone takes effect on the next open.
      if (settings.mic_device_id !== before) {
        if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
        if (hfActive) { disengageHandsFree(); if (attached) resumeHandsFree(); }
      }
      repaint();
    },
    setStreaming(on) { streaming = !!on; },
    // A PNG data URL of a .vrm, head and shoulders, for a card's profile picture.
    async captureVrmStill(url) {
      const m = await importAvatarModule();
      return m.captureStill(url);
    },
    beginReply() {
      stopSpeaking();
      if (ttsEnabled) ensureAudioCtx();
      // Make sure Orpheus is still loaded (an idle timeout or a backend restart unloads
      // it) while the first sentence is being written, not when it is about to be spoken.
      if (engineOn) postEngine(true);
    },
    feed(text, final) { feedSpeech(String(text || ""), !!final); },
    expr(label) { pendingExpr(String(label || "")); },
    flushExpr,
    stop() { stopSpeaking(); repaint(); },
    readAloud(text) {
      stopSpeaking();
      ensureAudioCtx();
      forced = true;
      try { feedSpeech(String(text || ""), true); } finally { forced = false; }
    },
    speaker(on) {
      ttsEnabled = on === undefined ? !ttsEnabled : !!on;
      try { localStorage.setItem("pantheon.speaker", ttsEnabled ? "1" : "0"); } catch {}
      if (ttsEnabled) ensureAudioCtx(); else stopSpeaking();
      repaint();
      return ttsEnabled;
    },
    pttStart, pttStop,
    handsFree(on) { return setHandsFree(on === undefined ? !settings.hands_free : !!on); },
    mood(mood, valence, stage) {
      if (!avatarActive) return;
      window.dispatchEvent(new CustomEvent("avatar:mood", { detail: { mood, valence, stage } }));
    },
    // Labels only appear after a mic grant, so ask once if they are blank.
    async mics(ask) {
      let devices = [];
      try {
        devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
        if (ask && devices.length && !devices[0].label) {
          const tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
          tmp.getTracks().forEach(t => t.stop());
          devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput");
        }
      } catch { /* denied or unavailable */ }
      return JSON.stringify(devices.filter(d => d.label).map(d => ({ id: d.deviceId, label: d.label,
                                                aec: /aec|echo|cancel/i.test(d.label || "") })));
    },
    state,
    // Test hook: what the self-hearing guard and wake-word gate would do with a transcript.
    _gate(text) { return applyWakeWord(String(text || "")); },
    _clean: cleanForTTS,
  };

  emit("ready");
})();
