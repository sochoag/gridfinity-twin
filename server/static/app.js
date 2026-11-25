import * as THREE from 'three';
import { OrbitControls } from '/vendor/three/examples/OrbitControls.js';
import { STLLoader } from '/vendor/three/examples/STLLoader.js';

const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const JSON_HDR = { 'Content-Type': 'application/json' };

const S = { st: null, sel: new Set(), paperName: 'A4', pw: 297, ph: 210, view: 'home', viewer: null, rectStamp: 0 };
window.__twin = S; // útil para depurar desde la consola

/* ------------------------------------------------------------------ utilidades */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (typeof j.detail === 'string') msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join(' · ');
    } catch { /* cuerpo no JSON */ }
    throw new Error(msg);
  }
  return r.json();
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), isErr ? 7000 : 3500);
}

async function busy(text, fn) {
  $('busy-text').textContent = text;
  $('busy').classList.remove('hidden');
  try {
    return await fn();
  } catch (e) {
    toast(e.message || String(e), true);
    return undefined;
  } finally {
    $('busy').classList.add('hidden');
  }
}

const svgEl = (tag, attrs = {}) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};
const pathD = (ring) => 'M' + ring.map((p) => `${p[0]},${p[1]}`).join(' L') + ' Z';

/* ------------------------------------------------------------------ navegación */
const VIEWS = ['home', 'corners', 'shape', 'bin'];
function updateSteps() {
  const st = S.st;
  const enabled = { home: true, corners: !!st, shape: !!(st && st.contours), bin: !!(st && st.contours) };
  document.querySelectorAll('.step').forEach((b) => {
    b.disabled = !enabled[b.dataset.step];
    b.classList.toggle('active', b.dataset.step === S.view);
  });
}
function go(view) {
  S.view = view;
  VIEWS.forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
  updateSteps();
  if (view === 'home') refreshAssets();
  if (view === 'corners') renderCorners();
  if (view === 'shape') renderShape();
  if (view === 'bin') renderBin();
  window.scrollTo({ top: 0 });
}
document.querySelectorAll('.step').forEach((b) => b.addEventListener('click', () => go(b.dataset.step)));
$('brand').addEventListener('click', (e) => { e.preventDefault(); go('home'); });

/* ------------------------------------------------------------------ inicio: subir foto */
const drop = $('drop');
const fileInput = $('file');
fileInput.addEventListener('change', () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ''; });
drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) upload(f); });
$('home-paper').addEventListener('change', () => $('home-custom').classList.toggle('hidden', $('home-paper').value !== 'custom'));

