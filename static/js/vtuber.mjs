// ===========================================================================
// Phase 3a — avatar renderer (module island)
//
// three.js is ESM-only, so the renderer lives here as a module and the page's voice
// code (static/js/voice.js; the classic app.js before it) talks to it over DOM
// CustomEvents on window — no imports either direction.
//
//   voice.js  --avatar:card-->    this   (which card is open; null = none)
//   voice.js  --avatar:expr-->    this   (an [expr:x] beat, already playback-timed)
//   voice.js  --avatar:speak-->   this   (mouth amplitude 0..1, per frame)
//   voice.js  --avatar:mood-->    this   (resting mood + relationship stage)
//   this      --avatar:active-->  voice.js (true = avatar took over, hide the sprite)
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
                                // KoboldCPP (and whatever else uses the GPU).

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
  armSign: 0, presets: "", emotion: "", beat: "", gesture: "", posture: "0",
  armPose: "rest",
  // Harness override: a pose name holds that pose; "" hands it back to the scheduler.
  get pose() { return armPose.forced; },
  set pose(v) { forceArmPose(String(v || "")); },
  get figure() { return figure; },
  set figure(v) { setFigure(v); },
  // Harness only: turn the model (degrees) to check a pose from the side.
  set turn(deg) { if (currentVrm) currentVrm.scene.rotation.y = (Number(deg) || 0) * Math.PI / 180 + (currentVrm.meta?.metaVersion === "0" ? Math.PI : 0); },
  shoulderDeg: 0,
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
// voice.js already computes an RMS amplitude per frame for the sprite mouth
// (the Phase 0 analyser) and forwards it as avatar:speak. That signal is one
// scalar, so it can drive a JAW but not visemes: which mouth shape belongs to
// which instant needs phoneme timings, and neither Kokoro nor Piper exposes
// them today. Anything here claiming to be a viseme would be decoration.
const speech = { amp: 0, target: 0, env: 0, last: 0 };
let mouthGain = 6;      // RMS from the analyser runs ~0.05-0.25 on speech

// ---- Phase 3d: emotion ----------------------------------------------------
// The app's vocabulary is Core-8 or Full-28; a VRM offers at most five emotive
// presets. The mapping is therefore lossy by construction, and the weight is
// what carries intensity — "annoyance" and "anger" both land on `angry`, at
// different strengths, rather than collapsing to the same face.
//
// Not every model has every preset: `surprised` exists in VRM 1.0 but not 0.x.
// Unsupported targets are remapped at load time against what the model actually
// reports, so a missing preset degrades instead of silently doing nothing.
const EXPR_MAP = {
  // The assistant's own labels (services/expressions.py ASSISTANT_EMOTIONS). The rest
  // below are the old roleplay set, kept so a mood stored in an old chat still shows.
  amused: ["happy", 0.75], curious: ["relaxed", 0.4], confused: ["surprised", 0.45],
  concerned: ["sad", 0.35], apologetic: ["sad", 0.3], grateful: ["happy", 0.65],
  proud: ["happy", 0.6], relieved: ["happy", 0.5], sympathetic: ["sad", 0.45],
  // -> happy
  happy: ["happy", 1.0], joy: ["happy", 1.0], amusement: ["happy", 0.8],
  excitement: ["happy", 0.9], optimism: ["happy", 0.6], pride: ["happy", 0.7],
  gratitude: ["happy", 0.6], approval: ["happy", 0.5], admiration: ["happy", 0.7],
  relief: ["happy", 0.5],
  // -> sad
  sad: ["sad", 1.0], sadness: ["sad", 1.0], grief: ["sad", 1.0],
  disappointment: ["sad", 0.7], remorse: ["sad", 0.8], nervousness: ["sad", 0.4],
  fear: ["sad", 0.6],
  // -> angry
  angry: ["angry", 1.0], anger: ["angry", 1.0], annoyance: ["angry", 0.5],
  disapproval: ["angry", 0.5], disgust: ["angry", 0.7],
  // -> surprised
  surprised: ["surprised", 1.0], surprise: ["surprised", 1.0],
  realization: ["surprised", 0.6], confusion: ["surprised", 0.5],
  // -> relaxed  (warmth, not amusement)
  relaxed: ["relaxed", 1.0], love: ["relaxed", 0.9], caring: ["relaxed", 0.8],
  desire: ["relaxed", 0.7], curiosity: ["relaxed", 0.4],
  embarrassed: ["relaxed", 0.5], embarrassment: ["relaxed", 0.5],
  thoughtful: ["relaxed", 0.3],
  neutral: ["neutral", 0],
};
// Where to go when a model lacks the mapped preset. VRM 0.x has no `surprised`.
const EXPR_FALLBACK = { surprised: "happy", relaxed: "happy", happy: null,
                        angry: null, sad: null, neutral: null };
const EMOTIVE = ["happy", "angry", "sad", "relaxed", "surprised"];

// Custom expressions. A model may carry its own VRM 1.0 custom expressions named
// after an app emotion label (e.g. `fear`, `pride`, `embarrassed`). When it does,
// that face is used at full weight instead of the lossy preset mapping above,
// since the author already set its strength. Aliases cover label variants.
const CUSTOM_ALIAS = { embarrassment: "embarrassed", embarrassed: "embarrassment" };

const emo = {
  channels: [...EMOTIVE],   // expressions this module drives: presets + customs
  weights: Object.fromEntries(EMOTIVE.map(k => [k, 0])),
  target:  Object.fromEntries(EMOTIVE.map(k => [k, 0])),
  supported: new Set(),
  custom: new Set(),        // custom expression names on THIS model
  beatUntil: 0,          // a beat is momentary; resting affect resumes after
  resting: { preset: null, weight: 0 },
};
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

// Frame the head and shoulders, fitted to the stage's shape. VRM models are ~1.5 m
// tall with the origin at the feet, so work from eye height, not the model's centre.
// The camera pulls back until a head-and-shoulders box fits BOTH the width and the
// height, with the head kept near the top: a tall, narrow stage is width-bound, so
// it shows more of the body instead of cropping the shoulders; a wide one is a bust.
// MAX_VIS_H caps the pull-back (head to upper thigh), so a very thin stage crops the
// arms rather than shrinking the face to nothing.
const FRAME_W = 0.55, FRAME_H = 0.62;   // metres that should stay in view
const MAX_VIS_H = 1.15;
const HEAD_AIR = 0.24;                  // eye height to the top edge of the frame
let viewerY = 1.4;                      // eye height the gaze aims at (not the camera's)
// Dancing to a clip (#120) the view eases out to more of her (0 = the bust, 1 = most of her).
// The eye height is measured once per model, at rest: measured while she dances, the camera
// would bob with her head.
const WIDE = { w: 1.0, h: 1.3, max: 1.9, air: 0.3 };
let frameView = 0, frameEye = null;
function frameBust() {
  if (!camera) return;
  let eye = 1.4;                                   // sane default if there is no rig
  if (frameEye !== null) eye = frameEye;
  else if (currentVrm) {
    // Matrices are stale right after scene.add() — without this the head reads as
    // world origin and the camera ends up on the floor looking at nothing.
    currentVrm.scene.updateWorldMatrix(true, true);
    const head = currentVrm.humanoid?.getNormalizedBoneNode?.("head")
              || currentVrm.humanoid?.getRawBoneNode?.("head");
    if (head) {
      const y = head.getWorldPosition(new THREE.Vector3()).y;
      if (y > 0.2) { eye = y + 0.08; frameEye = eye; }   // head BONE sits at the jaw/ear line;
    }                                              // nudge up to the visual centre of the head
  }
  viewerY = eye;
  const v = frameView, mix = (a, b) => a + (b - a) * v;
  const fw = mix(FRAME_W, WIDE.w), fh = mix(FRAME_H, WIDE.h), maxH = mix(MAX_VIS_H, WIDE.max);
  const tan = Math.tan(camera.fov * Math.PI / 360);
  const aspect = camera.aspect || 1;
  const d = Math.min(maxH / (2 * tan), Math.max(fh / (2 * tan), fw / (2 * tan * aspect)));
  const visH = 2 * d * tan;
  const cy = Math.max(visH / 2, eye + mix(HEAD_AIR, WIDE.air) - visH / 2);   // head top + air at the top edge
  camera.position.set(0, cy, d);
  camera.lookAt(0, cy, 0);
}

