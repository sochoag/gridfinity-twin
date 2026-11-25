"""Foto + hoja de papel -> contornos en mm.

Pipeline (equivalente funcional al servicio /api/process-contours de ShapeAny):
  1. detectar las 4 esquinas de la hoja
  2. homografia -> imagen rectificada a escala fija (PX_PER_MM)
  3. segmentar objetos por distancia de color al papel
  4. contornos externos/huecos, simplificados, en mm (origen arriba-izq, Y abajo)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import cv2
import numpy as np

PAPERS = {"A4": (297.0, 210.0), "LETTER": (279.4, 215.9)}
PX_PER_MM = 4
NEAR_MM = 30  # radio alrededor de lo detectado donde se buscan bordes debiles (puntas, tapas)


@dataclass
class Result:
    paper_mm: tuple
    corners: np.ndarray            # 4x2 en la foto original (TL, TR, BR, BL)
    rectified: np.ndarray          # BGR, PX_PER_MM px/mm
    mask: np.ndarray               # uint8 0/255 sobre la rectificada
    outer: list = field(default_factory=list)   # listas de (x_mm, y_mm)
    holes: list = field(default_factory=list)


def order_corners(pts: np.ndarray) -> np.ndarray:
    """TL, TR, BR, BL (sentido horario en pantalla). Robusto a hojas giradas: ordena por angulo
    alrededor del centro y arranca en la esquina mas cercana a la arriba-izquierda."""
    pts = pts.astype(np.float32)
    c = pts.mean(0)
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]      # horario con Y hacia abajo
    start = int(np.argmin(pts.sum(1)))
    return np.roll(pts, -start, axis=0).astype(np.float32)


def _line_intersect(l1, l2):
    (vx1, vy1, x1, y1), (vx2, vy2, x2, y2) = l1, l2
    A = np.array([[vx1, -vx2], [vy1, -vy2]], np.float64)
    t = np.linalg.solve(A, np.array([x2 - x1, y2 - y1], np.float64))
    return np.array([x1 + t[0] * vx1, y1 + t[0] * vy1], np.float32)


def _largest_contour(mask, k):
    ell = lambda n: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))
    m = cv2.morphologyEx(mask, cv2.MORPH_OPEN, ell(k))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ell(4 * k + 1))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        raise RuntimeError("No se encontro la hoja")
    return max(cnts, key=cv2.contourArea)


def _quad_from_contour(c):
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    for eps in np.linspace(0.01, 0.08, 15):
        ap = cv2.approxPolyDP(hull, eps * peri, True)
        if len(ap) == 4:
            return order_corners(ap.reshape(4, 2))
    return order_corners(cv2.boxPoints(cv2.minAreaRect(hull)))


def _fit_edge(pts, p0, p1, band):
    d = p1 - p0
    n = np.array([-d[1], d[0]]) / (np.linalg.norm(d) + 1e-9)
    t = ((pts - p0) @ d) / (d @ d)
    dist = np.abs((pts - p0) @ n)
    sel = pts[(t > 0.15) & (t < 0.85) & (dist < band)]
    if len(sel) < 10:
        return None
    return cv2.fitLine(sel, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()


def _detect_paper_legacy(img: np.ndarray) -> np.ndarray:
    """Esquinas de la hoja.

    Papel = region de croma bajo (neutra/azulada); la madera es calida (b alto).
    Dos mascaras:
      - estricta (muy clara + croma bajo): borde superior limpio, pero pierde zonas en sombra
      - tolerante (croma bajo, cualquier luz): bordes izq/der/inferior completos, pero un reflejo
        de luz sobre la madera se pega al borde superior
    Se ajusta una recta robusta por borde usando la mascara fiable para cada uno y se intersectan.
    """
    h, w = img.shape[:2]
    scale = 800 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (5, 5), 0), cv2.COLOR_BGR2LAB).astype(np.int16)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    k = int(round(15 * scale)) | 1
    strict = _largest_contour(((L > 190) & (b < 118) & (a < 122)).astype(np.uint8) * 255, k)
    loose = _largest_contour(((L > 100) & (b < 121) & (a < 123)).astype(np.uint8) * 255, k)
    qs, ql = _quad_from_contour(strict), _quad_from_contour(loose)
    # esquinas iniciales: superiores de la estricta, inferiores de la tolerante
    quad = np.array([qs[0], qs[1], ql[2], ql[3]], np.float32)
    ps, pl = strict.reshape(-1, 2).astype(np.float32), loose.reshape(-1, 2).astype(np.float32)
    band = 0.02 * max(small.shape[:2])
    lines = []
    for i in range(4):
        pts = ps if i == 0 else pl          # i == 0: borde superior (TL->TR)
        ln = _fit_edge(pts, quad[i], quad[(i + 1) % 4], band)
        if ln is None:
            lines = None
            break
        lines.append(ln)
    if lines is not None:
        quad = np.array([_line_intersect(lines[(i - 1) % 4], lines[i]) for i in range(4)], np.float32)
    return order_corners(quad / scale)


def _paper_candidates(small):
    """Mascaras candidatas de papel. El papel es azulado/neutro y la madera calida: el croma 'calido'
    (a + b respecto al gris) separa bien en cualquier luz, incluso con sombras (que solo cambian L)."""
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (7, 7), 0), cv2.COLOR_BGR2LAB).astype(np.float32)
    warm = (lab[..., 1] - 128) + (lab[..., 2] - 128)
    t_otsu, _ = cv2.threshold(np.clip((warm + 40) * 3, 0, 255).astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    t0 = float(np.clip(t_otsu / 3 - 40, -8, 8))
    L = lab[..., 0]
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mu = cv2.blur(g, (7, 7))
    sd = np.sqrt(np.maximum(cv2.blur(g * g, (7, 7)) - mu * mu, 0))     # textura local: papel liso, madera con veta
    smooth = cv2.dilate(((sd < 4.5) | ((sd < 7.0) & (warm < -22))).astype(np.uint8),   # sombra: mas ruido, pero croma claramente de papel
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))) > 0
    for lf in (0.55, 0.3):                                    # 2a pasada: tolera hojas con media sombra
        for dt in (0, -5, 5, -10, 10):
            m = warm < t0 + dt
            if m.sum() > 500:
                lp = np.percentile(L[m], 85)                  # luz tipica del papel en esta foto
                m &= L > max(70.0 if lf > 0.5 else 45.0, lf * lp)   # descarta madera oscura / sombra de objetos grandes
            yield (m & smooth).astype(np.uint8) * 255


def _score_quad(contour, quad, shape):
    h, w = shape[:2]
    area = cv2.contourArea(contour)
    qa = cv2.contourArea(quad.astype(np.float32))
    if qa < 0.04 * h * w:
        return -1.0
    fill = area / (qa + 1e-9)                              # cuanto del cuadrilatero es papel
    e = [np.linalg.norm(quad[(i + 1) % 4] - quad[i]) for i in range(4)]
    long_, short_ = max((e[0] + e[2]) / 2, (e[1] + e[3]) / 2), min((e[0] + e[2]) / 2, (e[1] + e[3]) / 2)
    ratio = long_ / (short_ + 1e-9)
    asp = max(0.0, 1.0 - min(abs(ratio - 1.414), abs(ratio - 1.294)) / 0.45)    # A4 = 1.414, Letter = 1.294
    # un cuadrilatero casi opuesto-paralelo (rectangulo en perspectiva suave)
    v = [quad[(i + 1) % 4] - quad[i] for i in range(4)]
    cosang = lambda a, b: abs(float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    par = 1.0 - 0.5 * (cosang(v[0], v[2]) < 0.9) - 0.5 * (cosang(v[1], v[3]) < 0.9)
    touch = sum(1 for p in quad if p[0] < 2 or p[1] < 2 or p[0] > w - 3 or p[1] > h - 3)
    return (fill ** 2) * (0.35 + 0.65 * asp) * (0.5 + 0.5 * par) * (qa / (h * w)) ** 0.25 * (0.85 ** touch)


def detect_paper(img: np.ndarray) -> np.ndarray:
    """Esquinas de la hoja (TL, TR, BR, BL) en la foto original.

    1) mascaras candidatas por croma (umbral adaptativo + variantes), 2) componentes grandes con los
    huecos de objetos rellenados, 3) se elige el cuadrilatero que mejor se parece a una hoja
    (relleno, proporcion A4/Letter, lados paralelos), 4) cada borde se reajusta con una recta robusta
    (Huber) para ignorar objetos o reflejos que muerden el borde y se intersectan las rectas."""
    h, w = img.shape[:2]
    scale = 800 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    k = max(3, int(round(9 * scale * 2)) | 1)
    ell = lambda n: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (n, n))
    best, best_s, best_c = None, 0.0, None
    for m in _paper_candidates(small):
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ell(k))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ell(2 * k + 1))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:4]:
            if cv2.contourArea(c) < 0.04 * sh * sw:
                continue
            quad = _quad_from_contour(c)
            sc = _score_quad(c, quad, small.shape)
            if sc > best_s:
                best, best_s, best_c = quad, sc, c
    if best is None or best_s < 0.12:
        return _detect_paper_legacy(img)
    # refinar cada borde con una recta robusta sobre los puntos del contorno cercanos a el
    pts = best_c.reshape(-1, 2).astype(np.float32)
    band = 0.012 * max(sh, sw)
    quad = order_corners(best)
    lines = []
    for i in range(4):
        ln = _fit_edge(pts, quad[i], quad[(i + 1) % 4], band)
        if ln is None:
            lines = None
            break
        lines.append(ln)
    if lines is not None:
        try:
            q2 = np.array([_line_intersect(lines[(i - 1) % 4], lines[i]) for i in range(4)], np.float32)
            if cv2.contourArea(q2) > 0.8 * cv2.contourArea(quad) and cv2.contourArea(q2) < 1.2 * cv2.contourArea(quad):
                quad = q2
        except np.linalg.LinAlgError:
            pass
    return order_corners(quad / scale)


def rectify(img, corners, paper_mm):
    pw, ph = paper_mm
    top = np.linalg.norm(corners[1] - corners[0])
    left = np.linalg.norm(corners[3] - corners[0])
    # orientacion: lado largo horizontal en la foto -> papel apaisado
    if top < left:
        pw, ph = ph, pw
    W, H = int(round(pw * PX_PER_MM)), int(round(ph * PX_PER_MM))
    dst = np.array([[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]], np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_CUBIC), (pw, ph)


def segment(rect: np.ndarray, margin_mm: float = 8.0) -> np.ndarray:
    """Mascara de objetos: distancia en Lab al color de papel (estimado localmente)."""
    H, W = rect.shape[:2]
    lab = cv2.cvtColor(rect, cv2.COLOR_BGR2LAB).astype(np.float32)
    # fondo de papel: mediana por bloques en la franja libre del borde, interpolada
    border = np.zeros((H, W), bool)
    b = int(14 * PX_PER_MM)
    border[:b] = border[-b:] = True
    border[:, :b] = border[:, -b:] = True
    paper = np.array([np.median(lab[..., k][border]) for k in range(3)], np.float32)
    # el papel tiene gradiente de iluminacion: lo modelamos con un plano ajustado en el borde
    ys, xs = np.nonzero(border)
    A = np.c_[xs / W, ys / H, np.ones_like(xs)].astype(np.float32)
    plane = np.empty_like(lab)
    gx, gy = np.meshgrid(np.arange(W) / W, np.arange(H) / H)
    G = np.c_[gx.ravel(), gy.ravel(), np.ones(W * H)].astype(np.float32)
    for k in range(3):
        coef, *_ = np.linalg.lstsq(A, lab[..., k][border], rcond=None)
        plane[..., k] = (G @ coef.astype(np.float32)).reshape(H, W)
    d = lab - plane
    # pesos: luminosidad pesa menos (las sombras cambian L pero no el croma)
    dist = np.sqrt((0.4 * d[..., 0]) ** 2 + d[..., 1] ** 2 + d[..., 2] ** 2)
    dist8 = np.clip(dist * 4, 0, 255).astype(np.uint8)
    dist8 = cv2.GaussianBlur(dist8, (0, 0), 1.2)
    t, mask = cv2.threshold(dist8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    # completar zonas casi del color del papel (puntas metalicas, tapas transparentes):
    # contornos por gradiente -> cierre -> relleno de huecos pequenos, solo cerca de objetos ya detectados
    gray = cv2.GaussianBlur(cv2.cvtColor(rect, cv2.COLOR_BGR2GRAY), (0, 0), 1.5)
    edges = cv2.Canny(gray, 25, 70)
    edges_weak = cv2.Canny(gray, 12, 36)                # solo para medir la 'ocupacion' de huecos (codigos, texto)
    near = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(NEAR_MM * 2) * PX_PER_MM | 1,) * 2))
    edges_raw = edges_weak
    edges = cv2.bitwise_and(edges, near)
    edges = cv2.dilate(edges, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(3 * PX_PER_MM) | 1,) * 2)
    shape = cv2.morphologyEx(cv2.bitwise_or(mask, edges), cv2.MORPH_CLOSE, ek)
    # rellenar huecos acotados (interior de puntas/tapas), no los grandes
    inv = cv2.bitwise_not(shape)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(inv)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        touches = x == 0 or y == 0 or x + w == W or y + h == H
        if touches:
            continue
        if a < 120 * PX_PER_MM ** 2:
            shape[lbl == i] = 255
        elif a < 9000 * PX_PER_MM ** 2:
            # hueco encerrado grande: si por dentro NO es papel limpio forma parte del objeto
            # (hoja metalica de una espatula, estuche transparente con etiquetas y codigos de barras);
            # si es papel limpio y liso (agujero real, p. ej. los del MDF) se conserva como hueco
            inside = lbl == i
            frac = float(np.mean(dist8[inside] > 0.5 * max(t, 8)))
            core = cv2.erode(inside.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * 3 + 1,) * 2)) > 0
            busy = float(np.mean(edges_raw[core] > 0)) if core.any() else 0.0
            if frac > 0.30 or busy > 0.012:
                shape[inside] = 255
    mask = cv2.morphologyEx(shape, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    # unir partes de un mismo objeto (p. ej. dos mangos de unas pinzas) si estan a < 6 mm
    gk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(6 * PX_PER_MM) | 1,) * 2)
    grouped = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, gk)
    n, lbl, st, _ = cv2.connectedComponentsWithStats(grouped)
    out = np.zeros_like(mask)
    m = int(margin_mm * PX_PER_MM)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        near_border = x < m or y < m or x + w > W - m or y + h > H - m
        if a > 150 * PX_PER_MM ** 2 and not near_border:
            out[lbl == i] = grouped[lbl == i]
    return out


def apply_edits(mask: np.ndarray, edits: list) -> np.ndarray:
    """Aplica pinceladas/lazos manuales (en mm sobre la rectificada) a la mascara automatica."""
    out = mask.copy()
    H, W = out.shape[:2]
    for e in edits:
        val = 255 if e["op"] == "add" else 0
        pts = np.array([[x * PX_PER_MM, y * PX_PER_MM] for x, y in e["pts"]], np.float32)
        if len(pts) == 0:
            continue
        ip = np.round(pts).astype(np.int32)
        if e.get("kind", "brush") == "lasso":
            if len(ip) >= 3:
                cv2.fillPoly(out, [ip.reshape(-1, 1, 2)], val)
        else:
            r = max(1, int(round(e.get("r", 5.0) * PX_PER_MM)))
            if len(ip) > 1:
                cv2.polylines(out, [ip.reshape(-1, 1, 2)], False, val, thickness=2 * r, lineType=cv2.LINE_8)
            for x, y in ip:
                cv2.circle(out, (int(x), int(y)), r, val, -1)
    return out


def extract_contours(mask, eps_mm=0.35, min_area_mm2=150.0, keep_holes=True):
    cnts, hier = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    outer, holes = [], []
    if hier is None:
        return outer, holes
    f = 1.0 / PX_PER_MM

    def to_mm(c):
        ap = cv2.approxPolyDP(c, eps_mm * PX_PER_MM, True).reshape(-1, 2)
        return [(float(x) * f, float(y) * f) for x, y in ap]

    for c, hh in zip(cnts, hier[0]):
        area = cv2.contourArea(c) * f * f
        if hh[3] == -1 and area >= min_area_mm2:
            outer.append(to_mm(c))
        elif hh[3] != -1 and keep_holes and area >= 8.0:
            holes.append(to_mm(c))
    return outer, holes


def run_image(img: np.ndarray, paper_mm, corners=None, eps_mm=0.35, keep_holes=True) -> Result:
    """Pipeline completo sobre una imagen ya decodificada. paper_mm = (ancho, alto) en mm."""
    corners = order_corners(np.array(corners, np.float32)) if corners is not None else detect_paper(img)
    rect, paper_mm = rectify(img, corners, paper_mm)
    mask = segment(rect)
    outer, holes = extract_contours(mask, eps_mm=eps_mm, keep_holes=keep_holes)
    return sort_components(Result(paper_mm, corners, rect, mask, outer, holes))


def sort_components(r: Result) -> Result:
    """Orden estable izquierda -> derecha; cada hueco queda tras su objeto (por contencion)."""
    from shapely.geometry import Polygon

    order = sorted(range(len(r.outer)), key=lambda i: min(p[0] for p in r.outer[i]))
    r.outer = [r.outer[i] for i in order]
    r.holes = sorted(r.holes, key=lambda h: min(p[0] for p in h))
    return r


def run(image_path: str, paper: str = "A4", corners=None, **kw) -> Result:
    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(image_path)
    return run_image(img, PAPERS[paper], corners, **kw)


def to_shapeany_json(r: Result) -> dict:
    xs = [p[0] for c in r.outer for p in c]
    ys = [p[1] for c in r.outer for p in c]
    return {
        "document": {
            "paper": {"width_mm": r.paper_mm[0], "height_mm": r.paper_mm[1]},
            "outer_contours": [[list(p) for p in c] for c in r.outer],
            "hole_contours": [[list(p) for p in c] for c in r.holes],
            "bbox": {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)},
        },
        "isManual": False,
        "revision": 1,
    }


if __name__ == "__main__":
    import sys

    res = run(sys.argv[1])
    print(json.dumps(to_shapeany_json(res))[:400])
