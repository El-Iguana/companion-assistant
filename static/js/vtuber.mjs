// ===========================================================================
// Phase 3a — avatar renderer (module island)
//
// app.js is a single ~1600-line classic script and stays that way. three.js is
// ESM-only, so the renderer lives here as a module and the two talk over DOM
// CustomEvents on window — no imports either direction, no refactor of app.js.
//
//   app.js  --avatar:card-->    this   (which card is open; null = none)
//   app.js  --avatar:expr-->    this   (an [expr:x] beat, already playback-timed)
//   app.js  --avatar:speak-->   this   (mouth amplitude 0..1, per frame)
//   this    --avatar:active-->  app.js (true = avatar took over, hide the sprite)
//
// This phase is bring-up only: load, frame, render, resize, fall back. Idle
// life (breath/blink/gaze/spring bones) is 3b, speech motion 3c, emotion and
// gesture 3d. The only motion here is a slow turntable on the placeholder, so
// you can see the loop is actually running.
// ===========================================================================
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const CANVAS_ID = "avatar-canvas";
const FRAME_CAP = 30;           // plenty for a bust, and this shares a 4070 with
                                // KoboldCPP + ComfyUI — see the plan's perf budget.

let renderer = null, scene = null, camera = null, clock = null;
let currentVrm = null, placeholder = null, rafId = null;
let cardId = null, lastFrame = 0, running = false;

// ---- Phase 3b: idle life -------------------------------------------------
// Four channels, all always-on once a model is loaded. Spring bones come free
// with vrm.update(dt) but only react to motion, so without breath and sway they
// never move — the three channels are load-bearing for each other.
// Rendered-frame counter. A static avatar is ambiguous — loop running with nothing
// animated, or loop stalled after one frame — and this tells the two apart.
const stats = { frames: 0, lastRender: 0,
  get model() { return currentVrm ? "vrm" : (placeholder ? "placeholder" : "none"); },
  get running() { return running; },
  get idle() { return idleFailed ? "FAILED" : (idle ? "on" : "off"); },
  chestDeg: 0,
  get gain() { return idleGain; },
  set gain(v) { idleGain = Math.max(0, Number(v) || 0); },
  mouth: 0,
  get mouthGain() { return mouthGain; },
  set mouthGain(v) { mouthGain = Math.max(0, Number(v) || 0); },
  armSign: 0,
  get armDeg() { return armRestDeg; },
  set armDeg(v) {
    armRestDeg = Number(v) || 0;
    if (currentVrm) { applyRestPose(currentVrm, armRestDeg); initIdle(currentVrm); }
  } };
if (typeof window !== "undefined") window.__avatarStats = stats;

let idle = null;                 // per-model idle state, rebuilt on load
let idleFailed = false;
// Amplitude multiplier. 1 = shipping values; the harness raises it to make the
// channels unmistakable when you are checking whether the rig is being driven
// at all, which is a different question from whether the motion reads well.
// Both picked by eye against the test model. At gain 1 the motion was technically
// running but too small to read at bust framing.
let idleGain = 2;
// How far the upper arms swing down out of T-pose. Exposed for tuning: if it ever
// reads inverted (arms raised), this is the one number to flip.
let armRestDeg = 72;

// ---- Phase 3c: speech motion ---------------------------------------------
// app.js already computes an RMS amplitude per frame for the sprite mouth
// (the Phase 0 analyser) and forwards it as avatar:speak. That signal is one
// scalar, so it can drive a JAW but not visemes: which mouth shape belongs to
// which instant needs phoneme timings, and neither Kokoro nor Piper exposes
// them today. Anything here claiming to be a viseme would be decoration.
const speech = { amp: 0, target: 0, env: 0, last: 0 };
let mouthGain = 6;      // RMS from the analyser runs ~0.05-0.25 on speech
let lookTarget = null;           // Object3D the VRM's gaze tracks

// --------------------------------------------------------------------------
// Capability check. A machine without WebGL keeps the sprite portrait rather
// than showing a dead black rectangle.
// --------------------------------------------------------------------------
function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext &&
              (c.getContext("webgl2") || c.getContext("webgl")));
  } catch { return false; }
}

function announce(active) {
  window.dispatchEvent(new CustomEvent("avatar:active", { detail: { active } }));
}

