import * as THREE from 'three';
import { OrbitControls } from '/vendor/three/examples/OrbitControls.js';
import { STLLoader } from '/vendor/three/examples/STLLoader.js';

const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const JSON_HDR = { 'Content-Type': 'application/json' };

const S = { bin: 'b1', st: null, sel: new Set(), paperName: 'A4', pw: 297, ph: 210, view: 'home', viewer: null, rectStamp: 0 };
window.__twin = S; // útil para depurar desde la consola
setTimeout(() => { window.__twinfn = { D, CFG, SHAPES, renderLayout, renderSettings, scheduleBuild, bboxOf, undo, redo, switchBin }; });

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
  $('main').classList.toggle('wide', view === 'bin');
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
  S.st = st; S.bin = 'b1';
  const ids = st.contours ? st.contours.components.map((c) => c.id) : [];
  const saved = st.design && st.design.shapes.map((x) => x.comp).filter((x) => x != null);
  S.sel = new Set(saved && saved.length ? saved.filter((i) => ids.includes(i)) : ids);
  go(st.design ? 'bin' : st.contours ? 'shape' : 'corners');
}

/* ------------------------------------------------------------------ paso 2: esquinas del papel */
function setPaper(name, w, h) {
  S.paperName = name; S.pw = w; S.ph = h;
  document.querySelectorAll('#paper-seg button').forEach((b) => {
    const on = b.dataset.paper === name;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', on);
  });
  if (w && h) { $('pw').value = w; $('ph').value = h; }
  $('pw').disabled = $('ph').disabled = name !== 'custom';
  $('measure-box').classList.toggle('hidden', name !== 'measure');
  $('preset-box').classList.toggle('hidden', name === 'measure');
  document.querySelector('#view-corners .row').classList.toggle('hidden', name === 'measure');
  if (S.st && $('corner-svg').querySelector('image')) cornerLayer();
}
document.querySelectorAll('#paper-seg button').forEach((b) => b.addEventListener('click', () => {
  const p = b.dataset.paper;
  if (p === 'A4') setPaper('A4', 297, 210);
  else if (p === 'LETTER') setPaper('LETTER', 279.4, 215.9);
  else if (p === 'measure') setPaper('measure', S.pw, S.ph);
  else setPaper('custom', +$('pw').value || 297, +$('ph').value || 210);
}));
$('pw').addEventListener('change', () => { S.pw = +$('pw').value; });
$('ph').addEventListener('change', () => { S.ph = +$('ph').value; });
$('name').addEventListener('change', async () => {
  const v = $('name').value.trim();
  if (!v || !S.st) return;
  try { await api(`/api/sessions/${S.st.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: v }) }); S.st.name = v; } catch (e) { toast(e.message, true); }
});

const MEAS = { p1: null, p2: null };
function renderCorners() {
  const st = S.st;
  const { width: W, height: H } = st.source;
  const svg = $('corner-svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.replaceChildren();
  svg.append(svgEl('image', { href: `/api/sessions/${st.id}/source.jpg`, width: W, height: H }));
  MEAS.p1 = [W * 0.35, H * 0.5]; MEAS.p2 = [W * 0.65, H * 0.5];
  $('name').value = st.name;
  $('corner-note').classList.toggle('hidden', st.corners_detected !== false);
  setPaper(st.paper.name || 'A4', st.paper.width_mm, st.paper.height_mm);
  cornerLayer();
}
const svgPoint = (svg, e) => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; return pt.matrixTransform(svg.getScreenCTM().inverse()); };
function measInfo() {
  const mm = +$('meas-mm').value, px = Math.hypot(MEAS.p2[0] - MEAS.p1[0], MEAS.p2[1] - MEAS.p1[1]);
  const { width: W, height: H } = S.st.source;
  $('meas-info').textContent = mm > 0 && px > 5 ? `${(px / mm).toFixed(2)} px/mm · la foto cubre ${(W / (px / mm)).toFixed(0)} × ${(H / (px / mm)).toFixed(0)} mm` : '';
}
function cornerLayer() {
  const st = S.st, svg = $('corner-svg');
  const { width: W, height: H } = st.source;
  svg.querySelectorAll('.cl').forEach((n) => n.remove());
  const r = Math.max(W, H) * 0.012;
  if (S.paperName === 'measure') {
    const line = svgEl('line', { class: 'cl meas-line', 'stroke-width': r * 0.35 });
    const draw = () => { line.setAttribute('x1', MEAS.p1[0]); line.setAttribute('y1', MEAS.p1[1]); line.setAttribute('x2', MEAS.p2[0]); line.setAttribute('y2', MEAS.p2[1]); measInfo(); };
    svg.append(line);
    ['p1', 'p2'].forEach((k, i) => {
      const h = svgEl('circle', { class: 'cl corner-handle', r, cx: MEAS[k][0], cy: MEAS[k][1], tabindex: 0, 'aria-label': `Marcador ${i + 1}`, role: 'slider' });
      h.addEventListener('pointerdown', (e) => { h.setPointerCapture(e.pointerId); h.dataset.drag = '1'; e.preventDefault(); });
      h.addEventListener('pointermove', (e) => {
        if (h.dataset.drag !== '1') return;
        const p = svgPoint(svg, e);
        MEAS[k] = [Math.min(W, Math.max(0, p.x)), Math.min(H, Math.max(0, p.y))];
        h.setAttribute('cx', MEAS[k][0]); h.setAttribute('cy', MEAS[k][1]); draw();
      });
      const end = () => { h.dataset.drag = '0'; };
      h.addEventListener('pointerup', end); h.addEventListener('pointercancel', end);
      h.addEventListener('keydown', (e) => {
        const d = e.shiftKey ? 10 : 2, m = { ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] }[e.key];
        if (!m) return;
        e.preventDefault();
        MEAS[k] = [Math.min(W, Math.max(0, MEAS[k][0] + m[0])), Math.min(H, Math.max(0, MEAS[k][1] + m[1]))];
        h.setAttribute('cx', MEAS[k][0]); h.setAttribute('cy', MEAS[k][1]); draw();
      });
      svg.append(h);
    });
    draw();
    return;
  }
  const quad = svgEl('polygon', { class: 'cl paper-quad' });
  svg.append(quad);
  const draw = () => quad.setAttribute('points', st.corners.map((c) => c.join(',')).join(' '));
  draw();
  const labels = ['TL', 'TR', 'BR', 'BL'];
  st.corners.forEach((c, i) => {
    const h = svgEl('circle', { class: 'cl corner-handle', r, cx: c[0], cy: c[1], tabindex: 0, 'aria-label': `Esquina ${labels[i]}`, role: 'slider' });
    h.addEventListener('pointerdown', (e) => { h.setPointerCapture(e.pointerId); h.dataset.drag = '1'; e.preventDefault(); });
    h.addEventListener('pointermove', (e) => {
      if (h.dataset.drag !== '1') return;
      const p = svgPoint(svg, e);
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
}
$('meas-mm').addEventListener('input', () => { $('meas-preset').value = ''; measInfo(); });
$('meas-preset').addEventListener('change', () => { if ($('meas-preset').value) { $('meas-mm').value = $('meas-preset').value; measInfo(); } });
$('paper-preset').addEventListener('change', () => {
  const v = $('paper-preset').value;
  if (!v) return;
  const [w, h] = v.split('x').map(Number);
  setPaper('custom', w, h);
  $('paper-preset').value = '';
});

function paperBody() {
  if (S.paperName === 'measure') {
    const { width: W, height: H } = S.st.source;
    const mm = +$('meas-mm').value, px = Math.hypot(MEAS.p2[0] - MEAS.p1[0], MEAS.p2[1] - MEAS.p1[1]);
    if (!(mm > 0) || px < 10) throw new Error('Pon dos marcadores separados y una distancia real mayor que 0');
    const s = px / mm;
    S.st.corners = [[0, 0], [W, 0], [W, H], [0, H]];
    return { paper: 'custom', paper_width_mm: +(Math.max(W, H) / s).toFixed(2), paper_height_mm: +(Math.min(W, H) / s).toFixed(2) };
  }
  const b = { paper: S.paperName };
  if (S.paperName === 'custom') { b.paper_width_mm = S.pw; b.paper_height_mm = S.ph; }
  return b;
}

async function trace(extra = {}) {
  const st = S.st;
  let pb;
  try { pb = paperBody(); } catch (e) { toast(e.message, true); return false; }
  const body = { corners: st.corners, ...pb, ...extra };
  const c = await busy('Trazando la silueta…', () => api(`/api/sessions/${st.id}/trace`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify(body) }));
  if (!c) return false;
  st.contours = c;
  st.bin_meta = null; st.design = null; st.bins = {}; S.bin = 'b1';
  S.rectStamp = Date.now();
  S.sel = new Set(c.components.map((k) => k.id));
  return true;
}
$('btn-trace').addEventListener('click', async () => { if (await trace()) go('shape'); });
$('btn-other').addEventListener('click', () => go('home'));

/* ------------------------------------------------------------------ paso 3: silueta */
$('simp').addEventListener('input', () => { $('simp-out').textContent = (+$('simp').value).toFixed(2); });
$('btn-retrace').addEventListener('click', async () => {
  if ((S.st.mask_edits || []).length) { await sendEdits(S.st.mask_edits); return; }   // conserva las ediciones manuales
  const ok = await trace({ simplification_mm: +$('simp').value, keep_holes: $('keep-holes-trace').checked });
  if (ok) { S.st.mask_edits = []; renderShape(); }
});

/* ---- edicion manual de la silueta: pincel / borrador / lazo sobre la mascara automatica */
const MT = { tool: 'select', size: 6, cur: null };
async function sendEdits(edits) {
  const st = S.st;
  try {
    const res = await busy('Aplicando…', () => api(`/api/sessions/${st.id}/mask`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify({ edits, simplification_mm: +$('simp').value, keep_holes: $('keep-holes-trace').checked }) }));
    if (!res || S.st !== st) return false;
    st.contours = res.contours; st.mask_edits = res.edits;
    st.bin_meta = null; st.design = null;
    S.rectStamp = S.rectStamp || st.created;
    S.sel = new Set(res.contours.components.map((k) => k.id));
    renderShape();
    return true;
  } catch (e) { toast(e.message, true); renderShape(); return false; }
}
function setMTool(t) {
  MT.tool = t;
  document.querySelectorAll('#brush-bar [data-mt]').forEach((b) => b.classList.toggle('on', b.dataset.mt === t));
  $('shape-svg').classList.toggle('brushing', t !== 'select');
  $('brush-size-l').classList.toggle('hidden', t.startsWith('lasso') || t === 'select');
}
document.querySelectorAll('#brush-bar [data-mt]').forEach((b) => b.addEventListener('click', () => setMTool(b.dataset.mt)));
$('brush-size').addEventListener('input', () => { MT.size = +$('brush-size').value; $('brush-size-o').textContent = MT.size; });
$('brush-undo').addEventListener('click', () => { const e = (S.st.mask_edits || []).slice(0, -1); sendEdits(e); });
$('brush-clear').addEventListener('click', () => sendEdits([]));
(() => {
  const svg = $('shape-svg');
  svg.addEventListener('pointerdown', (e) => {
    if (MT.tool === 'select' || e.button !== 0) return;
    e.preventDefault();
    svg.setPointerCapture(e.pointerId);
    const p = svgPoint(svg, e);
    const lasso = MT.tool.startsWith('lasso');
    const op = MT.tool.endsWith('erase') ? 'erase' : 'add';
    const el = svgEl(lasso ? 'polygon' : 'polyline', { class: `stroke-prev ${op}`, points: `${p.x},${p.y}`, 'stroke-width': lasso ? 0.6 : MT.size * 2 });
    svg.append(el);
    MT.cur = { op, kind: lasso ? 'lasso' : 'brush', r: MT.size, pts: [[r2(p.x), r2(p.y)]], el };
  });
  svg.addEventListener('pointermove', (e) => {
    const c = MT.cur;
    if (!c) return;
    const p = svgPoint(svg, e), l = c.pts[c.pts.length - 1];
    if (Math.hypot(p.x - l[0], p.y - l[1]) < (c.kind === 'lasso' ? 1.2 : Math.max(0.6, c.r * 0.18))) return;
    c.pts.push([r2(p.x), r2(p.y)]);
    c.el.setAttribute('points', c.pts.map((q) => q.join(',')).join(' '));
  });
  const end = async () => {
    const c = MT.cur;
    if (!c) return;
    MT.cur = null;
    c.el.remove();
    if (c.kind === 'lasso' && c.pts.length < 3) return;
    const edit = { op: c.op, kind: c.kind, r: c.r, pts: c.pts.slice(0, 4000) };
    await sendEdits([...(S.st.mask_edits || []), edit]);
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);
})();
$('btn-back-paper').addEventListener('click', () => go('corners'));
$('btn-to-bin').addEventListener('click', () => {
  if (!S.sel.size) return toast('Selecciona al menos un objeto', true);
  go('bin');
});

function toggleComp(id) {
  if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
  S.st.design = null; S.st.bin_meta = null; S.bin = 'b1'; // la selección cambia el diseño: se vuelve a colocar
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
  for (const e of st.mask_edits || []) {   // ediciones manuales ya aplicadas (tenues)
    svg.append(svgEl(e.kind === 'lasso' ? 'polygon' : 'polyline', { class: `stroke-done ${e.op}`, points: e.pts.map((q) => q.join(',')).join(' '), 'stroke-width': e.kind === 'lasso' ? 0.5 : e.r * 2 }));
  }
  $('brush-undo').disabled = $('brush-clear').disabled = !(st.mask_edits || []).length;
  setMTool(MT.tool);
  paintSelection();
}

/* ------------------------------------------------------------------ paso 4: editor del bin (formas, 2D, 3D, ajustes) */
const DEFAULT_SETTINGS = {
  auto_size: true, grid_width: 4, grid_depth: 4, height_units: 3, grid_unit_mm: 42, pocket_depth_mm: 12, pocket_chamfer: true,
  chamfer_mm: 1, clearance_mm: 1, contrast_insert: true, insert_thickness_mm: 0.4, fit_clearance_mm: 0.2, wall_thickness_mm: 1.6,
  stacking_lip: true, magnet_holes: false, magnet_diameter_mm: 6.5, magnet_depth_mm: 2.4,
  partial: false, cells_off: [], bed_x_mm: 256, bed_y_mm: 256, nozzle_mm: 0.4,
  hollow: false, hollow_rib_mm: 2.4, hollow_floor_mm: 1.2, lid_thickness_mm: 1.2, lid_ledge_mm: 0.8, lid_clearance_mm: 0.2,
  dividers_x: [], dividers_y: [], divider_thickness_mm: 1.2, divider_clearance_mm: 0.2, divider_slot_depth_mm: 0.8,
};
const E = { multi: new Set(), sel: null, tool: null, pts: false, building: false, pending: false, timer: 0, meta: null, drag: null, menu: null };
const D = () => S.st.design;
const CFG = () => D().settings;
const SHAPES = () => D().shapes;
const selShape = () => SHAPES().find((s) => s.id === E.sel) || null;
const compOf = (sh) => S.st.contours.components[sh.comp];
const r2 = (v) => Math.round(v * 100) / 100;
const rad = (d) => (d * Math.PI) / 180;

/* ---- geometría local de una forma (centrada, Y hacia abajo, con espejo) */
function localGeom(sh) {
  if (sh.outer) return { outer: sh.outer, holes: sh.holes || [] };
  const c = compOf(sh);
  const xs = c.outer.map((p) => p[0]), ys = c.outer.map((p) => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const m = sh.mirror ? -1 : 1;
  const f = (ring) => ring.map((p) => [m * (p[0] - cx), p[1] - cy]);
  return { outer: f(c.outer), holes: sh.keep_holes ? c.holes.map(f) : [] };
}
function xf(sh, p) {
  const a = rad(sh.rot), c = Math.cos(a), s = Math.sin(a);
  return [sh.x + p[0] * c - p[1] * s, sh.y + p[0] * s + p[1] * c];
}
function unxf(sh, p) {
  const a = rad(-sh.rot), c = Math.cos(a), s = Math.sin(a), dx = p[0] - sh.x, dy = p[1] - sh.y;
  return [dx * c - dy * s, dx * s + dy * c];
}
function bboxOf(sh) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of localGeom(sh).outer) { const q = xf(sh, p); x0 = Math.min(x0, q[0]); x1 = Math.max(x1, q[0]); y0 = Math.min(y0, q[1]); y1 = Math.max(y1, q[1]); }
  return [x0, y0, x1, y1];
}
const bcenter = (b) => [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
function setRot(sh, rot) { // gira sobre el centro visual de la caja
  const c0 = bcenter(bboxOf(sh));
  sh.rot = ((rot % 360) + 360) % 360;
  const c1 = bcenter(bboxOf(sh));
  sh.x = r2(sh.x + c0[0] - c1[0]); sh.y = r2(sh.y + c0[1] - c1[1]);
}
function rdp(pts, eps) { // Douglas-Peucker para anillos cerrados
  const n = pts.length;
  if (n <= 8) return pts.map((p) => [...p]);
  const dist = (p, a, b) => {
    const dx = b[0] - a[0], dy = b[1] - a[1], l = dx * dx + dy * dy;
    if (!l) return Math.hypot(p[0] - a[0], p[1] - a[1]);
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l));
    return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
  };
  const rec = (i, j, out) => {
    let md = 0, k = -1;
    for (let q = i + 1; q < j; q++) { const d = dist(pts[q], pts[i], pts[j]); if (d > md) { md = d; k = q; } }
    if (md > eps) { rec(i, k, out); out.push(k); rec(k, j, out); }
  };
  let far = 0, fd = 0;
  for (let q = 1; q < n; q++) { const d = Math.hypot(pts[q][0] - pts[0][0], pts[q][1] - pts[0][1]); if (d > fd) { fd = d; far = q; } }
  const o1 = [0], o2 = [far];
  rec(0, far, o1); rec(far, n - 1, o2); o2.push(n - 1);
  return [...new Set([...o1, ...o2])].sort((a, b) => a - b).map((i) => [r2(pts[i][0]), r2(pts[i][1])]);
}

/* ---- tamaño del bin (de la última construcción o estimado) */
function binGrid() {
  const c = CFG();
  if (!c.auto_size) return [c.grid_width, c.grid_depth];
  if (E.meta) return E.meta.grid;
  const b = groupBBox();
  const u = c.grid_unit_mm, m = 2 * (c.wall_thickness_mm + c.clearance_mm);
  return [Math.max(1, Math.ceil((2 * Math.max(Math.abs(b[0]), Math.abs(b[2])) + m) / u)), Math.max(1, Math.ceil((2 * Math.max(Math.abs(b[1]), Math.abs(b[3])) + m) / u))];
}
function groupBBox() {
  let b = [Infinity, Infinity, -Infinity, -Infinity];
  if (!SHAPES().length) return [0, 0, 0, 0];
  for (const sh of SHAPES()) { const q = bboxOf(sh); b = [Math.min(b[0], q[0]), Math.min(b[1], q[1]), Math.max(b[2], q[2]), Math.max(b[3], q[3])]; }
  return b;
}
function recenterGroup() {
  if (!CFG().auto_size || !SHAPES().length) return;
  const c = bcenter(groupBBox());
  for (const sh of SHAPES()) { sh.x = r2(sh.x - c[0]); sh.y = r2(sh.y - c[1]); }
}

/* ---- guardado / construcción con debounce */
function setStatus(kind, text) {
  const el = $('ed-status');
  el.className = 'status ' + kind;
  el.querySelector('b').textContent = text;
}
/* ---- deshacer / rehacer (instantaneas de formas + ajustes) */
const H = { undo: [], redo: [], cur: null, t: 0 };
const histSnap = () => JSON.stringify({ settings: CFG(), shapes: SHAPES() });
function histReset() { clearTimeout(H.t); H.undo = []; H.redo = []; H.cur = histSnap(); renderHist(); }
function histCommit() {
  clearTimeout(H.t);
  if (!S.st || !D() || H.cur == null) return;
  const s = histSnap();
  if (s !== H.cur) { H.undo.push(H.cur); if (H.undo.length > 100) H.undo.shift(); H.redo = []; H.cur = s; }
  renderHist();
}
function renderHist() {
  if (!$('btn-undo') || !S.st || !D()) return;
  $('btn-undo').disabled = !H.undo.length && H.cur === histSnap();
  $('btn-redo').disabled = !H.redo.length;
}
function histApply(snap) {
  const o = JSON.parse(snap);
  D().settings = { ...DEFAULT_SETTINGS, ...o.settings };
  D().shapes = o.shapes;
  E.sel = null; E.multi.clear(); E.tool = null; E.pts = false; E.meta = null;
  refreshEditor(); scheduleBuild(50);
}
function undo() { histCommit(); if (!H.undo.length) return; H.redo.push(H.cur); H.cur = H.undo.pop(); histApply(H.cur); renderHist(); }
function redo() { histCommit(); if (!H.redo.length) return; H.undo.push(H.cur); H.cur = H.redo.pop(); histApply(H.cur); renderHist(); }
document.addEventListener('keydown', (e) => {
  if (S.view !== 'bin' || !(e.ctrlKey || e.metaKey)) return;
  const tg = e.target;
  if (tg && (tg.tagName === 'INPUT' || tg.tagName === 'TEXTAREA' || tg.isContentEditable)) return;
  const k = e.key.toLowerCase();
  if (k === 'z' && !e.shiftKey) { e.preventDefault(); undo(); }
  else if ((k === 'z' && e.shiftKey) || k === 'y') { e.preventDefault(); redo(); }
});
function scheduleBuild(delay = 450) {
  clearTimeout(H.t); H.t = setTimeout(histCommit, 600);
  clearTimeout(E.timer);
  E.dirty = true;
  setStatus('working', 'Generando…');
  $('ed-saved').textContent = 'Sin guardar…';
  E.timer = setTimeout(buildNow, delay);
}
async function buildNow() {
  if (!S.st || !D()) return;
  if (E.building) { E.pending = true; return; }
  E.building = true;
  E.dirty = false;
  const st = S.st;
  try {
    const meta = await api(`/api/sessions/${st.id}/design?bin=${S.bin}`, { method: 'PUT', headers: JSON_HDR, body: JSON.stringify({ settings: CFG(), shapes: SHAPES() }) });
    if (S.st !== st) return;
    E.meta = st.bin_meta = meta;
    showInfo(meta);
    setDownloads(true);
    if (!E.dirty && !E.pending) { setStatus('ok', 'Listo'); $('ed-saved').textContent = 'Guardado'; }
    renderLayout(); renderSettingsInfo();
    loadModels(meta);
  } catch (e) {
    toast(e.message || String(e), true);
    setStatus('err', 'Error');
  } finally {
    E.building = false;
    if (E.pending) { E.pending = false; buildNow(); }
  }
}

function showInfo(m) {
  $('bin-info').innerHTML = [
    `<span>Grid <b>${m.grid[0]} × ${m.grid[1]}</b></span>`,
    `<span>Size <b>${m.size_mm[0]} × ${m.size_mm[1]} × ${m.total_height_mm} mm</b></span>`,
    m.mesh ? `<span>Mesh <b>${m.mesh.watertight ? 'cerrada ✓' : 'con errores ✗'}</b>${m.mesh.volume_cm3 ? ` · ${m.mesh.volume_cm3} cm³` : ''}</span>` : '',
    m.has_lid && m.stats && m.stats.solid_cm3 ? `<span>Hueco <b>${m.stats.solid_cm3} → ${m.stats.bin_cm3} cm³ (−${Math.round((1 - m.stats.bin_cm3 / m.stats.solid_cm3) * 100)} %)</b> · tapa ${m.stats.lid_cm3} cm³</span>` : '',
    `<span class="muted">${m.build_seconds} s</span>`,
  ].join('');
  const w = $('bin-warn');
  w.replaceChildren(...m.warnings.map((t) => { const li = document.createElement('li'); li.textContent = t; return li; }));
  renderChecks(m.checks || []);
}
function renderChecks(list) {
  const box = $('checks'), warns = list.filter((c) => c.level === 'warn'), infos = list.filter((c) => c.level !== 'warn');
  $('checks-h').textContent = warns.length ? `Printability · ${warns.length} aviso${warns.length > 1 ? 's' : ''}` : 'Printability · todo en orden ✓';
  box.replaceChildren(...[...warns, ...infos].map((c) => {
    const li = document.createElement('li');
    li.className = c.level; li.textContent = c.msg;
    if (c.shapes && c.shapes.length) {
      li.classList.add('click'); li.title = 'Seleccionar las formas implicadas';
      li.addEventListener('click', () => { E.sel = c.shapes[0]; renderPop(); renderToolbar(); renderLayout(); });
    }
    return li;
  }));
}
function setDownloads(on) {
  const id = S.st.id, b = S.bin || 'b1';
  for (const [el, path] of [[$('dl-zip'), `bin.zip?bin=${b}`], [$('dl-3mf'), `bin.3mf?bin=${b}`], [$('dl-svg'), `bin.svg?bin=${b}`]]) {
    el.classList.toggle('disabled', !on);
    el.href = on ? `/api/sessions/${id}/${path}` : '#';
  }
  const many = binList().length > 1;
  $('dl-all-wrap').classList.toggle('hidden', !many);
  $('dl-all-zip').href = `/api/sessions/${id}/bin.zip?all=1`;
  $('dl-all-3mf').href = `/api/sessions/${id}/bin.3mf?all=1`;
}

/* ---- pestañas de formas */
function closeMenu() { if (E.menu) { E.menu.remove(); E.menu = null; } }
function openMenu(anchor, items) {
  closeMenu();
  const m = document.createElement('div');
  m.className = 'menu';
  for (const it of items) {
    if (it === '-') { m.append(document.createElement('hr')); continue; }
    const b = document.createElement('button');
    b.textContent = it.label;
    if (it.danger) b.className = 'danger';
    b.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); it.fn(); });
    m.append(b);
  }
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  m.style.left = Math.min(window.innerWidth - m.offsetWidth - 8, r.left) + 'px';
  m.style.top = (r.bottom + 6 + window.scrollY) + 'px';
  E.menu = m;
}
document.addEventListener('click', (e) => { if (E.menu && !E.menu.contains(e.target)) closeMenu(); });

function renderTabs() {
  const box = $('ed-tabs');
  box.replaceChildren();
  const dd = document.createElement('button');
  dd.className = 'tab-dd';
  dd.textContent = `Shapes (${SHAPES().length}) ▾`;
  dd.addEventListener('click', (e) => { e.stopPropagation(); openMenu(dd, SHAPES().map((s) => ({ label: s.name, fn: () => selectShape(s.id) }))); });
  box.append(dd);
  for (const sh of SHAPES()) {
    const t = document.createElement('div');
    t.className = 'tab' + (sh.id === E.sel ? ' on' : '');
    const name = document.createElement('span');
    name.textContent = sh.name;
    name.addEventListener('click', () => selectShape(sh.id));
    const more = document.createElement('button');
    more.className = 'tab-more'; more.textContent = '···'; more.title = 'Más opciones'; more.setAttribute('aria-label', `Opciones de ${sh.name}`);
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      openMenu(more, [
        { label: 'Renombrar', fn: () => renameShape(sh, name) },
        { label: 'Duplicar', fn: () => addShape(sh.comp, sh) },
        { label: 'Guardar en la biblioteca…', fn: () => openSaveToLibrary(sh) },
        '-',
        { label: 'Eliminar', danger: true, fn: () => deleteShape(sh.id) },
      ]);
    });
    t.append(name, more);
    box.append(t);
  }
  const plus = document.createElement('button');
  plus.className = 'tab-plus'; plus.textContent = '+'; plus.title = 'Añadir un objeto detectado o importar de otro proyecto'; plus.setAttribute('aria-label', 'Añadir forma');
  plus.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu(plus, [...S.st.contours.components.map((c) => ({ label: `Objeto ${c.id + 1} · ${c.bbox.width.toFixed(0)} × ${c.bbox.height.toFixed(0)} mm`, fn: () => addShape(c.id) })), '-', { label: 'Forma manual (medidas / dibujo)…', fn: openManual }, { label: 'Compartimentos (filas × columnas)…', fn: openCompartments }, { label: 'Importar SVG o DXF…', fn: openVector }, { label: 'Importar de otro proyecto…', fn: openImport }, { label: 'Biblioteca de formas…', fn: openLibrary }]);
  });
  box.append(plus);
}
function renameShape(sh, span) {
  const inp = document.createElement('input');
  inp.className = 'tab-input'; inp.value = sh.name; inp.maxLength = 80;
  span.replaceWith(inp);
  inp.focus(); inp.select();
  const done = (ok) => { if (ok && inp.value.trim()) { sh.name = inp.value.trim(); scheduleBuild(); } renderTabs(); renderLayout(); };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') done(true); if (e.key === 'Escape') done(false); });
  inp.addEventListener('blur', () => done(true));
}
function newId() {
  let n = SHAPES().length + 1;
  while (SHAPES().some((s) => s.id === 's' + n)) n++;
  return 's' + n;
}
function overlapsAny(b, ignore) {
  return SHAPES().some((o) => { if (o === ignore) return false; const q = bboxOf(o); return !(b[2] < q[0] - 2 || b[0] > q[2] + 2 || b[3] < q[1] - 2 || b[1] > q[3] + 2); });
}
function shapeDims(sh) { // ancho y alto de la caja local (sin rotar)
  const o = localGeom(sh).outer, xs = o.map((p) => p[0]), ys = o.map((p) => p[1]);
  return [Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)];
}
function defaultNotchUV(w, h) { return h > w ? [0, r2(h / 2 - 2.4)] : [r2(w / 2 - 2.4), 0]; }
function placeShape(sh) { // busca un hueco libre (barrido sobre rejilla de 10 mm) y la añade
  const [w, h] = shapeDims(sh);
  sh.x = 0; sh.y = 0;
  const [gw, gd] = binGrid(), u = CFG().grid_unit_mm;
  const maxX = (gw * u) / 2, maxY = (gd * u) / 2;
  let placed = false;
  for (let y = -maxY; y <= maxY && !placed; y += 10) {
    for (let x = -maxX; x <= maxX && !placed; x += 10) {
      sh.x = x; sh.y = y;
      const b = bboxOf(sh);
      if (b[0] > -maxX + 2 && b[2] < maxX - 2 && b[1] > -maxY + 2 && b[3] < maxY - 2 && !overlapsAny(b, sh)) placed = true;
    }
  }
  if (!placed) { const gb = groupBBox(); sh.x = 0; sh.y = (gb[3] || 0) + h / 2 + 6; }
  SHAPES().push(sh);
}
function addShape(comp, from) {
  const id = newId();
  const sh = from ? JSON.parse(JSON.stringify(from)) : {
    comp, mirror: false, offset_mm: 0, depth_mm: null, keep_holes: false, rot: 0,
    notch: { on: false, d: 16, u: 0, v: 0 },
  };
  sh.id = id; sh.comp = comp;
  sh.name = from ? `${from.name} copia` : `${S.st.name}·P${SHAPES().length + 1}`;
  if (!from) {
    const c = S.st.contours.components[comp];
    [sh.notch.u, sh.notch.v] = defaultNotchUV(c.bbox.width, c.bbox.height);
  }
  placeShape(sh);
  recenterGroup();
  E.sel = id; E.tool = null; E.pts = false;
  refreshEditor();
  scheduleBuild();
}

/* ---- importar formas de otros proyectos (el contorno se copia: la forma queda independiente) */
function importShapes(items) {
  let last = null;
  for (const { c, name: srcName, label, withHoles, lib } of items) {
    const xs = c.outer.map((p) => p[0]), ys = c.outer.map((p) => p[1]);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
    const f = (ring) => ring.map((p) => [r2(p[0] - cx), r2(p[1] - cy)]);
    const sh = { id: newId(), name: label || `${srcName}·P${c.id + 1}`, comp: null, outer: f(c.outer), holes: withHoles ? (c.holes || []).map(f) : [],
      mirror: false, offset_mm: 0, depth_mm: null, keep_holes: false, rot: 0, notch: { on: false, d: 16, u: 0, v: 0 } };
    [sh.notch.u, sh.notch.v] = defaultNotchUV(c.bbox.width, c.bbox.height);
    if (lib) {   // forma de la biblioteca: conserva profundidad, offset y muesca guardados
      sh.depth_mm = lib.depth_mm ?? null; sh.offset_mm = lib.offset_mm || 0;
      sh.notch = { on: !!lib.notch.on, d: lib.notch.d, u: r2(lib.notch.u - cx), v: r2(lib.notch.v - cy) };
    }
    placeShape(sh);
    last = sh;
  }
  recenterGroup();
  E.sel = last.id; E.tool = null; E.pts = false;
  refreshEditor();
  scheduleBuild();
}
function thumbSvg(c) {
  const b = c.bbox, pad = Math.max(b.width, b.height) * 0.06;
  const svg = svgEl('svg', { viewBox: `${b.x - pad} ${b.y - pad} ${b.width + 2 * pad} ${b.height + 2 * pad}`, class: 'imp-thumb' });
  svg.append(svgEl('path', { d: [c.outer, ...c.holes].map(pathD).join(' '), 'fill-rule': 'evenodd' }));
  return svg;
}
async function openImport() {
  closeMenu();
  const ov = document.createElement('div');
  ov.className = 'modal-ov';
  ov.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="Importar formas"><div class="modal-h"><b>Importar formas de otro proyecto</b><span class="grow"></span><button class="link" id="imp-x">Cerrar</button></div><div class="modal-b" id="imp-body"><p class="muted">Cargando proyectos…</p></div><div class="modal-f"><span class="muted" id="imp-count">0 seleccionadas</span><span class="grow"></span><button class="btn primary" id="imp-go" disabled>Importar</button></div></div>`;
  document.body.append(ov);
  const close = () => ov.remove();
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#imp-x').addEventListener('click', close);
  const keyh = (e) => { if (e.key === 'Escape') { close(); document.removeEventListener('keydown', keyh); } };
  document.addEventListener('keydown', keyh);
  const body = ov.querySelector('#imp-body'), pick = new Map(); // clave "sid:id" -> {c, name}
  const upd = () => { ov.querySelector('#imp-count').textContent = `${pick.size} seleccionada${pick.size === 1 ? '' : 's'}`; ov.querySelector('#imp-go').disabled = !pick.size; };
  try {
    const list = (await api('/api/sessions')).filter((s) => s.traced && s.id !== S.st.id);
    if (!list.length) { body.innerHTML = '<p class="muted">No hay otros proyectos con siluetas trazadas.</p>'; return; }
    body.replaceChildren();
    for (const s of list) {
      const sec = document.createElement('section');
      sec.className = 'imp-sec';
      const h = document.createElement('h4');
      h.textContent = `${s.name} · ${s.n_components} objeto${s.n_components === 1 ? '' : 's'}`;
      const grid = document.createElement('div');
      grid.className = 'imp-grid';
      sec.append(h, grid);
      body.append(sec);
      api(`/api/sessions/${s.id}`).then((full) => {
        if (!full.contours) return;
        for (const c of full.contours.components) {
          const card = document.createElement('button');
          card.className = 'imp-card'; card.type = 'button';
          const cap = document.createElement('span');
          cap.textContent = `${c.bbox.width.toFixed(0)} × ${c.bbox.height.toFixed(0)} mm`;
          card.append(thumbSvg(c), cap);
          const key = `${s.id}:${c.id}`;
          card.addEventListener('click', () => {
            if (pick.has(key)) { pick.delete(key); card.classList.remove('on'); } else { pick.set(key, { c, name: s.name }); card.classList.add('on'); }
            upd();
          });
          grid.append(card);
        }
      }).catch(() => { grid.textContent = 'No se pudo leer este proyecto'; });
    }
  } catch (e) { body.innerHTML = `<p class="muted">${e.message}</p>`; }
  ov.querySelector('#imp-go').addEventListener('click', () => {
    const items = [...pick.values()];
    close(); document.removeEventListener('keydown', keyh);
    if (!items.length) return;
    importShapes(items);
    toast(`${items.length} forma${items.length === 1 ? '' : 's'} importada${items.length === 1 ? '' : 's'}`);
  });
}

/* ---- modal generico + formas manuales + SVG/DXF */
function modalShell(title, goLabel, wide = false) {
  const ov = document.createElement('div');
  ov.className = 'modal-ov';
  ov.innerHTML = `<div class="modal${wide ? ' wide' : ''}" role="dialog" aria-modal="true" aria-label="${title}"><div class="modal-h"><b>${title}</b><span class="grow"></span><button class="link" data-x>Cerrar</button></div><div class="modal-b" data-body></div><div class="modal-f"><span class="muted" data-info></span><span class="grow"></span><button class="btn primary" data-go disabled>${goLabel}</button></div></div>`;
  document.body.append(ov);
  const keyh = (e) => { if (e.key === 'Escape') close(); };
  const close = () => { ov.remove(); document.removeEventListener('keydown', keyh); };
  document.addEventListener('keydown', keyh);
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  ov.querySelector('[data-x]').addEventListener('click', close);
  return { ov, close, body: ov.querySelector('[data-body]'), info: ov.querySelector('[data-info]'), go: ov.querySelector('[data-go]') };
}
const bboxFromRing = (ring) => { const xs = ring.map((p) => p[0]), ys = ring.map((p) => p[1]); const x = Math.min(...xs), y = Math.min(...ys); return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y }; };
const arcPts = (cx, cy, r, a0, a1, n) => Array.from({ length: n + 1 }, (_, i) => { const a = ((a0 + ((a1 - a0) * i) / n) * Math.PI) / 180; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; });
function primitiveRing(kind, v) {
  const w = v.w, h = v.h;
  if (kind === 'rect') {
    const r = Math.min(v.r || 0, w / 2 - 0.01, h / 2 - 0.01);
    if (r <= 0.05) return [[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]];
    return [...arcPts(w / 2 - r, -h / 2 + r, r, -90, 0, 10), ...arcPts(w / 2 - r, h / 2 - r, r, 0, 90, 10), ...arcPts(-w / 2 + r, h / 2 - r, r, 90, 180, 10), ...arcPts(-w / 2 + r, -h / 2 + r, r, 180, 270, 10)];
  }
  if (kind === 'ellipse') return Array.from({ length: 72 }, (_, i) => { const a = (i / 72) * 2 * Math.PI; return [(w / 2) * Math.cos(a), (h / 2) * Math.sin(a)]; });
  if (kind === 'slot') {
    const r = Math.min(w, h) / 2;
    if (w >= h) return [...arcPts(w / 2 - r, 0, r, -90, 90, 20), ...arcPts(-w / 2 + r, 0, r, 90, 270, 20)];
    return [...arcPts(0, h / 2 - r, r, 0, 180, 20), ...arcPts(0, -h / 2 + r, r, 180, 360, 20)];
  }
  if (kind === 'poly') { const n = Math.max(3, Math.round(v.n)), R = w / 2; return Array.from({ length: n }, (_, i) => { const a = (i / n) * 2 * Math.PI - Math.PI / 2; return [R * Math.cos(a), R * Math.sin(a)]; }); }
  return [];
}
function openManual() {
  closeMenu();
  const M = modalShell('Forma manual', 'Añadir al bin', true);
  M.body.innerHTML = `
    <div class="seg" id="mn-mode"><button data-m="prim" class="on">Medidas</button><button data-m="draw">Dibujar polígono</button></div>
    <div id="mn-prim">
      <div class="mn-row"><label>Tipo <select id="mn-kind"><option value="rect">Rectángulo (esquinas redondeables)</option><option value="ellipse">Círculo / elipse</option><option value="slot">Ranura (obround)</option><option value="poly">Polígono regular</option></select></label>
        <label id="mn-w-l">Ancho (mm) <input type="number" id="mn-w" min="2" max="400" step="0.5" value="40"></label>
        <label id="mn-h-l">Alto (mm) <input type="number" id="mn-h" min="2" max="400" step="0.5" value="25"></label>
        <label id="mn-r-l">Radio de esquina (mm) <input type="number" id="mn-r" min="0" max="100" step="0.5" value="3"></label>
        <label id="mn-n-l" class="hidden">Lados <input type="number" id="mn-n" min="3" max="24" step="1" value="6"></label></div>
      <svg id="mn-prev" class="mn-canvas" viewBox="-60 -40 120 80" preserveAspectRatio="xMidYMid meet"></svg>
    </div>
    <div id="mn-draw" class="hidden">
      <p class="muted">Haz clic para poner vértices (cuadrícula de 1 mm; <b>Shift</b> = sin ajuste). Cierra con doble clic o con «Cerrar polígono».</p>
      <svg id="mn-cv" class="mn-canvas draw" viewBox="0 0 200 130" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="mn-row"><button class="btn ghost" id="mn-undo" type="button">Quitar último punto</button><button class="btn ghost" id="mn-close" type="button">Cerrar polígono</button><button class="btn ghost" id="mn-clear" type="button">Borrar todo</button><span class="muted" id="mn-dim"></span></div>
    </div>`;
  const $m = (id) => M.body.querySelector('#' + id);
  let mode = 'prim', pts = [], closed = false, ring = [];
  const showFields = () => {
    const k = $m('mn-kind').value;
    $m('mn-h-l').classList.toggle('hidden', k === 'poly');
    $m('mn-r-l').classList.toggle('hidden', k !== 'rect');
    $m('mn-n-l').classList.toggle('hidden', k !== 'poly');
    $m('mn-w-l').firstChild.textContent = k === 'poly' ? 'Diámetro (mm) ' : k === 'ellipse' ? 'Ancho / diámetro (mm) ' : 'Ancho (mm) ';
  };
  const upd = () => {
    if (mode === 'prim') {
      const v = { w: +$m('mn-w').value, h: +$m('mn-h').value, r: +$m('mn-r').value, n: +$m('mn-n').value };
      if (v.w >= 2 && (v.h >= 2 || $m('mn-kind').value === 'poly')) ring = primitiveRing($m('mn-kind').value, v); else ring = [];
      const pv = $m('mn-prev');
      pv.replaceChildren();
      for (let i = -60; i <= 60; i += 10) pv.append(svgEl('line', { x1: i, y1: -40, x2: i, y2: 40, class: 'bin-grid' }));
      for (let j = -40; j <= 40; j += 10) pv.append(svgEl('line', { x1: -60, y1: j, x2: 60, y2: j, class: 'bin-grid' }));
      if (ring.length) {
        const b = bboxFromRing(ring), f = Math.min(1, 100 / Math.max(b.width, b.height, 1));
        const g = svgEl('g', { transform: `scale(${f})` });
        g.append(svgEl('path', { d: pathD(ring), class: 'shp-body', fill: '#f5d9c8', stroke: '#e0561a', 'stroke-width': 1.2 / f }));
        pv.append(g);
        M.info.textContent = `${b.width.toFixed(1)} × ${b.height.toFixed(1)} mm${f < 1 ? ' (vista reducida)' : ''}`;
      } else M.info.textContent = 'Medidas no válidas';
    } else {
      const cv = $m('mn-cv');
      cv.replaceChildren();
      for (let i = 0; i <= 200; i += 10) cv.append(svgEl('line', { x1: i, y1: 0, x2: i, y2: 130, class: 'bin-grid' }));
      for (let j = 0; j <= 130; j += 10) cv.append(svgEl('line', { x1: 0, y1: j, x2: 200, y2: j, class: 'bin-grid' }));
      if (pts.length) {
        cv.append(svgEl(closed ? 'polygon' : 'polyline', { points: pts.map((p) => p.join(',')).join(' '), fill: closed ? '#f5d9c8' : 'none', stroke: '#e0561a', 'stroke-width': 0.8 }));
        pts.forEach((p) => cv.append(svgEl('circle', { cx: p[0], cy: p[1], r: 1.4, fill: '#e0561a' })));
      }
      ring = closed && pts.length >= 3 ? pts.map((p) => [p[0], p[1]]) : [];
      if (ring.length) { const b = bboxFromRing(ring); $m('mn-dim').textContent = `${b.width.toFixed(0)} × ${b.height.toFixed(0)} mm`; } else $m('mn-dim').textContent = `${pts.length} punto${pts.length === 1 ? '' : 's'}`;
      M.info.textContent = ring.length ? $m('mn-dim').textContent : 'Dibuja y cierra el polígono';
    }
    M.go.disabled = ring.length < 3;
  };
  M.body.querySelectorAll('#mn-prim input, #mn-prim select').forEach((el) => el.addEventListener('input', () => { showFields(); upd(); }));
  M.body.querySelectorAll('#mn-mode button').forEach((b) => b.addEventListener('click', () => {
    mode = b.dataset.m;
    M.body.querySelectorAll('#mn-mode button').forEach((x) => x.classList.toggle('on', x === b));
    $m('mn-prim').classList.toggle('hidden', mode !== 'prim'); $m('mn-draw').classList.toggle('hidden', mode !== 'draw');
    upd();
  }));
  const cv = $m('mn-cv');
  cv.addEventListener('click', (e) => {
    if (closed) return;
    const p = svgPoint(cv, e);
    let x = Math.min(200, Math.max(0, p.x)), y = Math.min(130, Math.max(0, p.y));
    if (!e.shiftKey) { x = Math.round(x); y = Math.round(y); }
    if (e.detail >= 2 && pts.length >= 3) { closed = true; } else if (!pts.length || pts[pts.length - 1][0] !== x || pts[pts.length - 1][1] !== y) pts.push([x, y]);
    upd();
  });
  $m('mn-undo').addEventListener('click', () => { if (closed) closed = false; else pts.pop(); upd(); });
  $m('mn-close').addEventListener('click', () => { if (pts.length >= 3) { closed = true; upd(); } });
  $m('mn-clear').addEventListener('click', () => { pts = []; closed = false; upd(); });
  M.go.addEventListener('click', () => {
    if (ring.length < 3) return;
    const r = ring.map((p) => [r2(p[0]), r2(p[1])]), b = bboxFromRing(r);
    const kindName = mode === 'draw' ? 'Dibujo' : { rect: 'Rect', ellipse: 'Elipse', slot: 'Ranura', poly: 'Polígono' }[$m('mn-kind').value];
    M.close();
    importShapes([{ c: { id: 0, outer: r, holes: [], bbox: b }, name: 'Manual', label: `${kindName} ${b.width.toFixed(0)}×${b.height.toFixed(0)}` }]);
    toast('Forma añadida');
  });
  showFields(); upd();
}
async function openVector() {
  closeMenu();
  const inp = document.createElement('input');
  inp.type = 'file'; inp.accept = '.svg,.dxf,image/svg+xml';
  inp.addEventListener('change', async () => {
    const f = inp.files[0];
    if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    const res = await busy('Leyendo el archivo…', () => api('/api/vector/parse', { method: 'POST', body: fd }));
    if (!res) return;
    if (!res.shapes.length) return toast('No encontré contornos cerrados', true);
    const M = modalShell(`Importar ${res.format.toUpperCase()} · ${f.name}`, 'Importar', true);
    M.body.innerHTML = `<p class="muted">Elige los contornos (medidas en mm según las unidades del archivo). Los huecos internos se conservan; actívalos por forma con «Detectar huecos» en su panel Pocket si los necesitas.</p>
      <label class="inline">Escala (%) <input type="number" id="vc-scale" min="1" max="2000" step="1" value="100"></label><div class="imp-grid" id="vc-grid"></div>`;
    const grid = M.body.querySelector('#vc-grid'), sc = M.body.querySelector('#vc-scale'), pick = new Set();
    const upd = () => { M.info.textContent = `${pick.size} seleccionada${pick.size === 1 ? '' : 's'}`; M.go.disabled = !pick.size; };
    const capOf = (c) => { const k = (+sc.value || 100) / 100; return `${(c.bbox.width * k).toFixed(1)} × ${(c.bbox.height * k).toFixed(1)} mm`; };
    const caps = [];
    for (const c of res.shapes) {
      const card = document.createElement('button');
      card.className = 'imp-card on'; card.type = 'button';
      const cap = document.createElement('span'); cap.textContent = capOf(c); caps.push([cap, c]);
      card.append(thumbSvg(c), cap);
      pick.add(c.id);
      card.addEventListener('click', () => { if (pick.has(c.id)) { pick.delete(c.id); card.classList.remove('on'); } else { pick.add(c.id); card.classList.add('on'); } upd(); });
      grid.append(card);
    }
    sc.addEventListener('input', () => caps.forEach(([el, c]) => { el.textContent = capOf(c); }));
    upd();
    M.go.addEventListener('click', () => {
      const k = Math.min(20, Math.max(0.01, (+sc.value || 100) / 100));
      const sel = res.shapes.filter((c) => pick.has(c.id)).map((c) => {
        const g = (ring) => ring.map((p) => [p[0] * k, p[1] * k]);
        const cc = { id: c.id, outer: g(c.outer), holes: c.holes.map(g), bbox: { x: c.bbox.x * k, y: c.bbox.y * k, width: c.bbox.width * k, height: c.bbox.height * k } };
        return { c: cc, name: f.name.replace(/\.[^.]+$/, ''), label: `${f.name.replace(/\.[^.]+$/, '')}·${c.id + 1}`, withHoles: true };
      });
      M.close();
      importShapes(sel);
      toast(`${sel.length} forma${sel.length === 1 ? '' : 's'} importada${sel.length === 1 ? '' : 's'}`);
    });
  });
  inp.click();
}
function deleteShape(id) {
  if (SHAPES().length <= 1) {   // sin siluetas el bin pasa a ser un contenedor abierto: la rejilla se fija
    const [a, b] = binGrid();
    CFG().auto_size = false; CFG().grid_width = a; CFG().grid_depth = b;
    toast('Bin sin siluetas: ahora es un contenedor abierto (usa divisores para dividirlo)');
  }
  D().shapes = SHAPES().filter((s) => s.id !== id);
  if (E.sel === id) { E.sel = null; E.tool = null; E.pts = false; }
  recenterGroup();
  refreshEditor();
  scheduleBuild();
}
function selectShape(id) {
  if (E.sel !== id) { E.pts = false; if (E.tool === 'edit' || E.tool === 'notch' || E.tool === 'pocket') E.tool = E.tool; }
  E.sel = id;
  renderTabs(); renderLayout(); renderToolbar(); renderPop();
}

/* ---- barra de herramientas y paneles flotantes */
function selIds() { return E.multi.size > 1 ? SHAPES().filter((q) => E.multi.has(q.id)).map((q) => q.id) : (E.sel ? [E.sel] : []); }
function renderToolbar() {
  if (!E.sel) E.multi.clear();
  const has = !!selShape();
  const n = selIds().length;
  document.querySelectorAll('#alignbar button').forEach((b) => {
    b.disabled = n < 1 || (n < 2 && b.dataset.al.startsWith('d')) || (n < 3 && b.dataset.al.startsWith('d'));
  });
  $('alignbar-n').textContent = n > 1 ? `${n} formas` : n === 1 ? '1 forma → bin' : '';
  document.querySelectorAll('#toolbar button').forEach((b) => {
    const t = b.dataset.tool;
    b.disabled = !has && t !== 'done';
    b.classList.toggle('on', E.tool === t);
  });
}
$('toolbar').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  const sh = selShape(), t = b.dataset.tool;
  if (t === 'rotate' && sh) { setRot(sh, sh.rot + 90); recenterGroup(); afterEdit(); }
  else if (t === 'center' && sh) { const c = bcenter(bboxOf(sh)); sh.x = r2(sh.x - c[0]); sh.y = r2(sh.y - c[1]); afterEdit(); }
  else if (t === 'delete' && sh) deleteShape(sh.id);
  else if (t === 'done') { E.sel = null; E.tool = null; E.pts = false; refreshEditor(); }
  else if (['notch', 'pocket', 'edit'].includes(t)) { E.tool = E.tool === t ? null : t; if (t !== 'edit') E.pts = false; renderToolbar(); renderPop(); renderLayout(); }
});
$('alignbar').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  const ids = selIds(), list = SHAPES().filter((q) => ids.includes(q.id)), a = b.dataset.al;
  const bb = list.map((q) => bboxOf(q));
  let ref;
  if (list.length > 1) ref = [Math.min(...bb.map((x) => x[0])), Math.min(...bb.map((x) => x[1])), Math.max(...bb.map((x) => x[2])), Math.max(...bb.map((x) => x[3]))];
  else if (CFG().auto_size) ref = groupBBox();
  else { const [gw, gd] = binGrid(), u = CFG().grid_unit_mm, w = gw * u - 0.5 - 2 * CFG().wall_thickness_mm, h = gd * u - 0.5 - 2 * CFG().wall_thickness_mm; ref = [-w / 2, -h / 2, w / 2, h / 2]; }
  const move = (q, dx, dy) => { q.x = r2(q.x + dx); q.y = r2(q.y + dy); };
  list.forEach((q, i) => {
    const x = bb[i];
    if (a === 'left') move(q, ref[0] - x[0], 0);
    else if (a === 'right') move(q, ref[2] - x[2], 0);
    else if (a === 'hc') move(q, (ref[0] + ref[2]) / 2 - (x[0] + x[2]) / 2, 0);
    else if (a === 'top') move(q, 0, ref[1] - x[1]);
    else if (a === 'bottom') move(q, 0, ref[3] - x[3]);
    else if (a === 'vc') move(q, 0, (ref[1] + ref[3]) / 2 - (x[1] + x[3]) / 2);
  });
  if (a === 'dh' || a === 'dv') {                          // reparte con huecos iguales entre cajas
    const ax = a === 'dh' ? 0 : 1;
    const ord = list.map((q, i) => ({ q, lo: bb[i][ax], hi: bb[i][ax + 2] })).sort((m, n) => m.lo - n.lo);
    const total = ord[ord.length - 1].hi - ord[0].lo, sizes = ord.reduce((t, o) => t + (o.hi - o.lo), 0), gap = (total - sizes) / (ord.length - 1);
    let pos = ord[0].lo;
    for (const o of ord) { const dlt = pos - o.lo; if (ax === 0) move(o.q, dlt, 0); else move(o.q, 0, dlt); pos += o.hi - o.lo + gap; }
  }
  recenterGroup(); afterEdit();
});
function afterEdit() { renderLayout(); renderPop(); scheduleBuild(); }