let sizeW = 0, sizeH = 0;
function resize() {
  if (!renderer || !camera) return;
  const c = renderer.domElement;
  const w = c.clientWidth || 1, h = c.clientHeight || 1;
  // Compare CSS sizes: c.width is scaled by the pixel ratio and never equals w on
  // a HiDPI screen, which resized the canvas every frame.
  if (w === sizeW && h === sizeH) return;
  sizeW = w; sizeH = h;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  frameBust();                                     // refit to the new shape
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
  const ls = bone("leftShoulder"), rs = bone("rightShoulder");
  idle = {
    spine, chest, neck, head, hips, ls, rs,
    // Rest must cover EVERY bone any channel touches. Shoulders were missing, and
    // because nothing reset them the posture and gesture deltas compounded frame
    // after frame until the arms wandered off and never came back.
    rest: { spine: rest(spine), chest: rest(chest), neck: rest(neck),
            head: rest(head), hips: rest(hips), ls: rest(ls), rs: rest(rs) },
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
  initArms(vrm);
  if (vrm.lookAt) {
    lookTarget = new THREE.Object3D();
    scene.add(lookTarget);
    vrm.lookAt.target = lookTarget;
  }
}

// Every animated bone back to its rest pose. Called once per frame BEFORE any
// channel runs, which is what lets idle, posture, speech and gesture all be
// purely additive — and lets an interrupted gesture simply stop contributing
// instead of leaving its offset baked in.
function resetPose() {
  if (!idle) return;
  for (const k of ["spine", "chest", "neck", "head", "hips", "ls", "rs"]) {
    const node = idle[k], r = idle.rest[k];
    if (node && r) node.rotation.set(r.x, r.y, r.z);
  }
}

function updateIdle(vrm, dt) {
  if (!idle) return;
  resetPose();                 // all channels below are += from here
  idle.t += dt;
  const t = idle.t;

  const G = idleGain;

  // --- breathing: chest rises, spine counter-rotates a touch -----------------
  const breath = Math.sin(t * 1.05);                   // ~17 breaths/min
  if (idle.chest) idle.chest.rotation.x -= breath * 0.045 * G;
  if (idle.spine) idle.spine.rotation.x += breath * 0.022 * G;

  // --- posture bias from the relationship -----------------------------------
  // A slow, sustained lean rather than a beat: guarded and closed at a distance,
  // open and inclined once she is close. Eased so a stage change does not snap.
  posture.cur += (posture.target - posture.cur) * Math.min(1, dt * 0.6);
  if (idle.spine) idle.spine.rotation.x += posture.cur * 0.055;
  if (idle.ls) idle.ls.rotation.z -= posture.cur * 0.03;
  if (idle.rs) idle.rs.rotation.z += posture.cur * 0.03;

  // --- weight shift: slow lateral sway, hips lead and the head trails --------
  const sway = Math.sin(t * 0.31 + idle.swayPhase);
  const sway2 = Math.sin(t * 0.19 + idle.swayPhase * 1.7);
  if (idle.hips) {
    idle.hips.rotation.z += sway * 0.026 * G;
    idle.hips.rotation.y += sway2 * 0.038 * G;
  }
  if (idle.neck) idle.neck.rotation.z -= sway * 0.030 * G;
  if (idle.head) {
    idle.head.rotation.z -= sway * 0.020 * G;
    idle.head.rotation.x += Math.sin(t * 0.23) * 0.018 * G;
  }
  // What actually reached the rig this frame — so "is the bone moving?" is a
  // number, not a squint.
  if (idle.chest && idle.rest.chest)
    stats.chestDeg = (idle.chest.rotation.x - idle.rest.chest.x) * 57.2958;
  // Shoulder drift was the bug that sent the arms wandering; keep it on screen so
  // it cannot come back unnoticed. Bounded numbers = healthy.
  if (idle.ls && idle.rest.ls)
    stats.shoulderDeg = (idle.ls.rotation.z - idle.rest.ls.z) * 57.2958;

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
    // Look at the viewer at eye height, offset by the drift, so "centred" really
    // means eyes on you. Not the camera's height: frameBust moves the camera down
    // for a tall stage, and the gaze must not follow it to your chest.
    lookTarget.position.set(
      camera.position.x + idle.gaze.x,
      viewerY + idle.gaze.y,
      camera.position.z,
    );
  }
}

// Which emotive presets does THIS model actually have? Asked once per load.
function initEmotion(vrm) {
  emo.supported = new Set();
  const em = vrm.expressionManager;
  for (const name of EMOTIVE) {
    if (em?.getExpression?.(name)) emo.supported.add(name);
  }
  emo.custom = new Set(Object.keys(em?.customExpressionMap || {}));
  emo.channels = [...emo.supported, ...emo.custom];
  emo.weights = {}; emo.target = {};
  for (const k of emo.channels) { emo.weights[k] = 0; emo.target[k] = 0; }
  emo.beatUntil = 0;
  emo.resting = { preset: null, weight: 0 };
  stats.presets = [...emo.supported].join(",") || "none";
  stats.custom = [...emo.custom].join(",") || "none";
}

// Resolve a label to a preset this model can actually show.
function resolvePreset(preset) {
  let p = preset, guard = 0;
  while (p && !emo.supported.has(p) && guard++ < 4) p = EXPR_FALLBACK[p] || null;
  return emo.supported.has(p) ? p : null;
}

// Resolve an emotion label to [expression, weight] for THIS model: its own
// custom expression first, then the preset mapping. null if nothing fits.
function resolveLabel(label) {
  const l = String(label || "").toLowerCase();
  for (const name of [l, CUSTOM_ALIAS[l]]) {
    if (name && emo.custom.has(name)) return [name, 1.0];
  }
  const hit = EXPR_MAP[l];
  if (!hit) return null;
  return [resolvePreset(hit[0]), hit[1]];
}

// A momentary beat from the stream. Phase 2 already timed these to audio playback,
// so this is deliberately not re-delayed here.
function setBeat(label) {
  const hit = resolveLabel(label);
  if (!hit) return;
  const [use, weight] = hit;
  for (const k of emo.channels) emo.target[k] = 0;
  if (use && weight > 0) emo.target[use] = weight;
  // Beats are punctuation, not state: hold, then fall back to resting affect.
  emo.beatUntil = performance.now() + 2600;
  stats.beat = label;
  const g = GESTURE_FOR[String(label || "").toLowerCase()];
  if (g) playGesture(g);
  armPoseForBeat(label);
}

// Resting affect from the running mood: a low, sustained bias, distinct from the
// sharp beats above. Valence -1..1 leans sad or happy; a named mood overrides.
function setResting(mood, valence) {
  const hit = mood && resolveLabel(mood);
  if (hit && hit[1] > 0) {
    const use = hit[0];
    emo.resting = use ? { preset: use, weight: hit[1] * 0.35 } : { preset: null, weight: 0 };
    return;
  }
  const v = Number(valence) || 0;
  const preset = resolvePreset(v > 0.15 ? "happy" : v < -0.15 ? "sad" : null);
  emo.resting = preset ? { preset, weight: Math.min(0.3, Math.abs(v) * 0.35) }
                       : { preset: null, weight: 0 };
}

function updateEmotion(vrm, dt) {
  const em = vrm.expressionManager;
  if (!em) return;

  // Beat expired: hand the face back to resting affect.
  if (emo.beatUntil && performance.now() > emo.beatUntil) {
    emo.beatUntil = 0;
    for (const k of emo.channels) emo.target[k] = 0;
    if (emo.resting.preset) emo.target[emo.resting.preset] = emo.resting.weight;
    stats.beat = "";
  }
  // Cross-fade, never snap. Rising faster than falling: a reaction should arrive
  // promptly and leave slowly, which is also how the sprite swap used to feel.
  for (const k of emo.channels) {
    const up = emo.target[k] > emo.weights[k];
    emo.weights[k] += (emo.target[k] - emo.weights[k]) * Math.min(1, dt * (up ? 4.5 : 1.8));
    em.setValue(k, emo.weights[k]);
  }
  stats.emotion = emo.channels.filter(k => emo.weights[k] > 0.02)
    .map(k => `${k} ${emo.weights[k].toFixed(2)}`).join(" ") || "neutral";
}

// ---- Phase 3d: gesture ----------------------------------------------------
// Procedural, not clip-based. Retargeting a Mixamo set is Track B3 and has not
// happened, so rather than block the whole phase these are short hand-authored
// pose offsets on an attack/hold/release envelope. They layer on top of the idle
// pose the same way speech motion does. When real clips arrive they replace the
// bodies of these functions, not the state machine around them.
const GESTURES = {
  // agreement / emphasis on a beat
  nod: { dur: 0.9, apply: (b, ph, w) => {
    const a = Math.sin(ph * Math.PI * 2) * w;
    if (b.head) b.head.rotation.x += a * 0.13;
    if (b.neck) b.neck.rotation.x += a * 0.05;
  }},
  // curiosity / listening
  tilt: { dur: 1.6, apply: (b, ph, w) => {
    const a = Math.sin(ph * Math.PI) * w;
    if (b.head) { b.head.rotation.z += a * 0.16; b.head.rotation.y += a * 0.05; }
  }},
  // interest, warmth — comes toward you
  leanIn: { dur: 2.2, apply: (b, ph, w) => {
    const a = Math.sin(ph * Math.PI) * w;
    if (b.spine) b.spine.rotation.x += a * 0.075;
    if (b.head) b.head.rotation.x -= a * 0.03;
  }},
  // shock / hurt — pulls back and up
  recoil: { dur: 1.1, apply: (b, ph, w) => {
    const a = Math.sin(ph * Math.PI) * w;
    if (b.spine) b.spine.rotation.x -= a * 0.09;
    if (b.head) b.head.rotation.x -= a * 0.07;
    if (b.ls) b.ls.rotation.z += a * 0.10;
    if (b.rs) b.rs.rotation.z -= a * 0.10;
  }},
  // dismissal / uncertainty
  shrug: { dur: 1.4, apply: (b, ph, w) => {
    const a = Math.sin(ph * Math.PI) * w;
    if (b.ls) b.ls.rotation.z += a * 0.16;
    if (b.rs) b.rs.rotation.z -= a * 0.16;
    if (b.head) b.head.rotation.z += a * 0.04;
  }},
};