async function upload(file) {
  if (!/\.(jpe?g|png|heic)$/i.test(file.name) && !/^image\//.test(file.type)) return toast('Elige una imagen JPG o PNG', true);
  if (file.size > 15 * 1024 * 1024) return toast('La imagen supera 15 MB', true);
  const fd = new FormData();
  fd.append('file', file);
  const paper = $('home-paper').value;
  fd.append('paper', paper);
  if (paper === 'custom') { fd.append('paper_width_mm', $('home-pw').value); fd.append('paper_height_mm', $('home-ph').value); }
  const st = await busy('Subiendo la foto y buscando la hoja…', () => api('/api/sessions', { method: 'POST', body: fd }));
  if (!st) return;
  S.st = st;
  S.sel = new Set();
  go('corners');
}

/* ------------------------------------------------------------------ mis assets */
async function refreshAssets() {
  let list = [];
  try { list = await api('/api/sessions'); } catch (e) { toast(e.message, true); }
  const box = $('assets');
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<p class="muted">Aún no hay nada guardado.</p>'; return; }
  for (const a of list) {
    const card = document.createElement('div');
    card.className = 'asset';
    const img = document.createElement('img');
    img.className = 'thumb'; img.loading = 'lazy'; img.alt = a.name;
    img.src = `/api/sessions/${a.id}/source.jpg`;
    img.addEventListener('click', () => openSession(a.id));
    const meta = document.createElement('div');
    meta.className = 'meta';
    const name = document.createElement('input');
    name.className = 'name'; name.value = a.name; name.maxLength = 80; name.setAttribute('aria-label', 'Nombre');
    name.addEventListener('change', async () => {
      const v = name.value.trim();
      if (!v) { name.value = a.name; return; }
      try { await api(`/api/sessions/${a.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: v }) }); a.name = v; } catch (e) { toast(e.message, true); }
    });
    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = a.traced ? `${a.n_components} objeto${a.n_components === 1 ? '' : 's'} · ${a.has_bin ? 'bin listo' : 'sin bin'}` : 'sin trazar';
    const acts = document.createElement('div');
    acts.className = 'acts';
    const open = document.createElement('button');
    open.className = 'btn small primary'; open.textContent = 'Abrir';
    open.addEventListener('click', () => openSession(a.id));
    const del = document.createElement('button');
    del.className = 'btn small danger'; del.textContent = 'Eliminar';
    let armed = false, t;
    del.addEventListener('click', async () => {
      if (!armed) { armed = true; del.textContent = '¿Seguro?'; t = setTimeout(() => { armed = false; del.textContent = 'Eliminar'; }, 3000); return; }
      clearTimeout(t);
      try { await api(`/api/sessions/${a.id}`, { method: 'DELETE' }); refreshAssets(); } catch (e) { toast(e.message, true); }
    });
    acts.append(open, del);
    meta.append(name, sub, acts);
    card.append(img, meta);
    box.append(card);
  }
}

async function openSession(id) {
  const st = await busy('Abriendo…', () => api(`/api/sessions/${id}`));
  if (!st) return;
  S.st = st;
  const ids = st.contours ? st.contours.components.map((c) => c.id) : [];
  const saved = st.bin_params && st.bin_params.components;
  S.sel = new Set(saved && saved.length ? saved.filter((i) => ids.includes(i)) : ids);
  go(st.bin_meta ? 'bin' : st.contours ? 'shape' : 'corners');
}

/* ------------------------------------------------------------------ paso 2: esquinas del papel */
function setPaper(name, w, h) {
  S.paperName = name; S.pw = w; S.ph = h;
  document.querySelectorAll('#paper-seg button').forEach((b) => {
    const on = b.dataset.paper === name;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', on);
  });
  $('pw').value = w; $('ph').value = h;
  $('pw').disabled = $('ph').disabled = name !== 'custom';
}
document.querySelectorAll('#paper-seg button').forEach((b) => b.addEventListener('click', () => {
  const p = b.dataset.paper;
  if (p === 'A4') setPaper('A4', 297, 210);
  else if (p === 'LETTER') setPaper('LETTER', 279.4, 215.9);
  else setPaper('custom', +$('pw').value || 297, +$('ph').value || 210);
}));
$('pw').addEventListener('change', () => { S.pw = +$('pw').value; });
$('ph').addEventListener('change', () => { S.ph = +$('ph').value; });
$('name').addEventListener('change', async () => {
  const v = $('name').value.trim();
  if (!v || !S.st) return;
  try { await api(`/api/sessions/${S.st.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: v }) }); S.st.name = v; } catch (e) { toast(e.message, true); }
});

function renderCorners() {
  const st = S.st;
  const { width: W, height: H } = st.source;
  const svg = $('corner-svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.replaceChildren();
  svg.append(svgEl('image', { href: `/api/sessions/${st.id}/source.jpg`, width: W, height: H }));
  const quad = svgEl('polygon', { class: 'paper-quad' });
  svg.append(quad);
  const draw = () => quad.setAttribute('points', st.corners.map((c) => c.join(',')).join(' '));
  draw();
  const r = Math.max(W, H) * 0.012;
  const labels = ['TL', 'TR', 'BR', 'BL'];
  st.corners.forEach((c, i) => {
    const h = svgEl('circle', { class: 'corner-handle', r, cx: c[0], cy: c[1], tabindex: 0, 'aria-label': `Esquina ${labels[i]}`, role: 'slider' });
    h.addEventListener('pointerdown', (e) => { h.setPointerCapture(e.pointerId); h.dataset.drag = '1'; e.preventDefault(); });
    h.addEventListener('pointermove', (e) => {
      if (h.dataset.drag !== '1') return;
      const pt = svg.createSVGPoint();
      pt.x = e.clientX; pt.y = e.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const x = Math.min(W, Math.max(0, p.x)), y = Math.min(H, Math.max(0, p.y));
      st.corners[i] = [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
      h.setAttribute('cx', x); h.setAttribute('cy', y);
      draw();
    });
    const end = () => { h.dataset.drag = '0'; };
    h.addEventListener('pointerup', end); h.addEventListener('pointercancel', end);
    h.addEventListener('keydown', (e) => { // accesibilidad: flechas mueven 2 px (shift: 10)
      const d = e.shiftKey ? 10 : 2;
      const m = { ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] }[e.key];
      if (!m) return;
      e.preventDefault();
      st.corners[i] = [Math.min(W, Math.max(0, st.corners[i][0] + m[0])), Math.min(H, Math.max(0, st.corners[i][1] + m[1]))];
      h.setAttribute('cx', st.corners[i][0]); h.setAttribute('cy', st.corners[i][1]);
      draw();
    });
    svg.append(h);
  });
  $('name').value = st.name;
  $('corner-note').classList.toggle('hidden', st.corners_detected !== false);
  setPaper(st.paper.name || 'A4', st.paper.width_mm, st.paper.height_mm);
}

function paperBody() {
  const b = { paper: S.paperName };
  if (S.paperName === 'custom') { b.paper_width_mm = S.pw; b.paper_height_mm = S.ph; }
  return b;
}

async function trace(extra = {}) {
  const st = S.st;
  const body = { corners: st.corners, ...paperBody(), ...extra };
  const c = await busy('Trazando la silueta…', () => api(`/api/sessions/${st.id}/trace`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify(body) }));
  if (!c) return false;
  st.contours = c;
  st.bin_meta = null;
  S.rectStamp = Date.now();
  S.sel = new Set(c.components.map((k) => k.id));
  return true;
}
$('btn-trace').addEventListener('click', async () => { if (await trace()) go('shape'); });
$('btn-other').addEventListener('click', () => go('home'));

/* ------------------------------------------------------------------ paso 3: silueta */
$('simp').addEventListener('input', () => { $('simp-out').textContent = (+$('simp').value).toFixed(2); });
$('btn-retrace').addEventListener('click', async () => {
  const ok = await trace({ simplification_mm: +$('simp').value, keep_holes: $('keep-holes-trace').checked });
  if (ok) renderShape();
});
$('btn-back-paper').addEventListener('click', () => go('corners'));
$('btn-to-bin').addEventListener('click', () => {
  if (!S.sel.size) return toast('Selecciona al menos un objeto', true);
  go('bin');
});

function toggleComp(id) {
  if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  S.st.bin_meta = null; // la selección cambia el bin: hay que regenerarlo
  paintSelection();
}
function paintSelection() {
  document.querySelectorAll('#shape-svg .comp').forEach((p) => p.classList.toggle('off', !S.sel.has(+p.dataset.id)));
  document.querySelectorAll('#comp-list li').forEach((li) => {
    li.classList.toggle('off', !S.sel.has(+li.dataset.id));
    li.querySelector('input').checked = S.sel.has(+li.dataset.id);
  });
}

function renderShape() {
  const st = S.st, c = st.contours;
  if (!c) return go('corners');
  const pw = c.paper.width_mm, ph = c.paper.height_mm;
  const svg = $('shape-svg');
  svg.setAttribute('viewBox', `0 0 ${pw} ${ph}`);
  svg.replaceChildren();
  svg.append(svgEl('image', { href: `/api/sessions/${st.id}/rectified.jpg?t=${S.rectStamp || st.created}`, width: pw, height: ph, preserveAspectRatio: 'none' }));
  const list = $('comp-list');
  list.replaceChildren();
  for (const k of c.components) {
    const p = svgEl('path', { class: 'comp', 'data-id': k.id, d: [k.outer, ...k.holes].map(pathD).join(' ') });
    p.addEventListener('click', () => toggleComp(k.id));
    svg.append(p);
    const t = svgEl('text', { class: 'comp-label', x: k.bbox.x + k.bbox.width / 2, y: k.bbox.y + k.bbox.height / 2, 'text-anchor': 'middle', 'dominant-baseline': 'middle', 'font-size': ph * 0.04 });
    t.textContent = k.id + 1;
    svg.append(t);
    const li = document.createElement('li');
    li.dataset.id = k.id;
    li.innerHTML = `<input type="checkbox" checked tabindex="-1"><span class="num">${k.id + 1}</span><span>${k.bbox.width.toFixed(0)} × ${k.bbox.height.toFixed(0)} mm${k.holes.length ? ` · ${k.holes.length} hueco${k.holes.length > 1 ? 's' : ''}` : ''}</span>`;
    li.addEventListener('click', (e) => { if (e.target.tagName !== 'INPUT') toggleComp(k.id); else toggleComp(k.id); });
    list.append(li);
  }
  paintSelection();
}

/* ------------------------------------------------------------------ paso 4: bin + vista 3D */
const BIN_DEFAULTS = { components: null, grid_width: null, grid_depth: null, grid_unit_mm: 42, height_units: 4, clearance_mm: 1, cutout_depth_mm: 12, stacking_lip: true, magnet_holes: false, magnet_diameter_mm: 6.5, magnet_depth_mm: 2.4, finger_notches: 'none', finger_diameter_mm: 16, keep_holes: false };
const num = (id) => +$(id).value;

function fillBinForm(p) {
  const auto = p.grid_width == null || p.grid_depth == null;
  $('b-auto').checked = auto;
  $('b-gw').value = p.grid_width ?? 3; $('b-gd').value = p.grid_depth ?? 3;
  $('b-gw').disabled = $('b-gd').disabled = auto;
  $('b-hu').value = p.height_units; $('b-unit').value = p.grid_unit_mm;
  $('b-clr').value = p.clearance_mm; $('b-depth').value = p.cutout_depth_mm;
  $('b-finger').value = p.finger_notches; $('b-fd').value = p.finger_diameter_mm;
  $('b-holes').checked = p.keep_holes; $('b-lip').checked = p.stacking_lip;
  $('b-mag').checked = p.magnet_holes; $('b-md').value = p.magnet_diameter_mm; $('b-mdep').value = p.magnet_depth_mm;
  $('b-md').disabled = $('b-mdep').disabled = !p.magnet_holes;
}
function readBinForm() {
  const auto = $('b-auto').checked;
  return {
    components: [...S.sel].sort((a, b) => a - b),
    grid_width: auto ? null : num('b-gw'), grid_depth: auto ? null : num('b-gd'),
    grid_unit_mm: num('b-unit'), height_units: num('b-hu'),
    clearance_mm: num('b-clr'), cutout_depth_mm: num('b-depth'),
    finger_notches: $('b-finger').value, finger_diameter_mm: num('b-fd'), keep_holes: $('b-holes').checked,
    stacking_lip: $('b-lip').checked, magnet_holes: $('b-mag').checked,
    magnet_diameter_mm: num('b-md'), magnet_depth_mm: num('b-mdep'),
  };
}
$('b-auto').addEventListener('change', () => { $('b-gw').disabled = $('b-gd').disabled = $('b-auto').checked; });
$('b-mag').addEventListener('change', () => { $('b-md').disabled = $('b-mdep').disabled = !$('b-mag').checked; });
$('btn-back-shape').addEventListener('click', () => go('shape'));
$('btn-build').addEventListener('click', () => buildBin());

function setDownloads(enabled) {
  const id = S.st.id;
  for (const [el, ext] of [[$('dl-stl'), 'stl'], [$('dl-svg'), 'svg']]) {
    el.classList.toggle('disabled', !enabled);
    el.href = enabled ? `/api/sessions/${id}/bin.${ext}` : '#';
  }
}

function showInfo(m) {
  $('bin-info').innerHTML = [
    `<span>Rejilla <b>${m.grid[0]} × ${m.grid[1]}</b></span>`,
    `<span>Tamaño <b>${m.size_mm[0]} × ${m.size_mm[1]} × ${m.total_height_mm} mm</b></span>`,
    m.mesh ? `<span>Malla <b>${m.mesh.watertight ? 'cerrada ✓' : 'con errores ✗'}</b>${m.mesh.volume_cm3 ? ` · ${m.mesh.volume_cm3} cm³` : ''}</span>` : '',
    `<span class="muted">${m.build_seconds} s</span>`,
  ].join('');
  const w = $('bin-warn');
  w.replaceChildren(...m.warnings.map((t) => { const li = document.createElement('li'); li.textContent = t; return li; }));
}

async function buildBin() {
  if (!S.sel.size) return toast('Selecciona al menos un objeto', true);
  const st = S.st;
  const params = readBinForm();
  const meta = await busy('Generando el bin…', () => api(`/api/sessions/${st.id}/bin`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify(params) }));
  if (!meta) return;
  st.bin_meta = meta; st.bin_params = params;
  showInfo(meta);
  setDownloads(true);
  await loadStl(`/api/sessions/${st.id}/bin.stl?t=${meta.stamp}`, true);
}

