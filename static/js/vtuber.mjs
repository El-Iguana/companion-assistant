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
  let eye = 1.4;
  if (currentVrm) {
    const head = currentVrm.humanoid?.getNormalizedBoneNode?.("head");
    if (head) eye = head.getWorldPosition(new THREE.Vector3()).y;
  }
  camera.position.set(0, eye, 1.35);
  camera.lookAt(0, eye - 0.05, 0);
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
      start();
      announce(true);
      return;
    } catch (e) {
      console.warn("[avatar] VRM load failed, falling back to sprite:", e);
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
  // 3b replaces this with real idle life. For now: enough motion to see the loop run.
  if (placeholder) placeholder.rotation.y += dt * 0.35;
  currentVrm?.update(dt);
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

  // 3c/3d consume these. Accepted now so app.js can be wired once, and so the
  // events are exercised before there is anything to drive with them.
  window.addEventListener("avatar:speak", () => { /* 3c: jaw from amplitude */ });
  window.addEventListener("avatar:expr",  () => { /* 3d: expression track */ });

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (currentVrm || placeholder) start();
  });
  announce(false);                                  // nothing loaded yet
}

boot();