function numField(label, id, val, attrs = '') {
  return `<label>${label}<input type="number" id="${id}" value="${val}" ${attrs}></label>`;
}
function renderPop() {
  const pop = $('pop'), sh = selShape();
  if (!sh || !E.tool || !['notch', 'pocket', 'edit'].includes(E.tool)) { pop.classList.add('hidden'); pop.replaceChildren(); return; }
  pop.classList.remove('hidden');
  const on = (id, fn, ev = 'change') => $(id).addEventListener(ev, fn);
  if (E.tool === 'notch') {
    pop.innerHTML = `<h4>Finger notch · ${sh.name}</h4>
      <label class="sw"><span>Enabled</span><input type="checkbox" id="p-n-on" ${sh.notch.on ? 'checked' : ''}><i></i></label>
      ${numField('Diameter (mm)', 'p-n-d', sh.notch.d, 'min="6" max="40" step="1"')}
      <p class="muted">Arrastra el círculo en el layout para moverlo.</p>`;
    on('p-n-on', () => { sh.notch.on = $('p-n-on').checked; afterEdit(); });
    on('p-n-d', () => { sh.notch.d = Math.min(40, Math.max(6, +$('p-n-d').value || 16)); afterEdit(); });
  } else if (E.tool === 'pocket') {
    pop.innerHTML = `<h4>Pocket · ${sh.name}</h4>
      ${numField('Depth (mm)', 'p-depth', sh.depth_mm ?? CFG().pocket_depth_mm, 'min="1" max="80" step="0.5"')}
      <p class="muted">Por defecto: ${CFG().pocket_depth_mm} mm (ajuste del bin).</p>
      <button class="btn small" id="p-depth-reset" ${sh.depth_mm == null ? 'disabled' : ''}>Usar el valor por defecto</button>`;
    on('p-depth', () => { sh.depth_mm = Math.min(80, Math.max(1, +$('p-depth').value || CFG().pocket_depth_mm)); afterEdit(); });
    on('p-depth-reset', () => { sh.depth_mm = null; afterEdit(); }, 'click');
  } else {
    pop.innerHTML = `<h4>Edit shape · ${sh.name}</h4>
      ${numField('Offset (mm)', 'p-off', sh.offset_mm, 'min="-5" max="10" step="0.1"')}
      <div class="btnrow"><button class="btn small" id="p-mirror">⇋ Mirror</button>
      <button class="btn small ${E.pts ? 'primary' : ''}" id="p-pts">${E.pts ? '✓ Editing points' : '✎ Edit points'}</button></div>
      <label class="check"><input type="checkbox" id="p-holes" ${sh.keep_holes ? 'checked' : ''} ${sh.outer ? 'disabled' : ''}> Keep inner holes</label>
      <button class="btn small" id="p-reset" ${sh.outer && sh.comp != null ? '' : 'disabled'}>Reset contour</button>
      <p class="muted">${E.pts ? 'Arrastra un punto; doble clic lo borra; arrastra un punto central para añadir uno.' : 'Offset agranda (+) o reduce (−) la forma.'}</p>`;
    on('p-off', () => { sh.offset_mm = Math.min(10, Math.max(-5, +$('p-off').value || 0)); afterEdit(); });
    on('p-mirror', () => {
      const flip = (r) => r.map((p) => [-p[0], p[1]]);
      if (sh.outer) { sh.outer = flip(sh.outer); sh.holes = (sh.holes || []).map(flip); }
      sh.notch.u = -sh.notch.u; sh.mirror = !sh.mirror; afterEdit();
    }, 'click');
    on('p-holes', () => { sh.keep_holes = $('p-holes').checked; afterEdit(); });
    on('p-pts', () => {
      if (!E.pts && !sh.outer) { const g = localGeom(sh); sh.outer = rdp(g.outer, 0.6); sh.holes = g.holes.map((h) => rdp(h, 0.6)); }
      E.pts = !E.pts; renderPop(); renderLayout();
    }, 'click');
    on('p-reset', () => { delete sh.outer; delete sh.holes; E.pts = false; afterEdit(); }, 'click');
  }
}