// Which gesture reads as which feeling. Deliberately not one per label: a
// gesture on every beat would be a fidget, so several emotions share one and
// some map to nothing at all.
const GESTURE_FOR = {
  happy: "nod", amused: "nod", grateful: "nod", relieved: "nod", proud: "nod",
  curious: "tilt", thoughtful: "tilt", confused: "tilt", apologetic: "tilt",
  concerned: "leanIn", sympathetic: "leanIn",
  surprised: "recoil",
};

const gesture = { name: null, t: 0, dur: 0, cooldown: 0 };

// -1 = closed/guarded, +1 = open/leaning in. Driven by relationship stage.
const posture = { cur: 0, target: 0 };
const STAGE_POSTURE = {
  stranger: -0.7, acquaintance: -0.3, friend: 0.15, close: 0.45,
  romantic: 0.7, intimate: 0.85,
};
function setPosture(stage) {
  const key = String(stage || "").toLowerCase();
  let v = 0;
  for (const k of Object.keys(STAGE_POSTURE)) if (key.includes(k)) v = STAGE_POSTURE[k];
  if (key.includes("strained")) v -= 0.35;        // tension pulls back whatever the stage
  posture.target = Math.max(-1, Math.min(1, v));
  stats.posture = posture.target.toFixed(2);
}

function playGesture(name) {
  const g = GESTURES[name];
  if (!g || gesture.cooldown > 0) return;
  gesture.name = name; gesture.t = 0; gesture.dur = g.dur;
  gesture.cooldown = g.dur + 1.2;          // no stacking, no twitching
  stats.gesture = name;
}

function updateGesture(vrm, dt) {
  if (gesture.cooldown > 0) gesture.cooldown -= dt;
  if (!gesture.name || !idle) return;
  gesture.t += dt;
  const ph = gesture.t / gesture.dur;
  if (ph >= 1) { gesture.name = null; stats.gesture = ""; return; }
  // Ease in and out so it never starts or stops abruptly.
  const w = Math.sin(Math.min(1, ph) * Math.PI) ** 0.6;
  GESTURES[gesture.name].apply(
    { head: idle.head, neck: idle.neck, spine: idle.spine, chest: idle.chest,
      ls: idle.ls, rs: idle.rs }, ph, w);
}


// ---- Phase 3e: arm poses ----------------------------------------------------
// Held poses for the arms, so a resting avatar doesn't stand with them hanging for
// minutes: folded arms, a hand on a hip, hands clasped, a hand to the chin. Each is
// held for a while and eased in and out; the scheduler picks the next one while she
// is quiet, and an emotion beat can ask for a fitting one.
//
// Unlike the other channels these are not small offsets: folding the arms moves the
// upper arm a long way, and adding Euler angles that far tears. So each arm bone is
// set ABSOLUTELY every frame, slerped between its rest rotation and the pose's, which
// also means nothing can drift (the rule resetPose exists for). Nothing else touches
// these bones; gestures and posture move the shoulders, which stay additive.
//
// Poses are DIRECTIONS, not angles: for each arm segment, where it should point in the
// body's own frame, [out, up, forward] (out = away from the body on that side, so one
// definition serves both arms; negative = across the body). The rotation that points
// each bone that way is worked out per model from where its child bone actually sits
// (targetArms), so a pose needs no per-model angles and doesn't care which way a rig's
// axes run: the two test models disagree on the arm sign, and Euler angles tuned on one
// were nonsense on the other. An optional 4th number twists the segment about its own
// length (radians), for the palm. Tunable live from the harness (window.__avatarPoses),
// which is how these numbers were picked.
const ARM_BONES = ["UpperArm", "LowerArm", "Hand"];
const ARM_CHILD = { UpperArm: "LowerArm", LowerArm: "Hand", Hand: "MiddleProximal" };

// Finger curl. A pose's `curl: [fingers, thumb]` (0 = straight, 1 = a loose fist) bends
// every joint toward the palm. Which way that is needs no per-model knowledge: the
// normalized rig's reference pose is a T-pose with the palms DOWN, so in every hand the
// palm side is local -Y, and each joint turns about (its own direction x palm); the thumb
// aims between the palm and the little finger, so it closes across rather than down. Degrees
// at curl 1, per joint, base to tip; three-vrm names VRM 0.x thumbs the VRM 1.0 way.
const FINGERS = {
  Thumb: ["Metacarpal", "Proximal", "Distal"],
  Index: ["Proximal", "Intermediate", "Distal"], Middle: ["Proximal", "Intermediate", "Distal"],
  Ring: ["Proximal", "Intermediate", "Distal"], Little: ["Proximal", "Intermediate", "Distal"],
};
const CURL_DEG = { finger: [70, 95, 60], thumb: [12, 35, 45] };
const REST_CURL = [0.18, 0.1];          // a relaxed hand is never dead straight
const PALM = new THREE.Vector3(0, -1, 0);
const POSES = {
  rest: null,                                   // the rest pose itself
  // Forearms stacked (the right one higher and nearer the chest), hands tucked back
  // toward the opposite upper arm rather than sticking out past it.
  foldedArms: {
    L: { UpperArm: [0.3, -0.85, 0.42], LowerArm: [-0.85, 0.12, 0.45], Hand: [-0.2, 0.25, -0.9],
         curl: [0.55, 0.35] },
    R: { UpperArm: [0.3, -0.85, 0.36], LowerArm: [-0.85, 0.28, 0.3], Hand: [-0.2, 0.25, -0.9],
         curl: [0.55, 0.35] },
  },
  handOnHipL: {
    L: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
  },
  handOnHipR: {
    R: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
  },
  handsOnHips: {
    L: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
    R: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
  },
  // Hands folded low in front. Forearms forward enough to clear the body.
  handsClasped: {
    L: { UpperArm: [0.14, -0.95, 0.24], LowerArm: [-0.45, -0.65, 0.6], Hand: [-0.6, -0.7, 0.35],
         curl: [0.4, 0.25] },
    R: { UpperArm: [0.14, -0.95, 0.24], LowerArm: [-0.45, -0.65, 0.6], Hand: [-0.6, -0.7, 0.35],
         curl: [0.4, 0.25] },
  },
  // Right hand to the chin, its elbow resting on the left forearm across the waist.
  thinking: {
    L: { UpperArm: [0.2, -0.88, 0.4], LowerArm: [-0.85, 0.1, 0.5], Hand: [-0.3, 0.3, -0.9],
         curl: [0.45, 0.3] },
    R: { UpperArm: [0.05, -0.85, 0.52], LowerArm: [-0.28, 0.82, 0.42], Hand: [-0.35, 0.5, -0.75],
         curl: [0.7, 0.4] },
  },
  // Standing at ease: hands together behind the back, elbows back. Mostly the male set.
  handsBehindBack: {
    L: { UpperArm: [0.12, -0.93, -0.25], LowerArm: [-0.88, -0.35, -0.2], Hand: [-0.85, -0.45, 0.05],
         curl: [0.35, 0.25] },
    R: { UpperArm: [0.12, -0.93, -0.25], LowerArm: [-0.88, -0.35, -0.2], Hand: [-0.85, -0.45, 0.05],
         curl: [0.35, 0.25] },
  },
  // Male hands on hips: elbows wider, chest up, standing square.
  handsOnHipsSquare: {
    L: { UpperArm: [0.75, -0.62, -0.2], LowerArm: [-0.72, -0.62, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.35, 0.2] },
    R: { UpperArm: [0.75, -0.62, -0.2], LowerArm: [-0.72, -0.62, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.35, 0.2] },
    body: { arch: 0.04 },
  },
  // Female hip poses: the weight on one leg, so that hip rises and the upper body
  // counter-tilts (contrapposto). `body` is applied to the hips and spine; see bodyLean.
  handOnHipLLean: {
    L: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
    body: { shift: 0.045, tilt: 0.1, turn: 0.06, arch: 0.03 },
  },
  handOnHipRLean: {
    R: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
    body: { shift: -0.045, tilt: -0.1, turn: -0.06, arch: 0.03 },
  },
  handsOnHipsLean: {
    L: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
    R: { UpperArm: [0.6, -0.75, -0.25], LowerArm: [-0.7, -0.6, 0.1], Hand: [-0.3, -0.6, 0.6],
         curl: [0.3, 0.15] },
    body: { shift: -0.05, tilt: -0.11, turn: -0.05, arch: 0.05 },
  },
  // A sheepish scratch: one hand behind the head, elbow out. The forearm runs across
  // more than back, or from the side the hand sticks out behind the head.
  handBehindHead: {
    R: { UpperArm: [0.7, 0.6, 0.05], LowerArm: [-0.8, 0.35, -0.4], Hand: [-0.85, -0.15, -0.3],
         curl: [0.25, 0.15] },
  },
  // Leaning back, both hands behind the head.
  bothHandsBehindHead: {
    L: { UpperArm: [0.7, 0.6, 0.05], LowerArm: [-0.8, 0.35, -0.4], Hand: [-0.85, -0.15, -0.3],
         curl: [0.25, 0.15] },
    R: { UpperArm: [0.7, 0.6, 0.05], LowerArm: [-0.8, 0.35, -0.4], Hand: [-0.85, -0.15, -0.3],
         curl: [0.25, 0.15] },
  },
  // Hello / goodbye: a moving pose. `osc` swings one segment side to side about the
  // pose's direction (delta in the same [out, up, forward] terms, `hz` per second), and
  // `brief` makes it a moment rather than a pose: back to rest after that many seconds.
  wave: {
    // Elbow low and forward, hand beside the face: a wider wave leaves a bust frame.
    R: { UpperArm: [0.4, -0.45, 0.55], LowerArm: [-0.05, 0.95, 0.15], curl: [0.08, 0.05] },
    osc: { side: "R", part: "LowerArm", delta: [0.25, 0, 0], hz: 1.7 },
    brief: 2.6,
  },
  // Dancing (#115): elbows in at the sides, forearms forward, loose fists, the forearms
  // pumping up and down in turn. `osc` may be a list, each with its own `offset`; hz
  // "beat" means the dance's own phase drives it, so the arms keep time with the body.
  // Never picked at random (not in any POSE_SETS): only the dance asks for it.
  dance: {
    L: { UpperArm: [0.18, -0.9, 0.2], LowerArm: [-0.05, 0.2, 0.95], curl: [0.6, 0.4] },
    R: { UpperArm: [0.18, -0.9, 0.2], LowerArm: [-0.05, 0.2, 0.95], curl: [0.6, 0.4] },
    osc: [{ side: "L", part: "LowerArm", delta: [0, 0.4, -0.1], hz: "beat", offset: 0 },
          { side: "R", part: "LowerArm", delta: [0, 0.4, -0.1], hz: "beat", offset: Math.PI }],
  },
};
if (typeof window !== "undefined") window.__avatarPoses = POSES;