// --------------------------------------------------------------------------
// Scene
// --------------------------------------------------------------------------
function initScene(canvas) {
  renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  scene = new THREE.Scene();                       // transparent: the chat shows through
  camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
  clock = new THREE.Clock();

  // Two lights, no shadows — cheap, and VRM toon materials do not want a harsh key.
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(1, 1.4, 2);
  scene.add(key, new THREE.AmbientLight(0xffffff, 1.1));

  resize();
}

// Frame the head and shoulders. VRM models are ~1.5 m tall with the origin at the
// feet, so aim at eye height rather than the model's centre.
function frameBust() {
  if (!camera) return;
  let eye = 1.4;                                   // sane default if there is no rig
  if (currentVrm) {
    // Matrices are stale right after scene.add() — without this the head reads as
    // world origin and the camera ends up on the floor looking at nothing.
    currentVrm.scene.updateWorldMatrix(true, true);
    const head = currentVrm.humanoid?.getNormalizedBoneNode?.("head")
              || currentVrm.humanoid?.getRawBoneNode?.("head");
    if (head) {
      const y = head.getWorldPosition(new THREE.Vector3()).y;
      if (y > 0.2) eye = y + 0.08;                 // head BONE sits at the jaw/ear line;
    }                                              // nudge up to the visual centre of the head
  }
  camera.position.set(0, eye, 1.4);
  camera.lookAt(0, eye - 0.03, 0);
}