function renderBin() {
  const st = S.st;
  if (!S.sel.size) return go('shape');
  fillBinForm({ ...BIN_DEFAULTS, ...(st.bin_params || {}) });
  initViewer();
  if (st.bin_meta) {
    showInfo(st.bin_meta); setDownloads(true);
    loadStl(`/api/sessions/${st.id}/bin.stl?t=${st.bin_meta.stamp}`, true);
  } else {
    $('bin-info').innerHTML = ''; $('bin-warn').replaceChildren(); setDownloads(false);
    clearMesh();
    buildBin();
  }
}

/* ---- three.js */
function initViewer() {
  if (S.viewer) { S.viewer.resize(); return; }
  const el = $('viewer');
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (e) {
    el.innerHTML = '<p style="padding:20px;color:#6b6b70">Tu navegador no pudo iniciar WebGL, así que no hay vista previa 3D. Puedes descargar el STL igualmente.</p>';
    S.viewer = { noGL: true, resize() {}, load() {}, clear() {} };
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  el.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
  camera.up.set(0, 0, 1);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8478, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.7); key.position.set(120, -160, 260); scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.5); fill.position.set(-200, 120, 80); scene.add(fill);
  const grid = new THREE.GridHelper(600, 60, 0xb9b5ab, 0xd2cec5);
  grid.rotation.x = Math.PI / 2; grid.position.z = -0.05;
  scene.add(grid);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const mat = new THREE.MeshStandardMaterial({ color: 0xd4651b, roughness: 0.55, metalness: 0.05 });
  const loader = new STLLoader();
  let mesh = null;
  const v = {
    renderer, scene, camera, controls,
    resize() {
      const w = el.clientWidth, h = el.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    },
    clear() { if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); mesh = null; } },
    load(url, fit) {
      return new Promise((resolve, reject) => loader.load(url, (g) => {
        v.clear();
        g.computeBoundingBox();
        mesh = new THREE.Mesh(g, mat);
        scene.add(mesh);
        if (fit) {
          const b = g.boundingBox, size = b.getSize(new THREE.Vector3());
          const d = Math.max(size.x, size.y, size.z) * 2.1;
          controls.target.set(0, 0, size.z / 2);
          camera.position.set(d * 0.75, -d * 0.95, d * 0.8);
          camera.near = d / 100; camera.far = d * 20; camera.updateProjectionMatrix();
          controls.update();
        }
        resolve();
      }, undefined, reject));
    },
  };
  new ResizeObserver(() => v.resize()).observe(el);
  v.resize();
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
  S.viewer = v;
}
function clearMesh() { if (S.viewer) S.viewer.clear(); }
async function loadStl(url, fit) {
  if (!S.viewer || S.viewer.noGL) return;
  try { await S.viewer.load(url, fit); } catch (e) { toast('No se pudo cargar la vista previa 3D', true); }
}

/* ------------------------------------------------------------------ arranque */
updateSteps();
go('home');