// Which poses a figure uses, how often each is picked when nothing asks for one, and
// which pose an emotion beat asks for. Rest stays the most common in every set: a pose
// is punctuation for a quiet moment, and constant re-posing is a fidget. A figure is the
// card's `figure` ("" = not set: the shared set every card had before figures).
const BEAT_POSE = {
  curious: "thinking", thoughtful: "thinking", confused: "thinking",
  proud: "handsOnHips", amused: "handOnHipL", happy: "handOnHipR",
  concerned: "handsClasped", sympathetic: "handsClasped", grateful: "handsClasped",
  apologetic: "handBehindHead", relieved: "bothHandsBehindHead",
};
const POSE_SETS = {
  "": {
    weights: { rest: 4, foldedArms: 2, handOnHipL: 1.2, handOnHipR: 1.2, handsOnHips: 0.6,
               handsClasped: 2, thinking: 1, handBehindHead: 0.5, bothHandsBehindHead: 0.5 },
    beats: {},
  },
  // Hip poses lean, with the weight on one leg; the clasped and behind-the-head poses stay.
  female: {
    weights: { rest: 4, foldedArms: 1.5, handOnHipLLean: 1.4, handOnHipRLean: 1.4,
               handsOnHipsLean: 0.8, handsClasped: 2, thinking: 1, handBehindHead: 0.5,
               bothHandsBehindHead: 0.6, handsBehindBack: 0.4 },
    beats: { proud: "handsOnHipsLean", happy: "handOnHipRLean", amused: "handOnHipLLean" },
  },
  // Square: folded arms and hands behind the back; no hands behind the head.
  male: {
    weights: { rest: 4, foldedArms: 2.5, handOnHipL: 0.6, handOnHipR: 0.6, handsOnHipsSquare: 1,
               handsBehindBack: 2, thinking: 1, handsClasped: 0.5 },
    beats: { proud: "handsOnHipsSquare", apologetic: "handsBehindBack", relieved: "handsBehindBack",
             concerned: "handsBehindBack", sympathetic: "handsBehindBack" },
  },
};
let figure = "";                         // this model's card's figure, from avatar:card
const poseSet = () => POSE_SETS[figure] || POSE_SETS[""];

const POSE_EASE_S = 0.9;                 // seconds to move into or out of a pose
const POSE_HOLD = [7, 16];               // a pose is held this long (s), at random
const POSE_REST = [6, 14];               // and rest between poses this long

const armPose = { name: "rest", forced: "", from: null, to: null, t: 1, hold: 4,
                  beatCooldown: 0, phase: 0, waveIn: -1, waveCooldown: 0, next: "",
                  body: { tilt: 0, turn: 0, arch: 0, shift: 0 }, bodyFrom: null, bodyTo: null };
const NO_LEAN = { tilt: 0, turn: 0, arch: 0, shift: 0 };

function initArms(vrm) {
  const bone = (n) => vrm.humanoid?.getNormalizedBoneNode?.(n) || null;
  const hips = bone("hips");
  const arms = { bones: {}, rest: {}, child: {}, curl: {}, parentRel: {}, frame: null };
  if (!hips) { idle.arms = null; return; }
  vrm.scene.updateWorldMatrix(true, true);
  const toHips = hips.getWorldQuaternion(new THREE.Quaternion()).invert();
  const hipsInv = new THREE.Matrix4().copy(hips.matrixWorld).invert();
  const at = (n) => { const b = bone(n); return b && b.getWorldPosition(new THREE.Vector3()).applyMatrix4(hipsInv); };
  // The body's frame, in the hips' space: left, up, forward from the model itself.
  const la = at("leftUpperArm"), ra = at("rightUpperArm"), head = at("head");
  if (!la || !ra || !head) { idle.arms = null; return; }
  const L = la.clone().sub(ra).normalize();
  const F = L.clone().cross(head.clone().normalize()).normalize();
  const U = F.clone().cross(L).normalize();
  arms.frame = { L, U, F };
  for (const side of ["L", "R"]) {
    const pre = side === "L" ? "left" : "right";
    const ua = bone(pre + "UpperArm");
    if (ua && ua.parent) arms.parentRel[side] = toHips.clone().multiply(ua.parent.getWorldQuaternion(new THREE.Quaternion()));
    for (const part of ARM_BONES) {
      const node = bone(pre + part);
      if (!node) continue;
      arms.bones[side + part] = node;
      arms.rest[side + part] = node.quaternion.clone();
      const c = bone(pre + ARM_CHILD[part]);
      if (c && c.position.lengthSq() > 1e-8) arms.child[side + part] = c.position.clone().normalize();
    }
    // A thumb closes across the palm, toward the little finger, not just down into it.
    const tb = bone(pre + "ThumbMetacarpal"), lb = bone(pre + "LittleProximal");
    const across = tb && lb ? lb.position.clone().sub(tb.position).normalize() : new THREE.Vector3();
    const thumbBend = PALM.clone().add(across.multiplyScalar(0.8)).normalize();
    for (const [finger, segs] of Object.entries(FINGERS)) {
      segs.forEach((seg, i) => {
        const node = bone(pre + finger + seg);
        if (!node) return;
        // The joint's own direction: toward the next joint, else from the last one.
        const next = segs[i + 1] && bone(pre + finger + segs[i + 1]);
        const dir = (next && next.position.lengthSq() > 1e-8 ? next.position : node.position).clone();
        if (dir.lengthSq() < 1e-8) return;
        const axis = dir.normalize().cross(finger === "Thumb" ? thumbBend : PALM);
        if (axis.lengthSq() < 1e-8) return;
        const key = side + finger + seg;
        arms.bones[key] = node;
        arms.rest[key] = node.quaternion.clone();
        arms.curl[key] = { axis: axis.normalize(), deg: CURL_DEG[finger === "Thumb" ? "thumb" : "finger"][i],
                           thumb: finger === "Thumb" };
      });
    }
  }
  // The lean's bones, with the body's forward/up/left axes in each one's own space.
  const lean = { axes: {} };
  for (const [key, name] of [["hips", "hips"], ["spine", "spine"], ["chest", "chest"], ["head", "head"]]) {
    const node = bone(name) || (key === "chest" ? bone("upperChest") : null);
    if (!node) continue;
    const inv = toHips.clone().multiply(node.getWorldQuaternion(new THREE.Quaternion())).invert();
    lean[key] = node;
    lean.axes[key] = { F: F.clone().applyQuaternion(inv), U: U.clone().applyQuaternion(inv),
                       L: L.clone().applyQuaternion(inv) };
  }
  const hips_ = bone("hips");
  lean.hipsPos = hips_.position.clone();
  lean.shiftDir = L.clone().applyQuaternion(hips_.quaternion);       // "left", in the hips' parent space
  arms.lean = lean;
  armPose.body = { ...NO_LEAN }; armPose.bodyFrom = null; armPose.bodyTo = null;
  idle.arms = arms;
  armPose.name = "rest"; armPose.t = 1; armPose.hold = 3 + Math.random() * 5;
  stats.armPose = "rest";
  armPose.from = snapshotArms(); armPose.to = targetArms("rest");
  armPose.waveIn = 0.8;                          // hello, once she has settled on screen
}

