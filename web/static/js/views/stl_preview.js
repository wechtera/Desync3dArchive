/**
 * The STL preview: one movable window that shows a mesh in the browser, lit like 4pm sun.
 * three.js loads on first use, because it's 2 MB and most sessions never open this.
 */
import { html, render } from '../template.js';

const VENDOR = '/static/vendor/three-0.186.1';
// Roughly where the sun sits at 4pm, low in the west-southwest and warm. Compass degrees, with the
// model's front facing south, because that puts the light across the side you see first.
const SUN_AZIMUTH_DEGREES = 250;
const SUN_ELEVATION_DEGREES = 30;
const SUN_COLOR = 0xffd9ac;
const SHADOW_MAP_SIZE = 2048;

let active = null;

function setStatus(preview, message, failed = false) {
  const status = preview.root.querySelector('.stl-preview-status');
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle('stl-preview-failed', failed);
}

function makeMovable(preview, handle) {
  const box = preview.root.querySelector('.stl-preview-window');
  let start = null;
  let offset = { x: 0, y: 0 };

  handle.addEventListener('pointerdown', (event) => {
    if (event.target.closest('button')) return;
    start = { x: event.clientX - offset.x, y: event.clientY - offset.y };
    handle.setPointerCapture(event.pointerId);
  });
  handle.addEventListener('pointermove', (event) => {
    if (!start) return;
    // The title bar stays on screen, or a window dragged off the edge can't be grabbed again.
    const { width } = box.getBoundingClientRect();
    const reach = { x: (window.innerWidth + width) / 2 - 60, y: window.innerHeight / 2 };
    offset = {
      x: Math.max(-reach.x, Math.min(reach.x, event.clientX - start.x)),
      y: Math.max(-reach.y, Math.min(reach.y, event.clientY - start.y)),
    };
    box.style.transform = `translate(${offset.x}px, ${offset.y}px)`;
  });
  const stop = () => { start = null; };
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
}

function buildWindow(title) {
  const root = document.createElement('div');
  root.className = 'stl-preview-backdrop';
  root.id = 'stl-preview';
  render(html`
    <div class="stl-preview-window" role="dialog" aria-label="STL preview">
      <div class="stl-preview-header">
        <h2 class="stl-preview-title">${title}</h2>
        <button type="button" class="stl-preview-close" aria-label="Close preview" @click=${closeStlPreview}>&times;</button>
      </div>
      <div class="stl-preview-stage">
        <canvas class="stl-preview-canvas"></canvas>
        <div class="stl-preview-status"></div>
      </div>
    </div>
  `, root);

  // Checks where the press began, because a drag that ends outside the window still fires a click here.
  let pressedOutside = false;
  root.addEventListener('pointerdown', (event) => { pressedOutside = event.target === root; });
  root.addEventListener('click', (event) => {
    if (event.target === root && pressedOutside) closeStlPreview();
  });

  document.body.append(root);
  const preview = { root, abort: new AbortController(), release: null };
  makeMovable(preview, root.querySelector('.stl-preview-header'));
  return preview;
}

async function fetchMesh(preview, sourceType, sourceId) {
  const res = await fetch('/api/preview/stl', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_type: sourceType, id: sourceId }),
    signal: preview.abort.signal,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }

  const total = Number(res.headers.get('Content-Length')) || 0;
  const chunks = [];
  let received = 0;
  const reader = res.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    if (total) setStatus(preview, `Loading ${Math.round((received / total) * 100)}%`);
  }
  const bytes = new Uint8Array(received);
  let at = 0;
  chunks.forEach((chunk) => {
    bytes.set(chunk, at);
    at += chunk.length;
  });
  return bytes.buffer;
}

function placeOnGround(geometry) {
  // STL is Z-up and three.js is Y-up, so a model would otherwise lie on its back.
  geometry.rotateX(-Math.PI / 2);
  geometry.computeBoundingBox();
  const { min, max } = geometry.boundingBox;
  geometry.translate(-(min.x + max.x) / 2, -min.y, -(min.z + max.z) / 2);
  geometry.computeBoundingSphere();
  return { height: max.y - min.y, radius: geometry.boundingSphere.radius || 1 };
}