/* ---- layout 2D */
const ringPath = (ring) => 'M' + ring.map((p) => `${r2(p[0])},${r2(p[1])}`).join(' L') + ' Z';
function renderLayout() {
  if (!S.st || !D()) return;
  const svg = $('lay-svg'), c = CFG(), u = c.grid_unit_mm;
  const [gw, gd] = binGrid();
  const Wd = gw * u, Dd = gd * u, m = 8;
  svg.setAttribute('viewBox', `${-Wd / 2 - m} ${-Dd / 2 - m} ${Wd + 2 * m} ${Dd + 2 * m}`);
  svg.replaceChildren();
  const g = (attrs = {}) => svgEl('g', attrs);
  const bin = svgEl('rect', { x: -Wd / 2, y: -Dd / 2, width: Wd, height: Dd, rx: 3.75, class: 'bin-rect' });
  svg.append(bin);
  for (let i = 1; i < gw; i++) svg.append(svgEl('line', { x1: -Wd / 2 + i * u, y1: -Dd / 2, x2: -Wd / 2 + i * u, y2: Dd / 2, class: 'bin-grid' }));
  for (let j = 1; j < gd; j++) svg.append(svgEl('line', { x1: -Wd / 2, y1: -Dd / 2 + j * u, x2: Wd / 2, y2: -Dd / 2 + j * u, class: 'bin-grid' }));
  for (const k of cellsOff()) {
    const [ci, cj] = k.split(',').map(Number);
    if (ci < gw && cj < gd) svg.append(svgEl('rect', { x: -Wd / 2 + ci * u, y: -Dd / 2 + cj * u, width: u, height: u, class: 'cell-off' }));
  }
  for (const x of c.dividers_x || []) svg.append(svgEl('line', { x1: x, y1: -Dd / 2, x2: x, y2: Dd / 2, class: 'div-line' }));
  for (const y of c.dividers_y || []) svg.append(svgEl('line', { x1: -Wd / 2, y1: y, x2: Wd / 2, y2: y, class: 'div-line' }));
  const probs = (E.meta && E.meta.problems) || {};
  const halo = Math.max(0, c.clearance_mm);
  for (const sh of SHAPES()) {
    const geo = localGeom(sh);
    const grp = g({ class: 'shp' + (sh.id === E.sel || E.multi.has(sh.id) ? ' sel' : '') + (probs[sh.id] ? ' bad' : ''), 'data-shape': sh.id, transform: `translate(${sh.x} ${sh.y}) rotate(${sh.rot})` });
    const d = [geo.outer, ...geo.holes].map(ringPath).join(' ');
    const ph = Math.max(0, halo + sh.offset_mm);
    if (ph > 0) grp.append(svgEl('path', { d, class: 'shp-halo', 'stroke-width': 2 * ph }));
    grp.append(svgEl('path', { d, class: 'shp-body' }));
    svg.append(grp);
    const b = bboxOf(sh), cc = bcenter(b);
    const t = svgEl('text', { x: cc[0], y: cc[1], class: 'shp-label', 'text-anchor': 'middle', 'dominant-baseline': 'middle', 'font-size': Math.max(4, Math.min(Wd, Dd) * 0.032) });
    t.textContent = sh.name.split('·').pop();
    svg.append(t);
    if (sh.notch.on) {
      const q = xf(sh, [sh.notch.u, sh.notch.v]);
      svg.append(svgEl('circle', { cx: q[0], cy: q[1], r: sh.notch.d / 2, class: 'notch' + (sh.id === E.sel ? ' sel' : ''), 'data-notch': sh.id }));
    }
  }
  const sh = selShape();
  if (sh) {
    const b = bboxOf(sh), cx = (b[0] + b[2]) / 2, hr = Math.max(2.2, Wd * 0.012);
    svg.append(svgEl('rect', { x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1], class: 'sel-box' }));
    // manija de rotación: debajo de la caja; si no cabe en el visor, arriba
    const below = b[3] + 9 + hr < Dd / 2 + m - 1;
    const y0 = below ? b[3] : b[1], y1 = below ? b[3] + 9 : b[1] - 9;
    svg.append(svgEl('line', { x1: cx, y1: y0, x2: cx, y2: y1, class: 'rot-line' }));
    svg.append(svgEl('circle', { cx, cy: y1, r: hr, class: 'rot-handle', 'data-rot': sh.id }));
    if (E.pts) {
      const rings = [localGeom(sh).outer, ...(sh.holes || [])];
      const vr = Math.max(1.4, Wd * 0.007);
      rings.forEach((ring, ri) => ring.forEach((p, i) => {
        const q = xf(sh, p), n = xf(sh, ring[(i + 1) % ring.length]);
        svg.append(svgEl('circle', { cx: (q[0] + n[0]) / 2, cy: (q[1] + n[1]) / 2, r: vr * 0.65, class: 'vtx-mid', 'data-mid': `${ri}:${i}` }));
      }));
      rings.forEach((ring, ri) => ring.forEach((p, i) => {
        const q = xf(sh, p);
        svg.append(svgEl('circle', { cx: q[0], cy: q[1], r: vr, class: 'vtx', 'data-vtx': `${ri}:${i}` }));
      }));
    }
  }
}