// The pose's rotation for every arm bone: each segment turned so its child bone points
// the way the pose says, worked down the chain (upper arm, then forearm, then hand) so
// every step knows where its parent ended up.
function targetArms(name, phase = 0) {
  const a = idle && idle.arms, out = {};
  if (!a) return out;
  const { L, U, F } = a.frame, def = POSES[name];
  const oscs = def && def.osc ? [].concat(def.osc) : [];
  for (const side of ["L", "R"]) {
    const sideDef = def && def[side];
    const out_ = side === "L" ? 1 : -1;               // "out" is +left for the left arm
    let parent = a.parentRel[side] ? a.parentRel[side].clone() : new THREE.Quaternion();
    for (const part of ARM_BONES) {
      const key = side + part;
      if (!a.bones[key]) continue;
      let v = sideDef && sideDef[part];
      for (const o of oscs) {
        if (!v || o.side !== side || o.part !== part) continue;
        const swing = Math.sin(phase + (o.offset || 0)) * (o.hz === "beat" ? dance.armGain : 1);
        v = v.map((x, i) => (i < 3 ? x + o.delta[i] * swing : x));
      }
      let q;
      if (!v || !a.child[key]) {
        q = a.rest[key].clone();
      } else {
        const d = L.clone().multiplyScalar(v[0] * out_)
          .add(U.clone().multiplyScalar(v[1])).add(F.clone().multiplyScalar(v[2])).normalize();
        const local = d.applyQuaternion(parent.clone().invert());
        q = new THREE.Quaternion().setFromUnitVectors(a.child[key], local);
        if (v[3]) q.premultiply(new THREE.Quaternion().setFromAxisAngle(local, v[3] * out_));
      }
      out[key] = q;
      parent = parent.multiply(q);
    }
    const [fc, tc] = (sideDef && sideDef.curl) || REST_CURL;
    for (const key of Object.keys(a.curl)) {
      if (key[0] !== side) continue;
      const c = a.curl[key], amount = c.thumb ? tc : fc;
      out[key] = a.rest[key].clone().multiply(
        new THREE.Quaternion().setFromAxisAngle(c.axis, (c.deg * amount) * Math.PI / 180));
    }
  }
  return out;
}

function snapshotArms() {
  const a = idle && idle.arms, out = {};
  if (a) for (const k of Object.keys(a.bones)) out[k] = a.bones[k].quaternion.clone();
  return out;
}

function startArmPose(name) {
  if (!idle || !idle.arms || !(name in POSES)) return;
  if (name === armPose.name && armPose.t >= 1) return;
  armPose.from = snapshotArms();                     // from wherever the arms are now
  armPose.to = targetArms(name);
  armPose.name = name; armPose.t = 0; armPose.phase = 0;
  armPose.bodyFrom = { ...armPose.body };
  armPose.bodyTo = { ...NO_LEAN, ...((POSES[name] && POSES[name].body) || {}) };
  const [lo, hi] = name === "rest" ? POSE_REST : POSE_HOLD;
  armPose.hold = POSES[name] && POSES[name].brief ? POSES[name].brief : lo + Math.random() * (hi - lo);
  stats.armPose = name;
}

// A different figure: drop a pose that isn't in the new set, back to rest.
function setFigure(v) {
  figure = v === "female" || v === "male" ? v : "";
  if (armPose.name && armPose.name !== "rest" && !POSES[armPose.name]?.brief
      && !(armPose.name in poseSet().weights) && !armPose.forced) startArmPose("rest");
}

// Hello and goodbye. Not while a pose is forced from the harness, and not twice in a
// row (a model load and a wake word can arrive together).
function wave() {
  if (!idle || !idle.arms || armPose.forced || armPose.waveCooldown > 0) return;
  armPose.waveCooldown = 6;
  armPose.name = "";                              // restart even if already waving
  startArmPose("wave");
}

function forceArmPose(name) {
  armPose.forced = name in POSES ? name : "";
  if (armPose.forced) { armPose.name = ""; startArmPose(armPose.forced); }   // even if current
}

function pickArmPose() {
  const w = poseSet().weights;
  const names = Object.keys(w).filter((n) => n !== armPose.name && n in POSES);
  let r = Math.random() * names.reduce((s, n) => s + w[n], 0);
  for (const n of names) { r -= w[n]; if (r <= 0) return n; }
  return "rest";
}

function armPoseForBeat(label) {
  const key = String(label || "").toLowerCase(), set = poseSet();
  const want = set.beats[key] || BEAT_POSE[key];
  // A pose outside this figure's set (a male card is never asked for hands behind the
  // head) is skipped, not substituted: the beat still plays on the face and the gesture.
  if (!want || !(want in set.weights) || armPose.forced || armPose.beatCooldown > 0 || want === armPose.name
      || armPose.name === "dance") return;
  armPose.beatCooldown = 20;                         // a beat may re-pose her now and then
  if (POSES[armPose.name] && POSES[armPose.name].brief) armPose.next = want;   // let a wave finish
  else startArmPose(want);
}

// The pose's lean, on top of idle/posture/gesture (which already reset these bones this
// frame): contrapposto, the S-curve of standing with the weight on one leg.
//   shift  the pelvis moves sideways over the standing leg (metres, + = to her left);
//   tilt   that hip rises (radians about the body's forward axis, + raises the left);
//   the spine carries the trunk back over the centre, the chest slopes the shoulders
//   against the hips, and the head stays near upright (LEAN: each one's share);
//   turn   the hips twist a little toward the raised side; arch: the chest lifts.
// Axes come from the body frame (initArms), so the lean goes the same way on any rig.
// Picked against screenshots: the lower spine keeps leaning away from the raised hip
// (negative: it adds to the hips' tilt) so the trunk comes back over the centre, and the
// chest more than undoes it, which slopes the shoulders against the hips.
const LEAN = { spine: -0.8, chest: 2.2, head: 0.3 };
if (typeof window !== "undefined") window.__avatarLean = LEAN;

function bodyLean(a, b) {
  const lean = a.lean;
  if (!lean) return;
  if (lean.hips && lean.hipsPos)                   // position isn't reset by resetPose
    lean.hips.position.copy(lean.hipsPos).addScaledVector(lean.shiftDir, b.shift || 0);
  if (!(b.tilt || b.turn || b.arch)) return;
  const rot = (key, axisName, angle) => {
    const node = lean[key];
    if (node && angle) node.quaternion.multiply(
      new THREE.Quaternion().setFromAxisAngle(lean.axes[key][axisName], angle));
  };
  rot("hips", "F", b.tilt);
  rot("hips", "U", b.turn);
  rot("spine", "F", -b.tilt * LEAN.spine);
  rot("chest", "F", -b.tilt * LEAN.chest);
  rot("head", "F", b.tilt * LEAN.head);
  rot("spine", "L", -b.arch);
  rot("chest", "L", -b.arch * 0.7);
}

function updateArms(vrm, dt) {
  const a = idle && idle.arms;
  if (!a || !armPose.to) return;
  if (armPose.beatCooldown > 0) armPose.beatCooldown -= dt;
  if (armPose.waveCooldown > 0) armPose.waveCooldown -= dt;
  if (armPose.waveIn >= 0 && (armPose.waveIn -= dt) < 0) wave();
  // Dancing: the arms join in unless she's speaking or listening, or a wave is on; out of
  // the dance they go back to rest, and the usual scheduler takes over from there.
  if (!armPose.forced && !(POSES[armPose.name] && POSES[armPose.name].brief)) {
    if (danceArms() && armPose.name !== "dance") startArmPose("dance");
    else if (!danceArms() && armPose.name === "dance") startArmPose("rest");
  }
  const def = POSES[armPose.name];
  if (def && def.osc) {                           // a moving pose: re-aim every frame
    const o = [].concat(def.osc)[0];
    armPose.phase = o.hz === "beat" ? dance.phase * Math.PI          // one pump per arm per 2 beats
                                    : armPose.phase + dt * Math.PI * 2 * o.hz;
    armPose.to = targetArms(armPose.name, armPose.phase);
  }
  armPose.t = Math.min(1, armPose.t + dt / (def && def.brief ? POSE_EASE_S * 0.6 : POSE_EASE_S));
  const k = armPose.t * armPose.t * (3 - 2 * armPose.t);         // smoothstep
  for (const key of Object.keys(a.bones)) {
    const from = armPose.from[key] || a.rest[key], to = armPose.to[key] || a.rest[key];
    a.bones[key].quaternion.slerpQuaternions(from, to, k);
  }
  if (armPose.bodyFrom && armPose.bodyTo)
    for (const c of ["tilt", "turn", "arch", "shift"])
      armPose.body[c] = armPose.bodyFrom[c] + (armPose.bodyTo[c] - armPose.bodyFrom[c]) * k;
  bodyLean(a, armPose.body);
  if (armPose.forced || armPose.t < 1 || armPose.name === "dance") return;
  armPose.hold -= dt;
  if (def && def.brief) {                         // a wave ends on time, talking or not
    if (armPose.hold <= 0) { startArmPose(armPose.next || "rest"); armPose.next = ""; }
    return;
  }
  // Held long enough: move on, but not mid-sentence: re-posing while she talks reads
  // as fidgeting. A beat (above) is the exception, since it is the reply's own cue.
  if (armPose.hold <= 0 && speech.env < 0.05)
    startArmPose(armPose.name === "rest" ? pickArmPose() : "rest");
}