function addSun(THREE, scene, height, radius) {
  const sun = new THREE.DirectionalLight(SUN_COLOR, 3);
  sun.position.setFromSphericalCoords(
    radius * 6,
    THREE.MathUtils.degToRad(90 - SUN_ELEVATION_DEGREES),
    THREE.MathUtils.degToRad(180 - SUN_AZIMUTH_DEGREES),
  );
  sun.target.position.set(0, height / 2, 0);
  sun.castShadow = true;
  sun.shadow.mapSize.set(SHADOW_MAP_SIZE, SHADOW_MAP_SIZE);
  Object.assign(sun.shadow.camera, {
    left: -radius * 3, right: radius * 3, top: radius * 3, bottom: -radius * 3, near: radius, far: radius * 12,
  });
  sun.shadow.normalBias = radius * 0.004;
  scene.add(sun, sun.target);
  // Sky light, so the side away from the sun reads as shade and not as black.
  scene.add(new THREE.HemisphereLight(0xc4d6e8, 0x4a4034, 1.5));
}

async function showMesh(preview, buffer) {
  const THREE = await import(`${VENDOR}/three.module.js`);
  const { STLLoader } = await import(`${VENDOR}/STLLoader.js`);
  const { OrbitControls } = await import(`${VENDOR}/OrbitControls.js`);
  if (active !== preview) return;

  const canvas = preview.root.querySelector('.stl-preview-canvas');
  const stage = preview.root.querySelector('.stl-preview-stage');
  const geometry = new STLLoader().parse(buffer);
  const { height, radius } = placeOnGround(geometry);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(stage.clientWidth, stage.clientHeight, false);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1a221c);
  const material = new THREE.MeshStandardMaterial({ color: 0xc9c2b4, roughness: 0.65, metalness: 0.05 });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  const groundGeometry = new THREE.CircleGeometry(radius * 12, 64);
  const groundMaterial = new THREE.MeshStandardMaterial({ color: 0x4a5a4e, roughness: 1 });
  const ground = new THREE.Mesh(groundGeometry, groundMaterial);
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(mesh, ground);
  addSun(THREE, scene, height, radius);

  const camera = new THREE.PerspectiveCamera(35, stage.clientWidth / stage.clientHeight, radius / 100, radius * 60);
  camera.position.set(-radius * 1.4, radius * 1.2, radius * 3);
  const controls = new OrbitControls(camera, canvas);
  controls.target.set(0, height / 2, 0);
  // A scene only changes when it's moved, so this draws on demand and leaves the GPU idle.
  const draw = () => renderer.render(scene, camera);
  controls.addEventListener('change', draw);
  controls.update();
  draw();

  preview.release = () => {
    controls.dispose();
    [geometry, material, groundGeometry, groundMaterial].forEach((item) => item.dispose());
    renderer.dispose();
  };
  preview.root.dataset.ready = 'true';
}

/** Closes the open preview, and frees its GPU memory. */
export function closeStlPreview() {
  if (!active) return;
  const preview = active;
  active = null;
  preview.abort.abort();
  preview.release?.();
  preview.root.remove();
}

/** Opens a preview, replacing any that's already open. */
export async function openStlPreview(sourceType, sourceId, title) {
  closeStlPreview();
  const preview = buildWindow(title || 'STL Preview');
  active = preview;
  setStatus(preview, 'Loading');
  try {
    const buffer = await fetchMesh(preview, sourceType, sourceId);
    if (active !== preview) return;
    setStatus(preview, 'Building the mesh');
    // One frame, so that message paints before a large file holds the thread.
    await new Promise((resolve) => { requestAnimationFrame(resolve); });
    await showMesh(preview, buffer);
    if (active === preview) setStatus(preview, '');
  } catch (error) {
    if (error.name === 'AbortError' || active !== preview) return;
    console.error('STL preview failed:', error);
    setStatus(preview, error.message || 'This file could not be shown.', true);
  }
}