const svgPt = (e) => { const svg = $('lay-svg'), pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const p = pt.matrixTransform(svg.getScreenCTM().inverse()); return [p.x, p.y]; };
$('lay-svg').addEventListener('pointerdown', (e) => {
  const t = e.target;
  const p = svgPt(e);
  const svg = $('lay-svg');
  const set = (d) => { E.drag = d; svg.setPointerCapture(e.pointerId); e.preventDefault(); };
  if (t.dataset.rot) { const sh = selShape(); const c = bcenter(bboxOf(sh)); set({ k: 'rot', sh, x: sh.x, y: sh.y, rot: sh.rot, c, a0: Math.atan2(p[1] - c[1], p[0] - c[0]) }); return; }
  if (t.dataset.vtx) {
    const [ri, i] = t.dataset.vtx.split(':').map(Number), now = performance.now(), sh = selShape();
    const ring = ri === 0 ? sh.outer : sh.holes[ri - 1];
    if (E.lastV && E.lastV.key === t.dataset.vtx && now - E.lastV.t < 450 && ring.length > 3) { // doble clic: borrar el punto
      ring.splice(i, 1); E.lastV = null; e.preventDefault(); renderLayout(); scheduleBuild(); return;
    }
    E.lastV = { key: t.dataset.vtx, t: now };
    set({ k: 'vtx', sh, ri, i });
    return;
  }
  if (t.dataset.mid) {
    const sh = selShape(), [ri, i] = t.dataset.mid.split(':').map(Number);
    const ring = ri === 0 ? sh.outer : sh.holes[ri - 1];
    ring.splice(i + 1, 0, unxf(sh, p));
    set({ k: 'vtx', sh, ri, i: i + 1 }); renderLayout(); return;
  }
  if (t.dataset.notch && t.dataset.notch === E.sel) { set({ k: 'notch', sh: selShape() }); return; }
  const gEl = t.closest('[data-shape]');
  if (gEl) {
    const sh = SHAPES().find((s) => s.id === gEl.dataset.shape);
    if (e.shiftKey) {                                       // Shift+clic: suma o quita de la seleccion multiple
      if (E.sel && !E.multi.size) E.multi.add(E.sel);
      if (E.multi.has(sh.id)) { E.multi.delete(sh.id); if (E.sel === sh.id) E.sel = [...E.multi][0] || null; }
      else { E.multi.add(sh.id); E.sel = sh.id; }
      if (E.multi.size < 2) E.multi.clear();
      E.tool = null; E.pts = false; renderTabs(); renderToolbar(); renderPop(); renderLayout();
      return;
    }
    if (E.multi.size > 1 && E.multi.has(sh.id)) {           // arrastrar el grupo seleccionado
      E.sel = sh.id;
      set({ k: 'move', sh, px: p[0], py: p[1], x: sh.x, y: sh.y, moved: false, grp: SHAPES().filter((q) => E.multi.has(q.id)).map((q) => ({ q, x: q.x, y: q.y })) });
      return;
    }
    E.multi.clear();
    if (sh.id !== E.sel) { E.sel = sh.id; E.pts = false; renderTabs(); renderToolbar(); renderPop(); renderLayout(); }
    set({ k: 'move', sh, px: p[0], py: p[1], x: sh.x, y: sh.y, moved: false });
    return;
  }
  if (E.sel && !t.closest('.pop') && !e.shiftKey) { E.sel = null; E.multi.clear(); E.pts = false; refreshEditor(); }
});
$('lay-svg').addEventListener('pointermove', (e) => {
  const d = E.drag;
  if (!d) return;
  const p = svgPt(e);
  if (d.k === 'move') {
    d.moved = true;
    d.sh.x = r2(d.x + p[0] - d.px); d.sh.y = r2(d.y + p[1] - d.py);
    for (const g of d.grp || []) { g.q.x = r2(g.x + p[0] - d.px); g.q.y = r2(g.y + p[1] - d.py); }
  } else if (d.k === 'rot') {
    d.sh.x = d.x; d.sh.y = d.y; d.sh.rot = d.rot;
    let delta = ((Math.atan2(p[1] - d.c[1], p[0] - d.c[0]) - d.a0) * 180) / Math.PI;
    let r = d.rot + delta;
    if (e.shiftKey) r = Math.round(r / 15) * 15; else r = Math.round(r * 2) / 2;
    setRot(d.sh, r);
  } else if (d.k === 'vtx') {
    const q = unxf(d.sh, p);
    const ring = d.ri === 0 ? d.sh.outer : d.sh.holes[d.ri - 1];
    ring[d.i] = [r2(q[0]), r2(q[1])];
  } else if (d.k === 'notch') {
    const q = unxf(d.sh, p);
    d.sh.notch.u = r2(q[0]); d.sh.notch.v = r2(q[1]);
  }
  renderLayout();
});
const endDrag = () => {
  const d = E.drag;
  if (!d) return;
  E.drag = null;
  if (d.k === 'move' && !d.moved) return;
  if (d.k === 'move' || d.k === 'rot') recenterGroup();
  renderLayout(); renderPop(); scheduleBuild();
};
$('lay-svg').addEventListener('pointerup', endDrag);
$('lay-svg').addEventListener('pointercancel', endDrag);
$('lay-svg').addEventListener('keydown', (e) => {
  const sh = selShape();
  if (!sh) return;
  const d = e.shiftKey ? 5 : 1;
  const m = { ArrowLeft: [-d, 0], ArrowRight: [d, 0], ArrowUp: [0, -d], ArrowDown: [0, d] }[e.key];
  if (m) { e.preventDefault(); sh.x = r2(sh.x + m[0]); sh.y = r2(sh.y + m[1]); renderLayout(); scheduleBuild(250); }
  else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteShape(sh.id); }
  else if (e.key === 'r' || e.key === 'R') { setRot(sh, sh.rot + 90); recenterGroup(); afterEdit(); }
  else if (e.key === 'Escape') { E.sel = null; E.tool = null; E.pts = false; refreshEditor(); }
});
$('btn-undo').addEventListener('click', undo);
$('btn-redo').addEventListener('click', redo);
$('btn-arrange').addEventListener('click', async () => {
  if (!SHAPES().length) return toast('No hay formas que ordenar', true);
  const res = await busy('Ordenando…', () => api(`/api/sessions/${S.st.id}/design/arrange`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify({ settings: CFG(), shapes: SHAPES() }) }));
  if (!res) return;
  for (const r of res) { const sh = SHAPES().find((s) => s.id === r.id); if (sh) { sh.x = r.x; sh.y = r.y; sh.rot = r.rot; } }
  E.meta = null;
  refreshEditor(); scheduleBuild(50);
});