// ---- #115: dancing to the music ----------------------------------------------------
// A generated dance timed to the song's tempo (avatar:dance {bpm}; null stops it): a bounce
// down on every beat (hips, spine, a nod), a sway from side to side over two beats, the
// shoulders rising in turn, and the arms pumping (the "dance" pose). A faster song is
// livelier (`energy`, 0 at 80 BPM to 1 at 160), not only quicker. While she speaks or
// listens (avatar:calm, the moments the music ducks) it softens to a sway so her face and
// gestures read, and it eases in and out, never snapping. Like idle, every body channel is
// additive on the bones resetPose returns to rest each frame; the hips' height is a
// position, so the bob is applied as a change from the last frame's.
const DANCE_EASE = 1.4;                  // how fast the dance comes and goes (1/s)
const DANCE_CALM = 0.22;                 // how much of it stays while she talks or listens
const dance = { bpm: 0, on: false, calm: false, w: 0, phase: 0, bob: 0, armGain: 1,
                synced: 0, local: 0, err: 0 };
// On the beat (#124): the player sends which beat the song is on (`phase`, beats as a float)
// and the tempo right there (`local`) every half second. A small difference is eased out over
// ~0.3 s; a big one (a seek, a new song) is jumped. Without them (no beats known), the dance
// keeps its own time at the song's tempo, as before.
const SYNC_SNAP = 0.3, SYNC_TTL_MS = 2000;
if (typeof window !== "undefined") window.__avatarDance = dance;

function setDance(d) {
  const o = d && typeof d === "object" ? d : { bpm: d };
  const b = Number(o.bpm) || 0;
  dance.on = b > 0;
  if (dance.on) dance.bpm = Math.max(50, Math.min(220, b));   // kept while it eases out
  stats.dance = dance.on ? dance.bpm : 0;
  if (dance.on && Number.isFinite(o.phase)) {
    const err = o.phase - dance.phase;
    if (!dance.synced || Math.abs(err) > SYNC_SNAP) { dance.phase = o.phase; dance.err = 0; }
    else dance.err = err;
    dance.local = Number(o.local) || dance.bpm;
    dance.synced = performance.now();
  }
}
const danceRate = () => (dance.synced && performance.now() - dance.synced < SYNC_TTL_MS && dance.local)
  ? dance.local : dance.bpm;

const danceEnergy = () => Math.max(0, Math.min(1, (dance.bpm - 80) / 80));
const danceArms = () => dance.on && !dance.calm && speech.env < 0.05 && dance.w > 0.3;

function updateDance(vrm, dt) {
  if (!idle) return;
  const quiet = dance.calm || speech.env > 0.05;
  const target = dance.on ? (quiet ? DANCE_CALM : 1) : 0;
  dance.w += (target - dance.w) * Math.min(1, dt * DANCE_EASE);
  if (dance.bpm > 0) {                                           // in beats, kept on the song's
    const fix = dance.err * Math.min(1, dt * 3.5);
    dance.phase += dt * danceRate() / 60 + fix;
    dance.err -= fix;
  }
  const e = danceEnergy(), w = dance.w, p = dance.phase;
  dance.armGain = 0.55 + 0.45 * e;
  stats.danceW = w;
  // The bounce: sharpest exactly on the beat, gone halfway between.
  const dip = w < 0.001 ? 0 : Math.pow(1 - Math.abs(Math.sin(Math.PI * p)), 1.5) * w;
  const sway = Math.sin(Math.PI * p) * w;                       // one side, then the other
  const hipK = figure === "male" ? 0.5 : figure === "female" ? 1.3 : 1;
  const { hips, spine, chest, neck, head, ls, rs } = idle;
  if (hips) {
    hips.rotation.z += sway * (0.035 + 0.025 * e) * hipK;
    hips.rotation.y += sway * (0.04 + 0.03 * e) * hipK;
    // bodyLean puts the hips back where they belong every frame when there's a lean; else
    // the last frame's bob is taken off here, so nothing accumulates either way.
    const reset = !!(idle.arms && idle.arms.lean && idle.arms.lean.hipsPos);
    const bob = -dip * (0.008 + 0.012 * e);
    hips.position.y += bob - (reset ? 0 : dance.bob);
    dance.bob = bob;
  }
  if (spine) spine.rotation.x += dip * (0.02 + 0.025 * e);
  if (chest) chest.rotation.z -= sway * (0.03 + 0.02 * e) * hipK;
  if (neck) neck.rotation.z -= sway * 0.012;
  if (head) {
    head.rotation.x += dip * (0.03 + 0.035 * e);                // the nod
    head.rotation.z += sway * 0.018;
  }
  const sh = Math.sin(Math.PI * p) * w * (0.03 + 0.04 * e);     // shoulders up in turn
  if (ls) ls.rotation.z += sh;
  if (rs) rs.rotation.z += sh;
}

// ---- #120: dance clips -----------------------------------------------------------------
// Real dance motion (motion capture), made by scripts/dance/convert.py into the VRM humanoid's
// own bone rotations (cantinero-dance/1), listed by GET /pmedia/dance. While she dances, a clip
// whose tempo can be matched to the song's (CLIP_RATE: played 0.8-1.25x, its tempo halved or
// doubled first) plays over the generated dance; a new one every 4-8 bars, cross-faded. Calm
// (she speaks or listens) fades it out to the generated sway and back in after. With no clips,
// or none that fits the tempo, she dances the generated way.
//
// Sampled here rather than by an AnimationMixer: a mixer blends with the bones' state at bind
// time, not with this frame's pose, so it couldn't fade into the generated dance. The bones the
// other channels never touch (legs, upper chest) are put back to rest every frame, so a clip
// fading out can't leave them where it was.
const CLIP_RATE = [0.8, 1.25];
const CLIP_FADE_S = 0.6;                 // between two clips
const CLIP_EASE = 1.6;                   // in and out of clips (1/s)
const CLIP_BARS = [4, 8];                // a clip plays this many bars of the song, at random
const clipState = {
  list: null, listAt: 0, loading: "", data: {}, cur: null, prev: null, fade: 1, w: 0,
  beats: 0, switchAt: 0, lastId: "", force: "", model: null, rest: {}, view: 0,
};
if (typeof window !== "undefined") window.__avatarClips = clipState;

async function clipList() {
  if (clipState.list && performance.now() - clipState.listAt < 5 * 60e3) return clipState.list;
  clipState.listAt = performance.now();
  try {
    const r = await fetch("/pmedia/dance", { credentials: "same-origin" });
    clipState.list = r.ok ? ((await r.json()).clips || []) : [];
  } catch { clipState.list = []; }
  return clipState.list;
}

// The song's tempo against a clip's: the playback rate, or 0 when it can't be matched.
function clipRate(clipBpm) {
  if (!clipBpm || !dance.bpm) return 0;
  let r = dance.bpm / clipBpm;
  while (r < 0.75) r *= 2;
  while (r > 1.5) r /= 2;
  return r >= CLIP_RATE[0] && r <= CLIP_RATE[1] ? r : 0;
}

function pickClip() {
  const list = (clipState.list || []).filter((c) => clipRate(c.bpm));
  if (clipState.force) return list.find((c) => c.id === clipState.force) || null;
  if (!list.length) return null;
  const e = danceEnergy();
  const scored = list.map((c) => ({ c, s: Math.abs((c.energy ?? 0.5) - e) + Math.abs(clipRate(c.bpm) - 1)
                                         + (c.id === clipState.lastId && list.length > 1 ? 5 : 0)
                                         + Math.random() * 0.35 }));
  scored.sort((a, b) => a.s - b.s);
  return scored[0].c;
}

// A clip as typed arrays, the VRM 0.x flip applied once (its normalized rig faces -Z).
async function loadClip(meta) {
  if (clipState.data[meta.id]) return clipState.data[meta.id];
  const r = await fetch(`/pmedia/dance/${encodeURIComponent(meta.id)}.json`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`clip ${meta.id}: ${r.status}`);
  const d = await r.json();
  const clip = { id: meta.id, name: d.name, bpm: d.bpm, fps: d.fps, frames: d.frames,
                 loop: d.loop || [0, d.frames - 1], bones: {}, hips: new Float32Array(d.hips || []) };
  for (const [b, v] of Object.entries(d.bones || {})) clip.bones[b] = new Float32Array(v);
  clip.restDirs = d.rest_dirs || {};
  clip.beatFrame = Number.isFinite(d.beat_frame) ? d.beat_frame : null;
  clipState.data[meta.id] = clip;
  return clip;
}