function resize() {
  if (!renderer || !camera) return;
  const c = renderer.domElement;
  const w = c.clientWidth || 1, h = c.clientHeight || 1;
  if (c.width === w && c.height === h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

// --------------------------------------------------------------------------
// Placeholder. Track B (sourcing/rigging a .vrm) is manual art work and has not
// landed, so a card on the avatar tier with no model file still gets something
// on screen — enough to prove the loop, framing and resize are correct. It is
// deliberately abstract: a stand-in that cannot be mistaken for the real avatar.
// --------------------------------------------------------------------------
function buildPlaceholder() {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({
    color: 0x8a7fd6, roughness: 0.55, metalness: 0.05,
    transparent: true, opacity: 0.9,
  });
  const head = new THREE.Mesh(new THREE.IcosahedronGeometry(0.115, 2), mat);
  head.position.y = 1.42;
  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.12, 0.26, 4, 16), mat);
  torso.position.y = 1.13;
  g.add(head, torso);
  g.userData.head = head;
  return g;
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------
// A VRM ships in T-pose: the format carries a rig and blendshapes but no animation,
// so nothing brings the arms down unless we do it. This is part of idle life, not
// polish — breathing on a T-posed mannequin still reads as a mannequin.
//
// Which way a positive Z rotation swings the arm is NOT reliably derivable. Working
// it out from the VRM axis convention gave the wrong answer on the first test model
// and put the arms in the air, most likely because of the 180-degree Y rotation
// rotateVRM0 applies to VRM 0.x — which would also mean 1.0 models want the opposite
// sign from 0.x ones.
//
// So ask the model instead of assuming: nudge the arm and see which way the hand
// actually moved. One probe at load time, and the whole question goes away for any
// model and either spec version.
function detectArmSign(vrm) {
  const b = (n) => vrm.humanoid?.getNormalizedBoneNode?.(n) || null;
  const ua = b("leftUpperArm");
  const tip = b("leftHand") || b("leftLowerArm");
  if (!ua || !tip) return 1;
  const y = () => {
    vrm.scene.updateWorldMatrix(true, true);       // normalized rig is parented here
    return tip.getWorldPosition(new THREE.Vector3()).y;
  };
  const keep = ua.rotation.z;
  const y0 = y();
  ua.rotation.z = keep + 0.3;
  const yUp = y();
  ua.rotation.z = keep; y();                        // restore before anything renders
  const sign = yUp < y0 ? 1 : -1;                   // +z lowered the hand => + is down
  stats.armSign = sign;
  return sign;
}

// Positive armDeg = arms down, on any model.
function applyRestPose(vrm, armDeg) {
  const b = (n) => vrm.humanoid?.getNormalizedBoneNode?.(n) || null;
  const sgn = detectArmSign(vrm);
  const a = ((armDeg ?? 72) * Math.PI / 180) * sgn;
  const set = (node, x, y, z) => { if (node) node.rotation.set(x, y, z); };
  set(b("leftUpperArm"),  0,  0,  a);
  set(b("rightUpperArm"), 0,  0, -a);
  // A little elbow bend and inward roll, so the arms hang rather than hinge.
  set(b("leftLowerArm"),  0,  0.18 * sgn,  0.06 * sgn);
  set(b("rightLowerArm"), 0, -0.18 * sgn, -0.06 * sgn);
  set(b("leftHand"),  0, 0,  0.05 * sgn);
  set(b("rightHand"), 0, 0, -0.05 * sgn);
  // Shoulders settle very slightly down from the T.
  const ls = b("leftShoulder"), rs = b("rightShoulder");
  if (ls) ls.rotation.z =  0.04 * sgn;
  if (rs) rs.rotation.z = -0.04 * sgn;
}

// Rest pose is captured per model: breath and sway are applied as offsets FROM it,
// never as absolute rotations, so a model that does not stand in a neutral T/A-pose
// is not yanked into one.
function initIdle(vrm) {
  applyRestPose(vrm, armRestDeg);        // out of T first — rest is captured after
  const bone = (n) => vrm.humanoid?.getNormalizedBoneNode?.(n) || null;
  const rest = (b) => (b ? { x: b.rotation.x, y: b.rotation.y, z: b.rotation.z } : null);
  const spine = bone("spine"), chest = bone("chest") || bone("upperChest");
  const neck = bone("neck"), head = bone("head"), hips = bone("hips");
  idle = {
    spine, chest, neck, head, hips,
    rest: { spine: rest(spine), chest: rest(chest), neck: rest(neck),
            head: rest(head), hips: rest(hips) },
    t: 0,
    // Blink: a random interval, and sometimes a double-blink, because a perfectly
    // periodic blink reads as a machine.
    nextBlink: 1 + Math.random() * 3,
    blinkT: -1, blinkDur: 0.12, pendingDouble: false,
    // Gaze drift: hold the viewer, then glance away, the way people actually do.
    gazeT: 0, nextGaze: 2 + Math.random() * 4, gaze: { x: 0, y: 0 },
    gazeTo: { x: 0, y: 0 },
    // Sway: two incommensurable periods so the loop never visibly repeats.
    swayPhase: Math.random() * Math.PI * 2,
  };
  if (vrm.lookAt) {
    lookTarget = new THREE.Object3D();
    scene.add(lookTarget);
    vrm.lookAt.target = lookTarget;
  }
}

function updateIdle(vrm, dt) {
  if (!idle) return;
  idle.t += dt;
  const t = idle.t;

  const G = idleGain;

  // --- breathing: chest rises, spine counter-rotates a touch -----------------
  const breath = Math.sin(t * 1.05);                   // ~17 breaths/min
  if (idle.chest && idle.rest.chest)
    idle.chest.rotation.x = idle.rest.chest.x - breath * 0.045 * G;
  if (idle.spine && idle.rest.spine)
    idle.spine.rotation.x = idle.rest.spine.x + breath * 0.022 * G;

  // --- weight shift: slow lateral sway, hips lead and the head trails --------
  const sway = Math.sin(t * 0.31 + idle.swayPhase);
  const sway2 = Math.sin(t * 0.19 + idle.swayPhase * 1.7);
  if (idle.hips && idle.rest.hips) {
    idle.hips.rotation.z = idle.rest.hips.z + sway * 0.026 * G;
    idle.hips.rotation.y = idle.rest.hips.y + sway2 * 0.038 * G;
  }
  if (idle.neck && idle.rest.neck)
    idle.neck.rotation.z = idle.rest.neck.z - sway * 0.030 * G;
  if (idle.head && idle.rest.head) {
    idle.head.rotation.z = idle.rest.head.z - sway * 0.020 * G;
    idle.head.rotation.x = idle.rest.head.x + Math.sin(t * 0.23) * 0.018 * G;
  }
  // What actually reached the rig this frame — so "is the bone moving?" is a
  // number, not a squint.
  if (idle.chest && idle.rest.chest)
    stats.chestDeg = (idle.chest.rotation.x - idle.rest.chest.x) * 57.2958;

  // --- blink ----------------------------------------------------------------
  const em = vrm.expressionManager;
  if (em) {
    if (idle.blinkT >= 0) {
      idle.blinkT += dt;
      const half = idle.blinkDur / 2;
      // up then down, so the lid actually travels rather than popping
      const v = idle.blinkT < half ? idle.blinkT / half
                                   : 1 - (idle.blinkT - half) / half;
      em.setValue("blink", Math.max(0, Math.min(1, v)));
      if (idle.blinkT >= idle.blinkDur) {
        em.setValue("blink", 0);
        idle.blinkT = -1;
        if (idle.pendingDouble) { idle.pendingDouble = false; idle.nextBlink = 0.16; }
        else { idle.nextBlink = 1.6 + Math.random() * 4.5; }
      }
    } else {
      idle.nextBlink -= dt;
      if (idle.nextBlink <= 0) {
        idle.blinkT = 0;
        idle.pendingDouble = Math.random() < 0.22;
      }
    }
  }

  // --- gaze -----------------------------------------------------------------
  idle.gazeT -= dt;
  if (idle.gazeT <= 0) {
    // Mostly return to the viewer; occasionally look away and hold it briefly.
    const away = Math.random() < 0.45;
    idle.gazeTo = away
      ? { x: (Math.random() - 0.5) * 0.55, y: (Math.random() - 0.5) * 0.3 }
      : { x: 0, y: 0 };
    idle.gazeT = away ? 0.8 + Math.random() * 1.8 : 2 + Math.random() * 4;
  }
  idle.gaze.x += (idle.gazeTo.x - idle.gaze.x) * Math.min(1, dt * 2.5);
  idle.gaze.y += (idle.gazeTo.y - idle.gaze.y) * Math.min(1, dt * 2.5);
  if (lookTarget && camera) {
    // Track the camera, offset by the drift, so "centred" really means eyes on you.
    lookTarget.position.set(
      camera.position.x + idle.gaze.x,
      camera.position.y + idle.gaze.y,
      camera.position.z,
    );
  }
}

function updateSpeech(vrm, dt) {
  const em = vrm.expressionManager;
  if (!em) return;

  // The stream stops sending when she stops talking, so silence is the absence
  // of events rather than a zero. Close the mouth if nothing arrives.
  if (performance.now() - speech.last > 150) speech.target = 0;

  // Fast attack, slower release: a symmetric filter makes the jaw chatter on
  // consonants and hang open on release.
  const k = speech.target > speech.amp ? 18 : 7;
  speech.amp += (speech.target - speech.amp) * Math.min(1, dt * k);

  const open = Math.max(0, Math.min(1, speech.amp * mouthGain));
  // Mostly "aa" with a little "ih" underneath — pure aa at full travel reads as
  // a constant gape rather than speech.
  em.setValue("aa", open * 0.85);
  em.setValue("ih", open * 0.15);
  stats.mouth = open;

  // Prosody: a slow envelope of speech energy, not the per-frame amplitude, so
  // the head moves with phrasing instead of twitching per syllable.
  speech.env += (open - speech.env) * Math.min(1, dt * 1.5);
  if (idle && idle.head && idle.rest.head) {
    idle.head.rotation.x += speech.env * 0.045;          // slight nod into the line
    idle.head.rotation.y += Math.sin(idle.t * 1.7) * speech.env * 0.020;
  }
  if (idle && idle.chest && idle.rest.chest)
    idle.chest.rotation.x -= speech.env * 0.012;          // chest lifts a touch
}

function disposeModel() {
  for (const m of [currentVrm?.scene, placeholder]) {
    if (!m) continue;
    scene.remove(m);
    m.traverse((o) => {
      o.geometry?.dispose?.();
      const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
      for (const mm of mats) mm.dispose?.();
    });
  }
  if (currentVrm) VRMUtils.deepDispose(currentVrm.scene);
  if (lookTarget) { scene.remove(lookTarget); lookTarget = null; }
  idle = null; idleFailed = false;
  speech.amp = speech.target = speech.env = speech.last = 0;
  currentVrm = null;
  placeholder = null;
}

async function loadCard(id, hasVrm, placeholderRequested) {
  cardId = id;
  disposeModel();
  if (!id) { stop(); announce(false); return; }

  // Sprite tier is the default and must be untouched: no canvas, no takeover, the
  // portrait keeps the corner exactly as it always has. The placeholder is NOT a
  // fallback for these cards — it is opt-in, for the dev harness only.
  if (!hasVrm && !placeholderRequested) { stop(); announce(false); return; }

  if (hasVrm) {
    try {
      const loader = new GLTFLoader();
      loader.register((parser) => new VRMLoaderPlugin(parser));
      const gltf = await loader.loadAsync(`/api/cards/${id}/avatar.vrm`);
      if (cardId !== id) return;                    // card switched mid-load
      const vrm = gltf.userData.vrm;
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.removeUnnecessaryJoints(gltf.scene);
      // VRM 0.x and 1.0 disagree on which way is forward; this normalises 0.x to
      // match 1.0 so one camera placement works for both. Do not also rotate by
      // hand — that double-turns a 1.0 model and shows you its back.
      VRMUtils.rotateVRM0(vrm);
      currentVrm = vrm;
      scene.add(vrm.scene);
      frameBust();
      start();                  // render FIRST: a broken idle channel must never
                                // cost us the loop that proves the model is there
      try {
        initIdle(vrm);          // 3b: breath, sway, blink, gaze — always on
      } catch (e) {
        console.warn("[avatar] idle init failed; model still renders:", e);
        idle = null;
        window.dispatchEvent(new CustomEvent("avatar:error", {
          detail: { where: "idle-init", message: String(e && e.message || e),
                    stack: String(e && e.stack || "") },
        }));
      }
      announce(true);
      return;
    } catch (e) {
      console.warn("[avatar] VRM load failed, falling back to sprite:", e);
      // Surface it: this catch also covers post-load setup, so a bug in initIdle
      // looked exactly like a network failure and reached no UI at all.
      window.dispatchEvent(new CustomEvent("avatar:error", {
        detail: { where: "load", message: String(e && e.message || e),
                  stack: String(e && e.stack || "") },
      }));
      disposeModel();
      stop();
      announce(false);                              // sprite portrait takes over again
      return;
    }
  }

  // Explicitly requested stand-in (harness): Track B has not produced a .vrm yet,
  // so this is how 3a/3b are exercised before there is a model to load.
  placeholder = buildPlaceholder();
  scene.add(placeholder);
  frameBust();
  start();
  announce(true);
}

// --------------------------------------------------------------------------
// Render loop. Capped, and paused when the tab is hidden — this is a continuous
// loop sharing a GPU with the model backends, unlike the sprite it replaces.
// --------------------------------------------------------------------------
function tick(now) {
  rafId = requestAnimationFrame(tick);
  if (now - lastFrame < 1000 / FRAME_CAP) return;
  lastFrame = now;
  const dt = clock.getDelta();
  resize();
  stats.frames++; stats.lastRender = now;          // window.__avatarStats — proves the
                                                   // loop is live even when nothing moves
  if (placeholder) placeholder.rotation.y += dt * 0.35;
  if (currentVrm) {
    // Pose first, then update: vrm.update() consumes the normalized-bone rotations
    // and runs the spring bones, so the hair reacts to THIS frame's sway. Update
    // first and the springs lag a frame behind the body driving them.
    try {
      updateIdle(currentVrm, dt);
      updateSpeech(currentVrm, dt);      // after idle: adds to the idle pose
    } catch (e) {
      if (!idleFailed) {
        idleFailed = true;                          // report once, then stay quiet
        console.warn("[avatar] idle update failed; disabling idle:", e);
        window.dispatchEvent(new CustomEvent("avatar:error", {
          detail: { where: "idle-update", message: String(e && e.message || e),
                    stack: String(e && e.stack || "") },
        }));
      }
      idle = null;
    }
    currentVrm.update(dt);
  }
  renderer.render(scene, camera);
}

function start() {
  if (running) return;
  running = true; lastFrame = 0;
  clock.start();
  rafId = requestAnimationFrame(tick);
}

function stop() {
  running = false;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  if (renderer) renderer.clear();
}

// --------------------------------------------------------------------------
// Bridge
// --------------------------------------------------------------------------
export function boot() {
  const canvas = document.getElementById(CANVAS_ID);
  if (!canvas) return;
  if (!webglAvailable()) {
    console.warn("[avatar] no WebGL — staying on sprite portrait");
    announce(false);
    return;
  }
  initScene(canvas);

  window.addEventListener("avatar:card", (e) => {
    const { id, hasVrm, placeholder: ph } = e.detail || {};
    loadCard(id || null, !!hasVrm, !!ph);
  });

  window.addEventListener("avatar:speak", (e) => {
    speech.target = Math.max(0, Math.min(1, Number(e.detail && e.detail.amp) || 0));
    speech.last = performance.now();
  });
  window.addEventListener("avatar:expr",  () => { /* 3d: expression track */ });

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (currentVrm || placeholder) start();
  });
  announce(false);                                  // nothing loaded yet
}

boot();