/* ---- ajustes del bin */
const NUM = { 's-gw': 'grid_width', 's-gd': 'grid_depth', 's-hu': 'height_units', 's-unit': 'grid_unit_mm', 's-depth': 'pocket_depth_mm', 's-clr': 'clearance_mm', 's-ithk': 'insert_thickness_mm', 's-ifit': 'fit_clearance_mm', 's-wall': 'wall_thickness_mm', 's-md': 'magnet_diameter_mm', 's-mdep': 'magnet_depth_mm', 's-rib': 'hollow_rib_mm', 's-hfloor': 'hollow_floor_mm', 's-lidt': 'lid_thickness_mm', 's-ledge': 'lid_ledge_mm', 's-lidc': 'lid_clearance_mm', 's-bedx': 'bed_x_mm', 's-bedy': 'bed_y_mm', 's-nozzle': 'nozzle_mm', 's-dth': 'divider_thickness_mm', 's-dcl': 'divider_clearance_mm', 's-dsd': 'divider_slot_depth_mm' };
const BOOL = { 's-auto': 'auto_size', 's-chamfer': 'pocket_chamfer', 's-insert': 'contrast_insert', 's-lip': 'stacking_lip', 's-mag': 'magnet_holes', 's-hollow': 'hollow', 's-partial': 'partial' };
for (const [id, key] of Object.entries(NUM)) {
  $(id).addEventListener('change', () => {
    const el = $(id), lo = +el.min, hi = +el.max;
    let v = +el.value;
    if (!Number.isFinite(v)) v = DEFAULT_SETTINGS[key];
    v = Math.min(hi, Math.max(lo, v));
    if (el.step === '1') v = Math.round(v);
    CFG()[key] = v;
    E.meta = id === 's-unit' ? null : E.meta;
    renderSettings(); renderLayout(); scheduleBuild();
  });
}
for (const [id, key] of Object.entries(BOOL)) {
  $(id).addEventListener('change', () => {
    const on = $(id).checked;
    if (key === 'auto_size' && !on && E.meta) { CFG().grid_width = E.meta.grid[0]; CFG().grid_depth = E.meta.grid[1]; }
    if (key === 'partial' && on) {
      if (E.meta) { CFG().grid_width = E.meta.grid[0]; CFG().grid_depth = E.meta.grid[1]; } else { const [a, b] = binGrid(); CFG().grid_width = a; CFG().grid_depth = b; }
      CFG().auto_size = false;
    }
    CFG()[key] = on;
    renderSettings(); renderLayout(); scheduleBuild();
  });
}
document.querySelectorAll('.stepper button').forEach((b) => b.addEventListener('click', () => {
  const el = $(b.dataset.step);
  if (el.disabled) return;
  el.value = Math.min(+el.max, Math.max(+el.min, +el.value + +b.dataset.d));
  el.dispatchEvent(new Event('change'));
}));
$('s-reset').addEventListener('click', () => {
  const keep = { grid_width: CFG().grid_width, grid_depth: CFG().grid_depth };
  D().settings = { ...DEFAULT_SETTINGS, ...keep };
  E.meta = null;
  renderSettings(); renderLayout(); scheduleBuild(100);
});
function renderSettings() {
  const c = CFG();
  const [gw, gd] = c.auto_size ? binGrid() : [c.grid_width, c.grid_depth];
  $('s-auto').checked = c.auto_size;
  $('s-gw').value = gw; $('s-gd').value = gd; $('s-hu').value = c.height_units;
  $('s-gw').disabled = $('s-gd').disabled = c.auto_size;
  document.querySelectorAll('.stepper button').forEach((b) => { b.disabled = (b.dataset.step === 's-gw' || b.dataset.step === 's-gd') && c.auto_size; });
  for (const [id, key] of Object.entries(NUM)) if (!['s-gw', 's-gd', 's-hu'].includes(id)) $(id).value = c[key];
  for (const [id, key] of Object.entries(BOOL)) $(id).checked = c[key];
  $('s-auto').disabled = c.partial;
  renderCells();
  renderDividerFields();
  $('s-ithk').disabled = $('s-ifit').disabled = !c.contrast_insert;
  $('s-md').disabled = $('s-mdep').disabled = !c.magnet_holes;
  for (const id of ['s-rib', 's-hfloor', 's-lidt', 's-ledge', 's-lidc']) $(id).disabled = !c.hollow;
  renderSettingsInfo();
}
function cellsOff() { return new Set(CFG().partial ? (CFG().cells_off || []) : []); }
function renderCells() {
  const c = CFG(), box = $('s-cells'), [gw, gd] = c.auto_size ? binGrid() : [c.grid_width, c.grid_depth];
  $('s-cells-wrap').hidden = !c.partial;
  box.replaceChildren();
  if (!c.partial) return;
  const off = new Set(c.cells_off || []);
  box.style.gridTemplateColumns = `repeat(${gw}, 1fr)`;
  for (let j = 0; j < gd; j++) for (let i = 0; i < gw; i++) {
    const b = document.createElement('button');
    const isOff = off.has(`${i},${j}`);
    b.type = 'button'; b.className = 'cellbtn' + (isOff ? '' : ' on'); b.textContent = isOff ? 'Off' : 'On';
    b.dataset.cell = `${i},${j}`;
    b.addEventListener('click', () => {
      const k = `${i},${j}`, cur = new Set(CFG().cells_off || []);
      if (cur.has(k)) cur.delete(k); else cur.add(k);
      if (cur.size >= gw * gd) cur.delete(k);               // al menos una celda activa
      CFG().cells_off = [...cur];
      renderCells(); renderLayout(); scheduleBuild();
    });
    box.append(b);
  }
}
$('s-complement').addEventListener('click', () => {
  const c = CFG(), [gw, gd] = c.auto_size ? binGrid() : [c.grid_width, c.grid_depth], cur = new Set(c.cells_off || []);
  const next = [];
  for (let j = 0; j < gd; j++) for (let i = 0; i < gw; i++) if (!cur.has(`${i},${j}`)) next.push(`${i},${j}`);
  if (!cur.size || next.length >= gw * gd) return toast('Apaga primero alguna celda: el complemento son las celdas apagadas', true);
  c.cells_off = next;
  renderCells(); renderLayout(); scheduleBuild();
});
function renderSettingsInfo() {
  const c = CFG(), [gw, gd] = c.auto_size ? binGrid() : [c.grid_width, c.grid_depth], u = c.grid_unit_mm;
  if (c.auto_size) { $('s-gw').value = gw; $('s-gd').value = gd; }
  $('s-gw-mm').textContent = `(${(gw * u - 0.5).toFixed(1)} mm)`;
  $('s-gd-mm').textContent = `(${(gd * u - 0.5).toFixed(1)} mm)`;
  $('s-hu-mm').textContent = `(${(c.height_units * 7).toFixed(1)} mm)`;
}

