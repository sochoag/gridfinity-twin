"""Generador de bins Gridfinity con cutouts a partir de contornos en mm.

Convenciones (Gridfinity estandar, valores habituales de la comunidad):
  - paso de rejilla 42 mm; el bin ocupa 42*n - 0.5 mm (holgura de 0.25 mm por lado)
  - unidad de altura 7 mm; la altura del cuerpo (sin labio) = 7 * heightUnits
  - pie: chaflan 0.8 @45deg + 1.8 vertical + 2.15 @45deg = 4.75 mm de alto, esquina exterior R 3.75 mm
  - stacking lip: 4.4 mm (1.9 chaflan + 1.8 vertical + 0.7 chaflan), pared base 2.6 mm; termina con 0.2 mm de plano
    (un filo de 0 mm genera triangulos degenerados y no se imprime)
  - imanes: 6.5 mm de diametro x 2.4 mm de profundidad a +-13 mm del centro de cada celda
Los contornos llegan con origen arriba-izquierda y Y hacia abajo (como en ShapeAny); aqui se
voltea Y y se centra el conjunto en el bin.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from manifold3d import CrossSection, FillRule, Manifold
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

GRID = 42.0
BIN_GAP = 0.5
HEIGHT_UNIT = 7.0
FOOT_H = 4.75
FOOT_PROFILE = [(0.8, 0.8), (1.8, 0.0), (2.15, 2.15)]  # (alto, desplazamiento horizontal) por tramo
CORNER_R = 3.75
LIP_H = 4.4
SEAM_EPS = 0.002   # el cuerpo es 2 um mas chico que el borde del pie: evita bordes exactamente coincidentes
LIP_TOP_FLAT = 0.2 # el labio termina con 0.2 mm de plano en vez de un filo de 0 mm


@dataclass
class BinConfig:
    grid_width: int | None = None        # celdas en X (None -> auto)
    grid_depth: int | None = None        # celdas en Y (None -> auto)
    grid_unit_mm: float = GRID
    height_units: int = 4
    clearance_mm: float = 1.0            # holgura alrededor de la silueta
    cutout_depth_mm: float = 12.0
    stacking_lip: bool = True
    magnet_holes: bool = False
    magnet_diameter_mm: float = 6.5
    magnet_depth_mm: float = 2.4
    finger_notches: str = "none"         # none | one_end | both_ends
    finger_diameter_mm: float = 16.0
    keep_holes: bool = False
    components: list[int] | None = None  # indices de contornos a usar (None -> todos)
    min_floor_mm: float = 1.0            # grosor minimo de suelo sobre el pie


@dataclass
class BinResult:
    manifold: Manifold
    grid: tuple
    size_mm: tuple
    total_height_mm: float
    pockets: list
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------- primitivas
def rounded_rect(w, d, r, n=14):
    r = max(min(r, w / 2 - 1e-3, d / 2 - 1e-3), 0.01)
    pts = []
    for cx, cy, a0 in [(w / 2 - r, d / 2 - r, 0), (-w / 2 + r, d / 2 - r, 90),
                       (-w / 2 + r, -d / 2 + r, 180), (w / 2 - r, -d / 2 + r, 270)]:
        for k in range(n + 1):
            a = math.radians(a0 + 90 * k / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return np.round(np.array(pts), 4)   # cuantizar: puntos 'iguales' deben serlo exactamente


def loft_rrect(w, d, r, z0, z1, off0, off1):
    """Tronco convexo entre dos rectangulos redondeados con desplazamientos off0 (en z0) y off1 (en z1)."""
    a = rounded_rect(w + 2 * off0, d + 2 * off0, r + off0)
    b = rounded_rect(w + 2 * off1, d + 2 * off1, r + off1)
    pts = np.vstack([np.c_[a, np.full(len(a), round(z0, 4))], np.c_[b, np.full(len(b), round(z1, 4))]]).astype(np.float64)
    return Manifold.hull_points(pts)


def extrude_poly(geom, z0, z1):
    """Extrusion de un Polygon/MultiPolygon de shapely entre z0 y z1."""
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    parts = []
    for p in polys:
        if p.is_empty or p.area < 1e-6:
            continue
        rings = [np.round(np.asarray(p.exterior.coords)[:-1], 4)] + [np.round(np.asarray(i.coords)[:-1], 4) for i in p.interiors]
        cs = CrossSection(rings, FillRule.EvenOdd)
        parts.append(Manifold.extrude(cs, round(z1 - z0, 4)).translate((0, 0, round(z0, 4))))
    return Manifold.batch_boolean(parts, 0) if parts else None  # 0 = Add


def cylinder(x, y, z0, z1, diameter, n=48):
    return Manifold.cylinder(z1 - z0, diameter / 2, diameter / 2, n).translate((x, y, z0))


# ---------------------------------------------------------------- contornos
def contours_to_polys(outer, holes, keep_holes=False, components=None):
    polys = []
    sel = range(len(outer)) if components is None else components
    for i in sel:
        p = Polygon(outer[i]).buffer(0)
        if keep_holes:
            for h in holes:
                hp = Polygon(h).buffer(0)
                if p.contains(hp):
                    p = p.difference(hp)
        polys.append(p)
    return polys


def place_polys(polys, offset=(0.0, 0.0)):
    """Voltea Y (papel -> bin) y centra el conjunto en el origen."""
    flipped = [Polygon([(x, -y) for x, y in p.exterior.coords],
                       [[(x, -y) for x, y in i.coords] for i in p.interiors]) for p in polys]
    minx, miny, maxx, maxy = unary_union(flipped).bounds
    cx, cy = (minx + maxx) / 2 + offset[0], (miny + maxy) / 2 + offset[1]
    from shapely.affinity import translate
    return [translate(p, -cx, -cy) for p in flipped]


def finger_notch_polys(polys, mode, diameter):
    """Circulos en los extremos del eje largo de cada objeto (para sacarlo con el dedo)."""
    if mode == "none":
        return []
    out = []
    for p in polys:
        rect = p.minimum_rotated_rectangle
        c = np.asarray(rect.exterior.coords)[:4]
        e0, e1 = c[1] - c[0], c[2] - c[1]
        long_v = e0 if np.linalg.norm(e0) >= np.linalg.norm(e1) else e1
        L = np.linalg.norm(long_v)
        u = long_v / L
        ctr = np.array(rect.centroid.coords[0])
        ends = [ctr + u * (L / 2 - diameter * 0.15)]
        if mode == "both_ends":
            ends.append(ctr - u * (L / 2 - diameter * 0.15))
        out += [Point(*e).buffer(diameter / 2, 32) for e in ends]
    return out


# ---------------------------------------------------------------- generador
def auto_grid(polys, cfg: BinConfig):
    margin = cfg.clearance_mm + (2.6 if cfg.stacking_lip else 1.2)
    minx, miny, maxx, maxy = unary_union(polys).bounds
    w = math.ceil((maxx - minx + 2 * margin) / cfg.grid_unit_mm)
    d = math.ceil((maxy - miny + 2 * margin) / cfg.grid_unit_mm)
    return max(w, 1), max(d, 1)


def build_bin(outer, holes, cfg: BinConfig = BinConfig()) -> BinResult:
    warns = []
    raw = contours_to_polys(outer, holes, cfg.keep_holes, cfg.components)
    placed = place_polys(raw)
    wall = 2.6 if cfg.stacking_lip else 1.2
    pockets = [p.buffer(cfg.clearance_mm, 12) for p in placed]
    pockets += finger_notch_polys(placed, cfg.finger_notches, cfg.finger_diameter_mm)
    pocket_union = unary_union(pockets).simplify(0.03)
    u = cfg.grid_unit_mm

    def inner_zone(n_w, n_d):
        return Polygon(rounded_rect(n_w * u - BIN_GAP - 2 * wall, n_d * u - BIN_GAP - 2 * wall,
                                    max(CORNER_R - wall, 0.5))).buffer(0)

    nw, nd = (cfg.grid_width, cfg.grid_depth)
    if nw is None or nd is None:
        aw, ad = auto_grid(placed, cfg)
        # la estimacion por bbox ignora esquinas redondeadas y muescas: subir de tamano si no cabe
        cands = sorted(((aw + a, ad + b) for a in (0, 1) for b in (0, 1)), key=lambda t: t[0] * t[1])
        for cw, cd in cands:
            if (nw or cw, nd or cd) and inner_zone(nw or cw, nd or cd).contains(pocket_union):
                aw, ad = cw, cd
                break
        else:
            aw, ad = cands[-1]
        nw, nd = nw or aw, nd or ad
    W, D = nw * u - BIN_GAP, nd * u - BIN_GAP
    H = cfg.height_units * HEIGHT_UNIT

    # --- pies (uno por celda)
    feet = []
    for i in range(nw):
        for j in range(nd):
            cx, cy = (i - (nw - 1) / 2) * u, (j - (nd - 1) / 2) * u
            z, off = 0.0, -round(sum(dx for _, dx in FOOT_PROFILE), 4)
            for h, dx in FOOT_PROFILE:
                feet.append(loft_rrect(u - BIN_GAP, u - BIN_GAP, CORNER_R, z, round(z + h, 4), off,
                                       round(off + dx, 4)).translate((round(cx, 4), round(cy, 4), 0)))
                z = round(z + h, 4)
                off = round(off + dx, 4)
    base = Manifold.batch_boolean(feet, 0)

    # --- cuerpo
    body = Manifold.extrude(CrossSection([rounded_rect(W - 2 * SEAM_EPS, D - 2 * SEAM_EPS, CORNER_R - SEAM_EPS)], FillRule.EvenOdd),
                            round(H - FOOT_H, 4)) \
        .translate((0, 0, FOOT_H))
    solid = base + body

    # --- imanes desde abajo
    if cfg.magnet_holes:
        mags = [cylinder((i - (nw - 1) / 2) * u + sx * (u / 2 - 8.0), (j - (nd - 1) / 2) * u + sy * (u / 2 - 8.0),
                         -0.01, cfg.magnet_depth_mm, cfg.magnet_diameter_mm)
                for i in range(nw) for j in range(nd) for sx in (-1, 1) for sy in (-1, 1)]
        solid = solid - Manifold.batch_boolean(mags, 0)

    # --- cutouts: contorno + clearance (+ muescas para el dedo)
    z_bot = H - cfg.cutout_depth_mm
    if z_bot < FOOT_H + cfg.min_floor_mm:
        z_bot = FOOT_H + cfg.min_floor_mm
        warns.append(f"cutout_depth recortado a {H - z_bot:.1f} mm para dejar {cfg.min_floor_mm} mm de suelo")
    if not inner_zone(nw, nd).contains(pocket_union):
        out = pocket_union.difference(inner_zone(nw, nd)).area
        warns.append(f"el cutout sobresale de la zona interior ({out:.0f} mm2): aumenta la rejilla")
    pk = extrude_poly(pocket_union, z_bot, H + 1.0)
    solid = solid - pk

    # --- stacking lip (anillo sobre el cuerpo)
    total_h = H
    if cfg.stacking_lip:
        ring_outer = Manifold.extrude(CrossSection([rounded_rect(W, D, CORNER_R)], FillRule.EvenOdd), LIP_H) \
            .translate((0, 0, H))
        cav = []
        z = 0.0
        for h, off0, off1 in [(1.9, -2.6, -0.7), (1.8, -0.7, -0.7), (0.7, -0.7, -LIP_TOP_FLAT)]:
            cav.append(loft_rrect(W, D, CORNER_R, H + z, H + round(z + h, 4), off0, off1))
            z = round(z + h, 4)
        cav = Manifold.batch_boolean(cav, 0)
        # margen para evitar caras coplanares al restar
        cav = cav.translate((0, 0, 0))
        solid = solid + (ring_outer - cav)
        total_h = H + LIP_H

    return BinResult(solid, (nw, nd), (W, D), total_h, pockets, warns)


def to_trimesh(m: Manifold):
    import trimesh
    mesh = m.to_mesh()
    return trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3],
                           faces=np.asarray(mesh.tri_verts), process=False)  # manifold ya garantiza malla cerrada


def export_stl(m: Manifold, path: str) -> dict:
    """Exporta STL binario y lo vuelve a leer como lo haria un slicer (vertices fusionados) para validarlo."""
    import trimesh
    to_trimesh(m).export(path)
    chk = trimesh.load(path, file_type="stl")
    return {"watertight": bool(chk.is_watertight), "degenerate_faces": int((chk.area_faces < 1e-9).sum()),
            "volume_cm3": round(float(chk.volume) / 1000, 2) if chk.is_watertight else None}


def pockets_to_svg(pockets, path, margin=5.0):
    g = unary_union(pockets)
    minx, miny, maxx, maxy = g.bounds
    w, h = maxx - minx + 2 * margin, maxy - miny + 2 * margin
    polys = list(g.geoms) if isinstance(g, MultiPolygon) else [g]
    d = ""
    for p in polys:
        for ring in [p.exterior, *p.interiors]:
            pts = [(x - minx + margin, -(y - maxy) + margin) for x, y in ring.coords]
            d += "M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in pts) + " Z "
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" '
           f'viewBox="0 0 {w:.3f} {h:.3f}"><path d="{d}" fill="none" stroke="#000" stroke-width="0.2" '
           f'fill-rule="evenodd"/></svg>')
    open(path, "w").write(svg)