function startClip(clip) {
  clipState.prev = clipState.cur;
  clipState.cur = { clip, frame: clip.loop[0], placed: false };
  clipState.fade = clipState.prev ? 0 : 1;
  clipState.lastId = clip.id;
  clipState.beats = 0;
  clipState.switchAt = 4 * (CLIP_BARS[0] + Math.floor(Math.random() * (CLIP_BARS[1] - CLIP_BARS[0] + 1)));
  stats.clip = clip.name;
}

async function chooseClip() {
  if (clipState.loading) return;
  clipState.loading = "…";
  try {
    await clipList();
    const meta = pickClip();
    if (!meta) { if (!clipState.cur) stats.clip = ""; return; }
    if (clipState.cur && meta.id === clipState.cur.clip.id) { clipState.beats = 0; return; }
    startClip(await loadClip(meta));
  } catch (e) {
    console.warn("[avatar] dance clip:", e);
  } finally { clipState.loading = ""; }
}

const _qa = new THREE.Quaternion(), _qb = new THREE.Quaternion();
function sampleBone(state, bone, out) {
  const c = state.clip, arr = c.bones[bone];
  const i = Math.floor(state.frame), f = state.frame - i;
  const j = i + 1 > c.loop[1] ? c.loop[0] : i + 1;
  _qa.fromArray(arr, i * 4); _qb.fromArray(arr, j * 4);
  return out.copy(_qa).slerp(_qb, f);
}
function sampleHips(state, out) {
  const c = state.clip, h = c.hips;
  const i = Math.floor(state.frame), f = state.frame - i;
  const j = i + 1 > c.loop[1] ? c.loop[0] : i + 1;
  return out.set(h[i * 3] + (h[j * 3] - h[i * 3]) * f, h[i * 3 + 1] + (h[j * 3 + 1] - h[i * 3 + 1]) * f,
                 h[i * 3 + 2] + (h[j * 3 + 2] - h[i * 3 + 2]) * f);
}
// Keep a clip's beats on the song's (#124). The converter marks a frame where the clip's moves
// land (beat_frame); played at `rate`, the clip takes m beats per song beat (1, or 2 or 1/2 when
// its tempo was doubled or halved to fit). The difference is jumped when the clip starts and
// eased out after, through the loop's wrap too (a loop is whole beats).
const CLIP_LEAD = 0;                     // beats: nudge if her moves look early (+) or late (-)
if (typeof window !== "undefined") window.__avatarClipLead = { beats: CLIP_LEAD };
function onBeat(state, rate, dt) {
  const c = state.clip;
  if (c.beatFrame == null || !dance.bpm) return;
  const pc = c.fps * 60 / c.bpm;                       // clip frames per clip beat
  const m = Math.pow(2, Math.round(Math.log2((c.bpm * rate) / dance.bpm)));
  const lead = (window.__avatarClipLead && window.__avatarClipLead.beats) || 0;
  const want = (dance.phase + lead) * m;
  let err = want - (state.frame - c.beatFrame) / pc;
  err -= Math.round(err);                              // to the nearest beat: -0.5..0.5
  const k = state.placed ? Math.min(1, dt * 3) : 1;
  state.frame += err * pc * k;
  state.placed = true;
  const len = c.loop[1] - c.loop[0];
  if (len > 0) {
    while (state.frame >= c.loop[1]) state.frame -= len;
    while (state.frame < c.loop[0]) state.frame += len;
  }
}

function advance(state, dt, rate) {
  const c = state.clip;
  state.frame += dt * c.fps * rate;
  const len = c.loop[1] - c.loop[0];
  if (len > 0) while (state.frame >= c.loop[1]) state.frame -= len;
}

// The humanoid's own hierarchy (the converter's VRM_PARENT), parents first, and each bone's
// child for its rest direction.
const CLIP_PARENT = {
  hips: null, spine: "hips", chest: "spine", upperChest: "chest", neck: "upperChest", head: "neck",
  leftShoulder: "upperChest", leftUpperArm: "leftShoulder", leftLowerArm: "leftUpperArm", leftHand: "leftLowerArm",
  rightShoulder: "upperChest", rightUpperArm: "rightShoulder", rightLowerArm: "rightUpperArm", rightHand: "rightLowerArm",
  leftUpperLeg: "hips", leftLowerLeg: "leftUpperLeg", leftFoot: "leftLowerLeg", leftToes: "leftFoot",
  rightUpperLeg: "hips", rightLowerLeg: "rightUpperLeg", rightFoot: "rightLowerLeg", rightToes: "rightFoot",
};
const CLIP_ORDER = Object.keys(CLIP_PARENT);
const CLIP_CHILD = {
  spine: ["chest"], chest: ["upperChest", "neck"], upperChest: ["neck"], neck: ["head"],
  leftShoulder: ["leftUpperArm"], leftUpperArm: ["leftLowerArm"], leftLowerArm: ["leftHand"],
  leftHand: ["leftMiddleProximal"], rightShoulder: ["rightUpperArm"], rightUpperArm: ["rightLowerArm"],
  rightLowerArm: ["rightHand"], rightHand: ["rightMiddleProximal"],
  leftUpperLeg: ["leftLowerLeg"], leftLowerLeg: ["leftFoot"], leftFoot: ["leftToes"],
  rightUpperLeg: ["rightLowerLeg"], rightLowerLeg: ["rightFoot"], rightFoot: ["rightToes"],
};
const clipParent = (clip, name) => {
  let p = CLIP_PARENT[name];
  while (p && !clip.bones[p]) p = CLIP_PARENT[p];
  return p;
};

// Per model and clip: the turn that takes each bone's rest direction on THIS model to the
// clip's (its source skeleton's rest). A normalized VRM bone's rest is the identity, so its
// child's position is its rest direction; for VRM 0.x it is mirrored into the clip's frame.
const LIMBS = new Set(["leftUpperLeg", "leftLowerLeg", "rightUpperLeg", "rightLowerLeg",
  "leftUpperArm", "leftLowerArm", "rightUpperArm", "rightLowerArm"]);
function restFixes(vrm, clip) {
  const key = clip.id;
  clipState.fixes = clipState.fixesFor === vrm ? clipState.fixes : {};
  clipState.fixesFor = vrm;
  if (clipState.fixes[key]) return clipState.fixes[key];
  const out = {};
  for (const [name, src] of Object.entries(clip.restDirs || {})) {
    // Limbs only: a limb is a straight segment on both skeletons, so its direction means the
    // same; a shoulder or spine segment runs between joints the two place differently (CMU's
    // clavicle rises 34 deg to its shoulder), and matching those would hunch her.
    if (!LIMBS.has(name)) continue;
    const kid = (CLIP_CHILD[name] || []).map((c) => vrm.humanoid?.getNormalizedBoneNode?.(c)).find(Boolean);
    if (!kid || kid.position.lengthSq() < 1e-10) continue;
    const mine = kid.position.clone().normalize();
    if (clipState.flip) { mine.x = -mine.x; mine.z = -mine.z; }
    out[name] = new THREE.Quaternion().setFromUnitVectors(mine, new THREE.Vector3().fromArray(src).normalize());
  }
  return (clipState.fixes[key] = out);
}

const OTHER_CHANNELS = new Set(["hips", "spine", "chest", "neck", "head", "leftShoulder", "rightShoulder",
  "leftUpperArm", "leftLowerArm", "leftHand", "rightUpperArm", "rightLowerArm", "rightHand"]);
const _q1 = new THREE.Quaternion(), _q2 = new THREE.Quaternion(), _v1 = new THREE.Vector3();