/* ---- v1.3: varios bins por proyecto */
const binList = () => [{ id: 'b1', name: S.st.bin1_name || 'Bin 1' }, ...Object.entries(S.st.bins || {}).sort((a, b) => +a[0].slice(1) - +b[0].slice(1)).map(([id, h]) => ({ id, name: h.name || id }))];
async function flushBuild() {
  clearTimeout(E.timer);
  if (E.dirty && D()) await buildNow();
  while (E.building) await new Promise((r) => setTimeout(r, 60));
}
async function loadBin(bid) {
  const full = await api(`/api/sessions/${S.st.id}`);
  const h = bid === 'b1' ? full : (full.bins || {})[bid];
  if (!h) throw new Error('bin no encontrado');
  full.design = h.design || null; full.bin_meta = h.bin_meta || null;
  S.st = full; S.bin = bid;
}
async function switchBin(bid) {
  if (bid === S.bin) return;
  await flushBuild();
  try { await busy('Cargando el bin…', () => loadBin(bid)); } catch (e) { return toast(e.message, true); }
  renderBin();
}
async function newBin(opts) {
  await flushBuild();
  const body = { copy_from: opts.copy || opts.mode ? S.bin : null, name: opts.name || null, mode: opts.mode || null };
  const nb = await busy('Creando el bin…', () => api(`/api/sessions/${S.st.id}/bins`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify(body) }));
  if (!nb) return;
  if (opts.comps) S.sel = new Set(opts.comps);
  try { await loadBin(nb.id); } catch (e) { return toast(e.message, true); }
  renderBin();
}
function renderBinTabs() {
  const box = $('bin-tabs');
  box.replaceChildren();
  const lb = document.createElement('span');
  lb.className = 'lbl-bins'; lb.textContent = 'Bins';
  box.append(lb);
  for (const b of binList()) {
    const t = document.createElement('div');
    t.className = 'tab' + (b.id === S.bin ? ' on' : '');
    t.dataset.bin = b.id;
    const name = document.createElement('span');
    name.textContent = b.name;
    name.addEventListener('click', () => switchBin(b.id));
    const more = document.createElement('button');
    more.className = 'tab-more'; more.textContent = '···'; more.title = 'Opciones del bin'; more.setAttribute('aria-label', `Opciones de ${b.name}`);
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      openMenu(more, [
        { label: 'Renombrar', fn: async () => {
          const v = (window.prompt('Nombre del bin', b.name) || '').trim();
          if (!v) return;
          try { await api(`/api/sessions/${S.st.id}/bins/${b.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: v }) }); if (b.id === 'b1') S.st.bin1_name = v; else S.st.bins[b.id].name = v; renderBinTabs(); } catch (er) { toast(er.message, true); }
        } },
        { label: 'Duplicar', fn: async () => { if (b.id !== S.bin) await switchBin(b.id); await newBin({ copy: true, name: `${b.name} copia` }); } },
        ...(b.id === 'b1' ? [] : ['-', { label: 'Eliminar', danger: true, fn: async () => {
          if (!window.confirm(`¿Eliminar «${b.name}»?`)) return;
          await flushBuild();
          try { await api(`/api/sessions/${S.st.id}/bins/${b.id}`, { method: 'DELETE' }); await loadBin('b1'); renderBin(); } catch (er) { toast(er.message, true); }
        } }]),
      ]);
    });
    t.append(name, more);
    box.append(t);
  }
  const plus = document.createElement('button');
  plus.className = 'tab-plus'; plus.textContent = '+'; plus.title = 'Añadir otro bin al proyecto'; plus.setAttribute('aria-label', 'Añadir bin');
  plus.addEventListener('click', (e) => {
    e.stopPropagation();
    openMenu(plus, [
      { label: 'Bin con todos los objetos', fn: () => newBin({ comps: S.st.contours.components.map((c) => c.id) }) },
      ...S.st.contours.components.map((c) => ({ label: `Bin solo con el objeto ${c.id + 1} · ${c.bbox.width.toFixed(0)} × ${c.bbox.height.toFixed(0)} mm`, fn: () => newBin({ comps: [c.id] }) })),
      '-', { label: 'Duplicar el bin actual', fn: () => newBin({ copy: true }) },
      { label: 'Complemento del bin actual (Partial bin, vacío)', fn: () => {
        const c = CFG();
        if (!c.partial || !(c.cells_off || []).length) return toast('Activa Partial bin y apaga alguna celda: el complemento rellena ese hueco', true);
        newBin({ mode: 'complement', name: `${binList().find((x) => x.id === S.bin).name} · complemento` });
      } },
      { label: 'Contenedor vacío (sin siluetas, p. ej. tornillos)', fn: () => newBin({ mode: 'empty', name: 'Contenedor' }) },
    ]);
  });
  box.append(plus);
}

/* ---- v1.3: divisores sueltos (ranuras + láminas) */
const parseList = (txt) => [...new Set(String(txt).split(/[;,\s]+/).filter((t) => t !== '').map(Number).filter((n) => Number.isFinite(n) && Math.abs(n) < 500).map((n) => Math.round(n * 10) / 10))].sort((a, b) => a - b).slice(0, 20);
function evenDividers(axis, n) {   // reparte dentro de las celdas activas (en un bin parcial, solo en su caja)
  const c = CFG(), [gw, gd] = binGrid(), u = c.grid_unit_mm, off = cellsOff();
  let i0 = 0, i1 = gw - 1, j0 = 0, j1 = gd - 1;
  if (off.size) {
    const act = [];
    for (let j = 0; j < gd; j++) for (let i = 0; i < gw; i++) if (!off.has(`${i},${j}`)) act.push([i, j]);
    if (act.length) { i0 = Math.min(...act.map((a) => a[0])); i1 = Math.max(...act.map((a) => a[0])); j0 = Math.min(...act.map((a) => a[1])); j1 = Math.max(...act.map((a) => a[1])); }
  }
  const [a0, a1, g] = axis === 'x' ? [i0, i1, gw] : [j0, j1, gd];
  const lo = (a0 - g / 2) * u + 0.25 + c.wall_thickness_mm, hi = ((a1 + 1) - g / 2) * u - 0.25 - c.wall_thickness_mm;
  return Array.from({ length: n }, (_, k) => Math.round((lo + ((k + 1) * (hi - lo)) / (n + 1)) * 10) / 10);
}
for (const [axis, nId, tId, key] of [['x', 's-dxn', 's-dxs', 'dividers_x'], ['y', 's-dyn', 's-dys', 'dividers_y']]) {
  $(nId).addEventListener('change', () => {
    const n = Math.min(10, Math.max(0, Math.round(+$(nId).value || 0)));
    CFG()[key] = evenDividers(axis, n);
    renderSettings(); renderLayout(); scheduleBuild();
  });
  $(tId).addEventListener('change', () => { CFG()[key] = parseList($(tId).value); renderSettings(); renderLayout(); scheduleBuild(); });
}
function renderDividerFields() {
  const c = CFG();
  $('s-dxn').value = (c.dividers_x || []).length; $('s-dyn').value = (c.dividers_y || []).length;
  $('s-dxs').value = (c.dividers_x || []).join(', '); $('s-dys').value = (c.dividers_y || []).join(', ');
  for (const id of ['s-dxn', 's-dyn', 's-dxs', 's-dys', 's-dth', 's-dcl', 's-dsd']) $(id).disabled = !!c.hollow;
}

/* ---- v1.3: generador de compartimentos (filas × columnas de bolsillos rectangulares) */
function openCompartments() {
  closeMenu();
  const c0 = CFG();
  const M = modalShell('Generador de compartimentos', 'Generar', true);
  M.body.innerHTML = `<p class="muted">Crea una cuadrícula de bolsillos rectangulares separados por una pared de grosor configurable, todo en una sola pieza. Se añaden como formas normales: luego puedes moverlas, cambiar su profundidad o mezclarlas con siluetas de fotos.</p>
    <div class="seg" id="cp-mode"><button data-m="fill" class="on">Rellenar la rejilla</button><button data-m="size">Tamaño de compartimento</button></div>
    <div class="cp-grid">
      <label>Columnas <input type="number" id="cp-cols" min="1" max="20" step="1" value="3"></label>
      <label>Filas <input type="number" id="cp-rows" min="1" max="20" step="1" value="2"></label>
      <label>Pared entre compartimentos (mm) <input type="number" id="cp-wall" min="0.8" max="10" step="0.1" value="1.6"></label>
      <label>Esquina redondeada (mm) <input type="number" id="cp-r" min="0" max="20" step="0.5" value="2"></label>
      <label data-f="fill">Rejilla: ancho (celdas) <input type="number" id="cp-gw" min="1" max="12" step="1" value="${Math.min(12, binGrid()[0])}"></label>
      <label data-f="fill">Rejilla: fondo (celdas) <input type="number" id="cp-gd" min="1" max="12" step="1" value="${Math.min(12, binGrid()[1])}"></label>
      <label data-f="size" class="hidden">Compartimento: ancho (mm) <input type="number" id="cp-w" min="4" max="300" step="0.5" value="30"></label>
      <label data-f="size" class="hidden">Compartimento: fondo (mm) <input type="number" id="cp-h" min="4" max="300" step="0.5" value="30"></label>
    </div>
    <label class="check"><input type="checkbox" id="cp-clear"> Quitar antes las formas actuales del bin</label>
    <svg id="cp-prev" class="mn-canvas" viewBox="-60 -40 120 80" preserveAspectRatio="xMidYMid meet"></svg>`;
  const $m = (id) => M.body.querySelector('#' + id);
  let mode = 'fill', plan = null;
  const calc = () => {
    const cols = Math.round(+$m('cp-cols').value), rows = Math.round(+$m('cp-rows').value), dw = +$m('cp-wall').value, rr = Math.max(0, +$m('cp-r').value || 0);
    const cl = c0.clearance_mm, u = c0.grid_unit_mm, margin = 0.9;   // margen hasta la pared exterior (esquinas redondeadas del bin)
    let pw, ph, gw = null, gd = null;
    if (mode === 'fill') {
      gw = Math.round(+$m('cp-gw').value); gd = Math.round(+$m('cp-gd').value);
      pw = (gw * u - 0.5 - 2 * c0.wall_thickness_mm - 2 * margin - (cols - 1) * dw) / cols;
      ph = (gd * u - 0.5 - 2 * c0.wall_thickness_mm - 2 * margin - (rows - 1) * dw) / rows;
    } else { pw = +$m('cp-w').value; ph = +$m('cp-h').value; }
    const ok = cols >= 1 && rows >= 1 && cols * rows <= 40 - (($m('cp-clear').checked) ? 0 : SHAPES().length) && dw >= 0.8 && pw - 2 * cl >= 2 && ph - 2 * cl >= 2;
    plan = ok ? { cols, rows, dw, rr, pw, ph, gw, gd, cl } : null;
    const pv = $m('cp-prev');
    pv.replaceChildren();
    if (!plan) { M.info.textContent = cols * rows > 40 ? 'Demasiados compartimentos (máx. 40 formas por bin)' : 'Medidas no válidas (¿compartimento demasiado pequeño?)'; M.go.disabled = true; return; }
    const TW = cols * pw + (cols - 1) * dw, TH = rows * ph + (rows - 1) * dw, f = Math.min(100 / TW, 64 / TH);
    const g = svgEl('g', { transform: `scale(${f})` });
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) g.append(svgEl('rect', { x: -TW / 2 + i * (pw + dw), y: -TH / 2 + j * (ph + dw), width: pw, height: ph, rx: Math.min(rr, pw / 2, ph / 2), fill: '#f5d9c8', stroke: '#e0561a', 'stroke-width': 0.8 / f }));
    pv.append(g);
    M.info.textContent = `${cols * rows} compartimentos de ${pw.toFixed(1)} × ${ph.toFixed(1)} mm · conjunto ${TW.toFixed(0)} × ${TH.toFixed(0)} mm`;
    M.go.disabled = false;
  };
  M.body.querySelectorAll('input').forEach((el) => el.addEventListener('input', calc));
  M.body.querySelectorAll('#cp-mode button').forEach((b) => b.addEventListener('click', () => {
    mode = b.dataset.m;
    M.body.querySelectorAll('#cp-mode button').forEach((x) => x.classList.toggle('on', x === b));
    M.body.querySelectorAll('[data-f]').forEach((l) => l.classList.toggle('hidden', l.dataset.f !== mode));
    calc();
  }));
  M.go.addEventListener('click', () => {
    if (!plan) return;
    const p = plan;
    M.close();
    if (M.body.querySelector('#cp-clear') && $m('cp-clear').checked) D().shapes = [];
    const w = p.pw - 2 * p.cl, h = p.ph - 2 * p.cl, r = Math.max(0, p.rr - p.cl);
    const ring = primitiveRing('rect', { w, h, r });
    const TW = p.cols * p.pw + (p.cols - 1) * p.dw, TH = p.rows * p.ph + (p.rows - 1) * p.dw;
    const [nu, nv] = defaultNotchUV(w, h);
    let last = null;
    for (let j = 0; j < p.rows; j++) for (let i = 0; i < p.cols; i++) {
      const sh = { id: newId(), name: `Comp ${j * p.cols + i + 1}`, comp: null, outer: ring.map((q) => [r2(q[0]), r2(q[1])]), holes: [], mirror: false, offset_mm: 0, depth_mm: null, keep_holes: false, rot: 0,
        x: r2(-TW / 2 + p.pw / 2 + i * (p.pw + p.dw)), y: r2(-TH / 2 + p.ph / 2 + j * (p.ph + p.dw)), notch: { on: false, d: 16, u: nu, v: nv } };
      SHAPES().push(sh); last = sh;
    }
    if (p.gw) { CFG().auto_size = false; CFG().partial = false; CFG().grid_width = p.gw; CFG().grid_depth = p.gd; } else { CFG().auto_size = true; CFG().partial = false; }
    E.meta = null; E.sel = last.id; E.multi.clear(); E.tool = null; E.pts = false;
    refreshEditor(); scheduleBuild();
    toast(`${p.cols * p.rows} compartimentos añadidos`);
  });
  calc();
}

/* ---- v1.3: biblioteca de formas guardadas con nombre */
function bakedShape(sh) {
  const g = localGeom(sh), xs = g.outer.map((p) => p[0]), ys = g.outer.map((p) => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const f = (ring) => ring.map((p) => [r2(p[0] - cx), r2(p[1] - cy)]);
  return { outer: f(g.outer), holes: g.holes.map(f), depth_mm: sh.depth_mm ?? null, offset_mm: sh.offset_mm || 0, notch: { on: !!sh.notch.on, d: sh.notch.d, u: r2(sh.notch.u - cx), v: r2(sh.notch.v - cy) } };
}
function openSaveToLibrary(sh) {
  closeMenu();
  const M = modalShell('Guardar en la biblioteca', 'Guardar');
  M.body.innerHTML = `<div class="cp-grid"><label>Nombre <input type="text" id="lb-name" maxlength="80" value="${sh.name.replace(/"/g, '&quot;')}"></label>
    <label>Etiquetas (separadas por comas) <input type="text" id="lb-tags" placeholder="llaves, destornilladores" autocomplete="off"></label></div>
    <p class="muted">Se guarda la silueta tal como está ahora (espejo, edición de puntos, offset, profundidad y muesca incluidos) para usarla en cualquier proyecto.</p>`;
  const $m = (id) => M.body.querySelector('#' + id);
  M.go.disabled = false;
  M.go.addEventListener('click', async () => {
    const name = $m('lb-name').value.trim();
    if (!name) return;
    const body = { name, tags: $m('lb-tags').value.split(','), ...bakedShape(sh) };
    M.close();
    const r = await busy('Guardando…', () => api('/api/library', { method: 'POST', headers: JSON_HDR, body: JSON.stringify(body) }));
    if (r) toast(`«${name}» guardada en la biblioteca`);
  });
}
async function openLibrary() {
  closeMenu();
  const M = modalShell('Biblioteca de formas', 'Añadir al bin', true);
  M.body.innerHTML = `<div class="lib-tools"><input type="search" id="lb-q" placeholder="Buscar por nombre o etiqueta…" aria-label="Buscar"><span id="lb-chips"></span></div><div class="imp-grid" id="lb-grid"><p class="muted">Cargando…</p></div>`;
  const $m = (id) => M.body.querySelector('#' + id);
  let items = [], tag = '';
  const pick = new Set();
  const upd = () => { M.info.textContent = `${pick.size} seleccionada${pick.size === 1 ? '' : 's'}`; M.go.disabled = !pick.size; };
  const draw = () => {
    const q = $m('lb-q').value.trim().toLowerCase(), grid = $m('lb-grid');
    const tags = [...new Set(items.flatMap((i) => i.tags))].sort();
    const chips = $m('lb-chips');
    chips.replaceChildren(...tags.map((t) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'chip' + (t === tag ? ' on' : ''); b.textContent = t; b.addEventListener('click', () => { tag = tag === t ? '' : t; draw(); }); return b; }));
    const list = items.filter((i) => (!tag || i.tags.includes(tag)) && (!q || i.name.toLowerCase().includes(q) || i.tags.some((t) => t.includes(q))));
    grid.replaceChildren();
    if (!items.length) { grid.innerHTML = '<p class="muted">La biblioteca está vacía. Abre el menú «···» de una forma y elige «Guardar en la biblioteca».</p>'; return; }
    if (!list.length) { grid.innerHTML = '<p class="muted">Sin resultados.</p>'; return; }
    for (const it of list) {
      const card = document.createElement('div');
      card.className = 'imp-card lib-card' + (pick.has(it.id) ? ' on' : '');
      card.tabIndex = 0; card.setAttribute('role', 'button'); card.dataset.lib = it.id;
      const nm = document.createElement('span'); nm.className = 'lib-name'; nm.textContent = it.name; nm.title = it.name;
      const cap = document.createElement('span'); cap.textContent = `${it.bbox.width.toFixed(0)} × ${it.bbox.height.toFixed(0)} mm`;
      const tg = document.createElement('span'); tg.className = 'lib-tags'; tg.textContent = it.tags.join(' · ');
      const ed = document.createElement('button'); ed.type = 'button'; ed.className = 'lib-x lib-e'; ed.textContent = 'Editar'; ed.setAttribute('aria-label', `Editar ${it.name}`);
      const del = document.createElement('button'); del.type = 'button'; del.className = 'lib-x'; del.textContent = 'Borrar'; del.setAttribute('aria-label', `Borrar ${it.name}`);
      ed.addEventListener('click', async (e) => {
        e.stopPropagation();
        const n = (window.prompt('Nombre', it.name) || '').trim(); if (!n) return;
        const t = window.prompt('Etiquetas (separadas por comas)', it.tags.join(', '));
        if (t === null) return;
        try { Object.assign(it, await api(`/api/library/${it.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: n, tags: t.split(',') }) })); draw(); } catch (er) { toast(er.message, true); }
      });
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!window.confirm(`¿Borrar «${it.name}» de la biblioteca?`)) return;
        try { await api(`/api/library/${it.id}`, { method: 'DELETE' }); items = items.filter((x) => x.id !== it.id); pick.delete(it.id); draw(); upd(); } catch (er) { toast(er.message, true); }
      });
      card.append(thumbSvg({ outer: it.outer, holes: it.holes, bbox: it.bbox }), nm, cap, tg, ed, del);
      const toggle = () => { if (pick.has(it.id)) { pick.delete(it.id); card.classList.remove('on'); } else { pick.add(it.id); card.classList.add('on'); } upd(); };
      card.addEventListener('click', toggle);
      card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      grid.append(card);
    }
  };
  $m('lb-q').addEventListener('input', draw);
  try { items = await api('/api/library'); } catch (e) { $m('lb-grid').textContent = e.message; return; }
  draw(); upd();
  M.go.addEventListener('click', () => {
    const sel = items.filter((i) => pick.has(i.id));
    if (!sel.length) return;
    M.close();
    importShapes(sel.map((i) => ({ c: { id: 0, outer: i.outer, holes: i.holes, bbox: i.bbox }, name: i.name, label: i.name, withHoles: true, lib: i })));
    toast(`${sel.length} forma${sel.length === 1 ? '' : 's'} de la biblioteca añadida${sel.length === 1 ? '' : 's'}`);
  });
}

/* ---- v1.3: fusionar proyectos existentes (Mis assets) */
$('btn-merge').addEventListener('click', async () => {
  let list = [];
  try { list = (await api('/api/sessions')).filter((s) => s.traced); } catch (e) { return toast(e.message, true); }
  if (list.length < 2) return toast('Necesitas al menos dos proyectos con siluetas trazadas', true);
  const M = modalShell('Fusionar proyectos', 'Crear proyecto fusionado');
  M.body.innerHTML = `<p class="muted">Crea un proyecto nuevo con las siluetas de los proyectos que marques (los originales no cambian). Después eliges qué objetos llevar al bin como siempre.</p>
    <label class="inline">Nombre <input type="text" id="mg-name" maxlength="80" placeholder="Fusión: …"></label><div class="merge-list" id="mg-list"></div>`;
  const pick = new Set(), box = M.body.querySelector('#mg-list');
  const upd = () => { M.info.textContent = `${pick.size} proyecto${pick.size === 1 ? '' : 's'}`; M.go.disabled = pick.size < 2; };
  for (const s of list) {
    const l = document.createElement('label');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.dataset.sid = s.id;
    cb.addEventListener('change', () => { if (cb.checked) pick.add(s.id); else pick.delete(s.id); upd(); });
    const t = document.createElement('span'); t.textContent = `${s.name} · ${s.n_components} objeto${s.n_components === 1 ? '' : 's'}`;
    l.append(cb, t); box.append(l);
  }
  upd();
  M.go.addEventListener('click', async () => {
    const ids = [...pick], name = M.body.querySelector('#mg-name').value.trim();
    M.close();
    const st = await busy('Fusionando…', () => api('/api/sessions/merge', { method: 'POST', headers: JSON_HDR, body: JSON.stringify({ ids, name: name || null }) }));
    if (st) { toast('Proyecto fusionado creado'); openSession(st.id); }
  });
});