function updateClips(vrm, dt) {
  if (!idle) return;
  const bone = (n) => vrm.humanoid?.getNormalizedBoneNode?.(n) || null;
  if (clipState.model !== vrm) {                     // a new model: its rest, its orientation
    clipState.model = vrm; clipState.rest = {};
    clipState.flip = String(vrm.meta?.metaVersion ?? "1") === "0";
  }
  // Bones only a clip moves go back to rest first (see above).
  for (const [name, q] of Object.entries(clipState.rest)) { const n = bone(name); if (n) n.quaternion.copy(q); }

  const quiet = dance.calm || speech.env > 0.05;
  const cur = clipState.cur;
  const rate = cur ? clipRate(cur.clip.bpm) : 0;
  const want = dance.on && !quiet && !!cur && rate > 0;
  clipState.w += ((want ? 1 : 0) - clipState.w) * Math.min(1, dt * CLIP_EASE);
  if (!dance.on && clipState.w < 0.01) { clipState.cur = clipState.prev = null; stats.clip = ""; }
  // Choose: when dancing starts, when the tempo stops fitting, and every few bars.
  if (dance.on && !clipState.loading && performance.now() >= (clipState.retryAt || 0)) {
    if (!cur || !rate || clipState.beats >= clipState.switchAt || (clipState.force && cur.clip.id !== clipState.force)) {
      clipState.retryAt = performance.now() + 2000;      // none fits: look again in a while
      chooseClip();
    }
  }
  // The view: out while a clip shows, back after.
  const view = clipState.w;
  if (Math.abs(view - frameView) > 0.002) { frameView = view; frameBust(); }
  stats.clipW = clipState.w;
  if (!cur || clipState.w < 0.002) return;

  const r = rate || 1;
  advance(cur, dt, r);
  onBeat(cur, r, dt);
  if (clipState.prev) {
    advance(clipState.prev, dt, clipRate(clipState.prev.clip.bpm) || 1);
    clipState.fade = Math.min(1, clipState.fade + dt / CLIP_FADE_S);
    if (clipState.fade >= 1) clipState.prev = null;
  }
  clipState.beats += dt * dance.bpm / 60;
  const w = clipState.w, prev = clipState.prev, k = clipState.fade;
  // 1. The clip's world rotations (its locals are relative to CLIP_PARENT).
  const world = clipState.world || (clipState.world = {});
  for (const name of CLIP_ORDER) {
    if (!cur.clip.bones[name]) continue;
    const q = world[name] || (world[name] = new THREE.Quaternion());
    sampleBone(cur, name, q);
    if (prev && prev.clip.bones[name]) q.copy(sampleBone(prev, name, _q2).slerp(q, k));
    const p = clipParent(cur.clip, name);
    if (p) q.premultiply(world[p]);
  }
  // 2. Each of this model's bones: the clip's world turn, corrected by how its rest direction
  //    differs from the clip's (restFix), made local to its nearest ancestor the model has.
  const fix = restFixes(vrm, cur.clip);
  for (const name of CLIP_ORDER) {
    const n = bone(name);
    if (!n || !cur.clip.bones[name]) continue;
    if (!OTHER_CHANNELS.has(name) && !clipState.rest[name]) clipState.rest[name] = n.quaternion.clone();
    _q1.copy(world[name]);
    if (fix[name]) _q1.multiply(fix[name]);
    let p = CLIP_PARENT[name];
    while (p && !(bone(p) && cur.clip.bones[p])) p = CLIP_PARENT[p];
    if (p) {
      _q2.copy(world[p]);
      if (fix[p]) _q2.multiply(fix[p]);
      _q1.premultiply(_q2.invert());
    }
    if (clipState.flip) { _q1.x = -_q1.x; _q1.z = -_q1.z; }
    n.quaternion.slerp(_q1, w);
  }
  // The hips' sway, in leg lengths: on top of where the other channels put them this frame.
  const lean = idle.arms && idle.arms.lean, hips = bone("hips");
  if (hips && lean && lean.hipsPos && cur.clip.hips.length) {
    sampleHips(cur, _v1);
    if (clipState.flip) { _v1.x = -_v1.x; _v1.z = -_v1.z; }
    const leg = Math.max(0.5, lean.hipsPos.y);
    hips.position.addScaledVector(_v1, leg * w);
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
  if (idle && idle.head) {
    idle.head.rotation.x += speech.env * 0.045;          // slight nod into the line
    idle.head.rotation.y += Math.sin(idle.t * 1.7) * speech.env * 0.020;
  }
  if (idle && idle.chest) idle.chest.rotation.x -= speech.env * 0.012;
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
  dance.bob = 0;                                   // a new model starts from its own hips
  frameEye = null; frameView = 0;
  clipState.model = null;
  currentVrm = null;
  placeholder = null;
}

async function loadCard(id, hasVrm, placeholderRequested, modelUrl, rev) {
  cardId = id;
  disposeModel();
  if (!id) { stop(); announce(false); return; }

  // Sprite tier is the default and must be untouched: no canvas, no takeover, the
  // portrait keeps the corner exactly as it always has. The placeholder is NOT a
  // fallback for these cards — it is opt-in, for the dev harness only.
  if (!hasVrm && !modelUrl && !placeholderRequested) { stop(); announce(false); return; }

  if (hasVrm || modelUrl) {
    try {
      const loader = new GLTFLoader();
      loader.register((parser) => new VRMLoaderPlugin(parser));
      // A direct url wins over the card lookup: the harness previews a model that
      // no card points at yet, which is also what a model picker needs.
      // The per-card URL is stable while its contents are not, so a changed
      // model would otherwise be served from cache. `rev` is the filename, so
      // the URL only changes when the model actually does.
      const url = modelUrl ||
        `/api/cards/${id}/avatar.vrm${rev ? "?v=" + encodeURIComponent(rev) : ""}`;
      const gltf = await loader.loadAsync(url);
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
        initEmotion(vrm);       // 3d: what presets does this model have?
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
      updateGesture(currentVrm, dt);     // and so does gesture
      updateArms(currentVrm, dt);        // 3e: held arm poses (absolute, from rest)
      updateDance(currentVrm, dt);       // #115: after the arms, which reset the hips' position
      updateClips(currentVrm, dt);       // #120: real dance motion over all of that
      updateEmotion(currentVrm, dt);     // expression track (independent of pose)
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
    const { id, hasVrm, placeholder: ph, url, rev, figure: fig } = e.detail || {};
    setFigure(fig || "");
    loadCard(id || null, !!hasVrm, !!ph, url || null, rev || "");
  });

  window.addEventListener("avatar:speak", (e) => {
    speech.target = Math.max(0, Math.min(1, Number(e.detail && e.detail.amp) || 0));
    speech.last = performance.now();
  });
  window.addEventListener("avatar:expr", (e) => {
    if (currentVrm) setBeat(e.detail && e.detail.label);
  });
  // Hello when the wake word opens a conversation (voice.js), goodbye when it ends;
  // not when the mic is merely switched off, as with the spoken farewell.
  window.addEventListener("avatar:wave", () => { if (currentVrm) wave(); });
  window.addEventListener("pvoice:asleep", (e) => {
    if (currentVrm && !(e.detail && e.detail.why === "mic off")) wave();
  });
  // Dancing to the music (#115): the player says the tempo (null or 0 stops), and the
  // voice says when she is speaking or listening (the music ducks then; she calms).
  window.addEventListener("avatar:dance", (e) => setDance(e.detail));
  if (window.__cantineroDance) setDance(window.__cantineroDance);   // sent before this loaded
  window.addEventListener("avatar:calm", (e) => { dance.calm = !!(e.detail && e.detail.on); });
  window.addEventListener("avatar:mood", (e) => {
    const d = e.detail || {};
    if (!currentVrm) return;
    setResting(d.mood, d.valence);
    if (d.stage !== undefined) setPosture(d.stage);
  });

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (currentVrm || placeholder) start();
  });
  announce(false);                                  // nothing loaded yet
}

boot();

// --------------------------------------------------------------------------
// Profile picture: one still of a model, rendered off-screen. Same rest pose,
// lights and toon materials as the live avatar, but its own renderer and scene,
// so it never disturbs an avatar that is on screen. Returns a PNG data URL with a
// transparent background, framed head-and-shoulders at 3:4 like the card grid.
// Used by the Studio's "Profile picture: 3D model still" (via PantheonVoice).
// --------------------------------------------------------------------------
export async function captureStill(url, { width = 768, height = 1024, expression = "" } = {}) {
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  const r = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, preserveDrawingBuffer: true });
  r.setPixelRatio(1);
  r.setSize(width, height, false);
  r.outputColorSpace = THREE.SRGBColorSpace;
  const sc = new THREE.Scene();
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(1, 1.4, 2);
  sc.add(key, new THREE.AmbientLight(0xffffff, 1.1));
  const cam = new THREE.PerspectiveCamera(30, width / height, 0.1, 20);
  let vrm = null;
  try {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync(url);
    vrm = gltf.userData.vrm;
    if (!vrm) throw new Error("not a VRM model");
    VRMUtils.rotateVRM0(vrm);
    sc.add(vrm.scene);
    applyRestPose(vrm, armRestDeg);

    vrm.scene.updateWorldMatrix(true, true);
    const head = vrm.humanoid?.getNormalizedBoneNode?.("head");
    let eye = 1.4;
    if (head) {
      const y = head.getWorldPosition(new THREE.Vector3()).y;
      if (y > 0.2) eye = y + 0.08;
    }
    // Head and shoulders: ~0.5 m tall at 0.95 m with a 30° lens, centred a little
    // below the eyes so there is air above the hair and the collar is in frame.
    // The face sits in the upper-middle, which the round avatars' centre crop keeps.
    cam.position.set(0, eye - 0.1, 0.95);
    cam.lookAt(0, eye - 0.12, 0);
    if (vrm.lookAt) vrm.lookAt.target = cam;
    if (expression && vrm.expressionManager?.getExpression?.(expression)) {
      vrm.expressionManager.setValue(expression, 1);
    }
    vrm.update(0);                                  // apply pose, gaze, expression
    r.render(sc, cam);
    return canvas.toDataURL("image/png");
  } finally {
    if (vrm) VRMUtils.deepDispose(vrm.scene);
    r.dispose();
    r.forceContextLoss?.();
  }
}