/* ---- puesta en marcha del editor */
function refreshEditor() { renderHist(); renderBinTabs(); renderTabs(); renderToolbar(); renderPop(); renderSettings(); renderLayout(); }

$('ed-back').addEventListener('click', () => go('shape'));
$('ed-name').addEventListener('change', async () => {
  const v = $('ed-name').value.trim();
  if (!v || !S.st) { $('ed-name').value = S.st.name; return; }
  try { await api(`/api/sessions/${S.st.id}`, { method: 'PATCH', headers: JSON_HDR, body: JSON.stringify({ name: v }) }); S.st.name = v; } catch (e) { toast(e.message, true); }
});

async function renderBin() {
  const st = S.st;
  if (!st.contours) return go('corners');
  $('ed-name').value = st.name;
  initViewer();
  clearTimeout(E.timer);
  E.sel = null; E.multi.clear(); E.tool = null; E.pts = false; E.meta = st.bin_meta || null;
  setDownloads(!!st.bin_meta);
  if (!st.design) {
    if (!S.sel.size) return go('shape');
    const d = await busy('Colocando las formas…', () => api(`/api/sessions/${st.id}/design/init?bin=${S.bin}`, { method: 'POST', headers: JSON_HDR, body: JSON.stringify({ components: [...S.sel].sort((a, b) => a - b) }) }));
    if (!d) return go('shape');
    st.design = d; st.bin_meta = null; E.meta = null;
    setDownloads(false);
  } else {
    st.design.settings = { ...DEFAULT_SETTINGS, ...st.design.settings };
  }
  refreshEditor();
  histReset();
  if (st.bin_meta) { showInfo(st.bin_meta); setStatus('ok', 'Listo'); $('ed-saved').textContent = 'Guardado'; loadModels(st.bin_meta); }
  else { $('bin-info').innerHTML = ''; $('bin-warn').replaceChildren(); scheduleBuild(50); }
}

/* ---- visor 3D (bin + inserto) */
const PALETTES = [
  { bin: 0x3b3b3f, insert: 0xe0561a, lid: 0x6b6b72, dividers: 0xe9a23b }, { bin: 0xc9c6bd, insert: 0x2d6cdf, lid: 0x9d9a90, dividers: 0xd9822b }, { bin: 0x2f6f4f, insert: 0xf2b134, lid: 0x58a07e, dividers: 0xe8d7a1 }, { bin: 0x4a4e8a, insert: 0xe8d7a1, lid: 0x7c80c4, dividers: 0xf2b134 },
];
function initViewer() {
  if (S.viewer) { S.viewer.resize(); return; }
  const el = $('viewer');
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (e) {
    el.innerHTML = '<p style="padding:20px;color:#6b6b70">Tu navegador no pudo iniciar WebGL, así que no hay vista previa 3D. Puedes descargar los STL igualmente.</p>';
    S.viewer = { noGL: true, resize() {}, load() {}, clear() {}, view() {}, palette() {}, setLid() {}, cycleLid() { return 'on'; } };
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
  const grid = new THREE.GridHelper(900, 90, 0xcfcfd2, 0xe2e2e5);
  grid.rotation.x = Math.PI / 2; grid.position.z = -0.05;
  scene.add(grid);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const mats = { bin: new THREE.MeshStandardMaterial({ color: PALETTES[0].bin, roughness: 0.6, metalness: 0.05 }), insert: new THREE.MeshStandardMaterial({ color: PALETTES[0].insert, roughness: 0.5, metalness: 0.05 }), lid: new THREE.MeshStandardMaterial({ color: PALETTES[0].lid, roughness: 0.6, metalness: 0.05 }), dividers: new THREE.MeshStandardMaterial({ color: PALETTES[0].dividers, roughness: 0.55, metalness: 0.05 }) };
  const loader = new STLLoader();
  const meshes = { bin: null, insert: null, lid: null, dividers: null };
  let size = new THREE.Vector3(120, 120, 30), pal = 0, lidZ = 20, lidMode = 'on';
  const placeLid = () => { if (!meshes.lid) return; meshes.lid.visible = lidMode !== 'off'; meshes.lid.position.z = lidZ + (lidMode === 'apart' ? Math.max(25, size.z * 1.1) : 0); };
  const v = {
    renderer, scene, camera, controls,
    resize() {
      const w = el.clientWidth, h = el.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
    },
    clear(kind) {
      for (const k of kind ? [kind] : ['bin', 'insert', 'lid', 'dividers']) if (meshes[k]) { scene.remove(meshes[k]); meshes[k].geometry.dispose(); meshes[k] = null; }
    },
    load(url, kind, fit) {
      return new Promise((resolve, reject) => loader.load(url, (g) => {
        v.clear(kind);
        g.computeBoundingBox();
        meshes[kind] = new THREE.Mesh(g, mats[kind]);
        scene.add(meshes[kind]);
        if (kind === 'bin') { size = g.boundingBox.getSize(new THREE.Vector3()); if (fit) v.view('reset'); }
        if (kind === 'lid') placeLid();
        if (kind === 'dividers') meshes.dividers.position.set(0, size.y / 2 + 8, 0);
        resolve();
      }, undefined, reject));
    },
    view(name) {
      const d = Math.max(size.x, size.y, size.z * 2) * 2.1;
      const z = size.z / 2;
      const P = { reset: [d * 0.7, -d * 0.9, d * 0.8 + z], iso: [d * 0.7, -d * 0.9, d * 0.8 + z], top: [0, -0.01, d * 1.15], front: [0, -d * 1.1, z], side: [d * 1.1, 0, z] }[name];
      if (!P) return;
      controls.target.set(0, 0, name === 'top' ? 0 : z);
      camera.position.set(...P);
      camera.near = d / 100; camera.far = d * 20; camera.updateProjectionMatrix();
      controls.update();
    },
    palette() { pal = (pal + 1) % PALETTES.length; mats.bin.color.setHex(PALETTES[pal].bin); mats.insert.color.setHex(PALETTES[pal].insert); mats.lid.color.setHex(PALETTES[pal].lid); mats.dividers.color.setHex(PALETTES[pal].dividers); },
    setLid(z) { lidZ = z; placeLid(); },
    cycleLid() { lidMode = { on: 'apart', apart: 'off', off: 'on' }[lidMode]; placeLid(); return lidMode; },
  };
  new ResizeObserver(() => v.resize()).observe(el);
  v.resize();
  renderer.setAnimationLoop(() => { controls.update(); renderer.render(scene, camera); });
  S.viewer = v;
}
let firstFit = true;
async function loadModels(meta) {
  if (!S.viewer || S.viewer.noGL) return;
  const id = S.st.id, q = `bin=${S.bin}&t=${meta.stamp}`;
  try {
    await S.viewer.load(`/api/sessions/${id}/bin.stl?${q}`, 'bin', firstFit);
    firstFit = false;
    if (meta.has_insert) await S.viewer.load(`/api/sessions/${id}/insert.stl?${q}`, 'insert', false);
    else S.viewer.clear('insert');
    if (meta.has_dividers) await S.viewer.load(`/api/sessions/${id}/dividers.stl?${q}`, 'dividers', false);
    else S.viewer.clear('dividers');
    const lb = $('v-lid');
    lb.classList.toggle('hidden', !meta.has_lid);
    if (meta.has_lid) { S.viewer.setLid(meta.stats.lid_z); await S.viewer.load(`/api/sessions/${id}/lid.stl?${q}`, 'lid', false); }
    else S.viewer.clear('lid');
  } catch (e) { toast('No se pudo cargar la vista previa 3D', true); }
}
document.querySelectorAll('#vbar button').forEach((b) => b.addEventListener('click', () => {
  if (!S.viewer) return;
  if (b.dataset.view === 'palette') S.viewer.palette();
  else if (b.dataset.view === 'lid') b.textContent = 'Tapa: ' + { on: 'montada', apart: 'separada', off: 'oculta' }[S.viewer.cycleLid()];
  else S.viewer.view(b.dataset.view);
}));

/* ------------------------------------------------------------------ arranque */
updateSteps();
go('home');
