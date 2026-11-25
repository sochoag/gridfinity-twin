"""Diseno multi-forma al estilo ShapeAny: cada forma tiene posicion, rotacion, espejo, profundidad,
muesca para el dedo y (opcionalmente) un contorno editado; el bin tiene chaflan de bolsillo e inserto de contraste.

Marco de coordenadas del diseno (el mismo que dibuja el editor 2D): origen en el centro del bin, X a la derecha,
Y HACIA ABAJO, angulos en grados en sentido horario en pantalla. Cada forma vive en coordenadas locales centradas
en el centro del bbox de su contorno original; su transformacion es  local -> (espejo) -> rotar(rot) -> trasladar(x, y).
Solo al construir la geometria 3D se voltea Y (Gridfinity usa Y hacia arriba).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from manifold3d import Manifold
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from manifold3d import CrossSection, FillRule

from .gridfinity import (BIN_GAP, CORNER_R, FOOT_H, FOOT_PROFILE, HEIGHT_UNIT, LIP_H, cylinder, extrude_poly, lip_ring,
                         loft_rrect, make_shell, rounded_rect)

DEFAULT_SETTINGS = {
    "auto_size": True, "grid_width": 4, "grid_depth": 4, "height_units": 3, "grid_unit_mm": 42.0,
    "pocket_depth_mm": 12.0, "pocket_chamfer": True, "chamfer_mm": 1.0, "clearance_mm": 1.0,
    "contrast_insert": True, "insert_thickness_mm": 0.4, "fit_clearance_mm": 0.2,
    "wall_thickness_mm": 1.6, "stacking_lip": True,
    "magnet_holes": False, "magnet_diameter_mm": 6.5, "magnet_depth_mm": 2.4,
    "hollow": False, "hollow_rib_mm": 2.4, "hollow_floor_mm": 1.2,
    "lid_thickness_mm": 1.2, "lid_ledge_mm": 0.8, "lid_clearance_mm": 0.2,
}
MIN_FLOOR = 1.0
FINGER_D = 16.0
CHAMFER_STEPS = 5


@dataclass
class DesignResult:
    bin: Manifold
    insert: Manifold | None
    grid: tuple
    size_mm: tuple
    total_height_mm: float
    pockets: list
    warnings: list = field(default_factory=list)
    problems: dict = field(default_factory=dict)   # id de forma -> ["overlap" | "outside"]
    lid: Manifold | None = None                    # tapa (modo hueco), plana sobre z=0
    stats: dict = field(default_factory=dict)      # volumenes en cm3 (macizo / hueco / tapa)


# ---------------------------------------------------------------- geometria de una forma
def comp_of(comps: list[dict], sh: dict):
    """Objeto detectado al que apunta la forma, o None si es una forma importada (lleva su propio contorno)."""
    i = sh.get("comp")
    return comps[i] if i is not None and 0 <= i < len(comps) else None


def local_polygon(comp: dict, sh: dict) -> Polygon:
    """Poligono local de la forma (centrado, Y hacia abajo, con espejo aplicado, sin rotar)."""
    if sh.get("outer") or comp is None:
        p = Polygon(sh["outer"]).buffer(0)
        for h in sh.get("holes") or []:
            p = p.difference(Polygon(h).buffer(0))
        return p
    p = Polygon(comp["outer"]).buffer(0)
    minx, miny, maxx, maxy = p.bounds
    if sh.get("keep_holes"):
        for h in comp.get("holes", []):
            hp = Polygon(h).buffer(0)
            if p.contains(hp):
                p = p.difference(hp)
    p = affinity.translate(p, -(minx + maxx) / 2, -(miny + maxy) / 2)
    if sh.get("mirror"):
        p = affinity.scale(p, xfact=-1, yfact=1, origin=(0, 0))
    return p


def to_bin_frame(g, sh: dict):
    """local -> rotar -> trasladar (marco del bin, Y hacia abajo)."""
    g = affinity.rotate(g, sh.get("rot", 0.0), origin=(0, 0))
    return affinity.translate(g, sh.get("x", 0.0), sh.get("y", 0.0))


def flip_y(g):
    return affinity.scale(g, xfact=1, yfact=-1, origin=(0, 0))


def default_notch(poly: Polygon, d: float = FINGER_D) -> tuple[float, float]:
    """Centro (local) de la muesca por defecto: extremo del eje largo de la forma."""
    rect = poly.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)[:4]
    e0, e1 = c[1] - c[0], c[2] - c[1]
    v = e0 if np.linalg.norm(e0) >= np.linalg.norm(e1) else e1
    L = float(np.linalg.norm(v))
    u = v / L
    ctr = np.array(rect.centroid.coords[0])
    if u[0] < 0 or (abs(u[0]) < 1e-9 and u[1] < 0):
        u = -u
    p = ctr + u * (L / 2 - d * 0.15)
    return round(float(p[0]), 2), round(float(p[1]), 2)


# ---------------------------------------------------------------- disposicion automatica
def arrange(polys: list[Polygon], pad: float, unit: float, wall: float) -> list[tuple[float, float, float]]:
    """Empaqueta las formas en filas y devuelve (x, y, rot) por forma, centrado en el bin.

    Prueba el eje largo horizontal y vertical y varios anchos de fila, y se queda con la rejilla mas pequena."""
    def long_axis(p):
        c = np.asarray(p.minimum_rotated_rectangle.exterior.coords)[:4]
        e0, e1 = c[1] - c[0], c[2] - c[1]
        v = e0 if np.linalg.norm(e0) >= np.linalg.norm(e1) else e1
        return math.degrees(math.atan2(v[1], v[0]))

    best = None
    for extra in (0.0, 90.0):
        rots = [round((-long_axis(p) + extra) % 360, 2) for p in polys]
        boxes = []
        for p, r in zip(polys, rots):
            b = affinity.rotate(p, r, origin=(0, 0)).bounds
            boxes.append((b[2] - b[0] + 2 * pad, b[3] - b[1] + 2 * pad, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
        order = sorted(range(len(polys)), key=lambda i: -boxes[i][1])
        wmax, wsum = max(b[0] for b in boxes), sum(b[0] for b in boxes)
        for k in range(10):
            target = wmax + (wsum - wmax) * k / 9
            x = y = rowh = W = 0.0
            pos = {}
            for i in order:
                w, h = boxes[i][0], boxes[i][1]
                if x > 0 and x + w > target + 1e-6:
                    y += rowh
                    x = rowh = 0.0
                pos[i] = (x + w / 2, y + h / 2)
                x += w
                rowh = max(rowh, h)
                W = max(W, x)
            Hh = y + rowh
            cells = math.ceil((W + 2 * wall) / unit) * math.ceil((Hh + 2 * wall) / unit)
            score = (cells, abs(W - Hh), W * Hh)
            if best is None or score < best[0]:
                best = (score, rots, boxes, pos, W, Hh)
    _, rots, boxes, pos, W, Hh = best
    out = []
    for i, p in enumerate(polys):
        cx, cy = pos[i]
        # (cx, cy) es el centro de la caja rotada; el origen local queda desplazado (bx, by) de ese centro
        out.append((round(cx - W / 2 - boxes[i][2], 2), round(cy - Hh / 2 - boxes[i][3], 2), rots[i]))
    return out


# ---------------------------------------------------------------- construccion 3D
def _clean(g, eps=0.01):
    """Cierre morfologico minimo + simplificacion: quita estrechamientos de 0 mm que dan mallas no cerradas."""
    return g.buffer(eps, 4).buffer(-eps, 4).simplify(0.03)


def _poly_union(items):
    g = unary_union(items)
    return g.buffer(0) if not g.is_valid else g


def _hollow_parts(nw, nd, u, H, zone, pk_all, S, c_chamfer):
    """Cavidades del modo hueco (se restan junto con los bolsillos).

    Abierto por ARRIBA: sin puentes ni soportes al imprimir. Alrededor de cada bolsillo queda una costilla de
    `hollow_rib_mm`; el suelo y los pies son copas huecas que se abren al cuerpo; la tapa se asienta en el
    rebaje de la parte alta (las costillas terminan `lid_t` por debajo del borde del bin)."""
    rib, bf = S["hollow_rib_mm"], S["hollow_floor_mm"]
    wall = S["wall_thickness_mm"]
    lid_t = S["lid_thickness_mm"]
    rim = max(0.8, rib - S["lid_ledge_mm"])                       # reborde alto junto al bolsillo; el resto de la costilla es la grada de la tapa
    # el rebaje de la tapa termina justo donde empieza la base del labio (sin voladizo bajo el anillo); la cavidad
    # queda `lid_ledge_mm` mas adentro: esa es la grada de la pared exterior
    r_in = 2.65 if S["stacking_lip"] else wall   # 0.05 mm dentro del labio: evita aristas coincidentes con el anillo
    l_out = max(0.0, S["lid_ledge_mm"])
    def _zone(inset):
        return Polygon(rounded_rect(nw * u - BIN_GAP - 2 * inset, nd * u - BIN_GAP - 2 * inset, max(CORNER_R - inset, 0.5))).buffer(0)
    zone_ext = _zone(r_in)
    zone = _zone(r_in + l_out)
    z_body = FOOT_H + bf                                          # suelo de la cavidad del cuerpo
    A = zone.difference(pk_all.buffer(rib, 12))
    A = A.buffer(-0.5, 4).buffer(0.5, 4).simplify(0.03)           # quita rendijas < 1 mm (mallas degeneradas)
    A = unary_union([g for g in (A.geoms if hasattr(A, "geoms") else [A]) if g.area > 6.0])
    if A.is_empty:
        return [], None, lid_t, l_out, rim
    parts = []
    # cavidad del cuerpo
    parts.append(extrude_poly(A, z_body, H - lid_t + 0.01))
    # copas de los pies: contorno del pie recortado `rib`, abiertas hacia el cuerpo
    cups = []
    fw = rib
    for i in range(nw):
        for j in range(nd):
            cx, cy = round((i - (nw - 1) / 2) * u, 4), round((j - (nd - 1) / 2) * u, 4)
            z, off = 0.0, -round(sum(dx for _, dx in FOOT_PROFILE), 4)
            for h, dx in FOOT_PROFILE:
                cups.append(loft_rrect(u - BIN_GAP, u - BIN_GAP, CORNER_R, z, round(z + h, 4), round(off - fw, 4),
                                       round(off + dx - fw, 4)).translate((cx, cy, 0)))
                z, off = round(z + h, 4), round(off + dx, 4)
            # prisma hecho con el mismo camino numerico (hull) que el pie: las aristas coinciden bit a bit
            top = loft_rrect(u - BIN_GAP, u - BIN_GAP, CORNER_R, FOOT_H, round(FOOT_H + bf + 0.2, 4),
                             round(-fw, 4), round(-fw, 4)).translate((cx, cy, 0))
            cups.append(top)
    cup = Manifold.batch_boolean(cups, 0)
    box = Manifold.cube((nw * u + 10, nd * u + 10, FOOT_H + 0.2), center=True).translate((0, 0, bf + (FOOT_H + 0.2) / 2))
    cup = cup ^ box                                                # fondo de las copas: `hollow_floor_mm`
    cup = cup ^ extrude_poly(A, -0.5, z_body + 0.3)               # las costillas atraviesan los pies
    if S["magnet_holes"]:
        bosses = [cylinder((i - (nw - 1) / 2) * u + sx * (u / 2 - 8.0), (j - (nd - 1) / 2) * u + sy * (u / 2 - 8.0),
                           -1.0, z_body + 1.0, S["magnet_diameter_mm"] + 2 * rib)
                  for i in range(nw) for j in range(nd) for sx in (-1, 1) for sy in (-1, 1)]
        cup = cup - Manifold.batch_boolean(bosses, 0)
    parts.append(cup)
    # rebaje de la tapa (toda la zona util + escalon sobre la pared exterior)
    seat = zone_ext.difference(pk_all.buffer(rim, 12)).buffer(-0.05, 4).buffer(0.05, 4).simplify(0.03)
    parts.append(extrude_poly(seat, H - lid_t, H + 1.0))
    return [p for p in parts if p is not None], zone_ext, lid_t, l_out, rim


def build_design(comps: list[dict], shapes: list[dict], S: dict) -> DesignResult:
    S = {**DEFAULT_SETTINGS, **S}
    if not shapes:
        raise ValueError("el bin no tiene formas")
    warns, problems = [], {}
    u, wall = S["grid_unit_mm"], S["wall_thickness_mm"]

    items = []
    for sh in shapes:
        comp = comp_of(comps, sh)
        base = local_polygon(comp, sh)
        if sh.get("offset_mm"):
            base = base.buffer(sh["offset_mm"], 12)
        if base.is_empty:
            raise ValueError(f"la forma '{sh.get('name', sh['id'])}' quedo vacia (offset demasiado negativo)")
        pocket = flip_y(to_bin_frame(base, sh)).buffer(S["clearance_mm"], 12)
        notch = None
        n = sh.get("notch") or {}
        if n.get("on"):
            c = to_bin_frame(Point(n.get("u", 0.0), n.get("v", 0.0)), sh)
            notch = flip_y(c.buffer(float(n.get("d", FINGER_D)) / 2, 32))
        full = unary_union([pocket] + ([notch] if notch is not None else []))
        items.append({"id": sh["id"], "pocket": pocket, "full": full,
                      "depth": float(sh.get("depth_mm") or S["pocket_depth_mm"])})

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i]["full"].intersection(items[j]["full"]).area > 0.5:
                problems.setdefault(items[i]["id"], set()).add("overlap")
                problems.setdefault(items[j]["id"], set()).add("overlap")
    allu = unary_union([it["full"] for it in items])

    def inner_zone(nw_, nd_):
        return Polygon(rounded_rect(nw_ * u - BIN_GAP - 2 * wall, nd_ * u - BIN_GAP - 2 * wall,
                                    max(CORNER_R - wall, 0.5))).buffer(1e-6)

    if S["auto_size"]:
        minx, miny, maxx, maxy = allu.bounds
        nw = max(1, math.ceil((2 * max(abs(minx), abs(maxx)) + 2 * wall + BIN_GAP) / u))
        nd = max(1, math.ceil((2 * max(abs(miny), abs(maxy)) + 2 * wall + BIN_GAP) / u))
        for _ in range(4):
            if inner_zone(nw, nd).contains(allu):
                break
            c = sorted([(nw + 1, nd), (nw, nd + 1), (nw + 1, nd + 1)], key=lambda t: t[0] * t[1])
            nw, nd = next(((a, b) for a, b in c if inner_zone(a, b).contains(allu)), c[-1])
    else:
        nw, nd = int(S["grid_width"]), int(S["grid_depth"])
    W, D = nw * u - BIN_GAP, nd * u - BIN_GAP
    H = S["height_units"] * HEIGHT_UNIT
    zone = inner_zone(nw, nd)
    for it in items:
        if not zone.contains(it["full"]):
            problems.setdefault(it["id"], set()).add("outside")
    if any("outside" in v for v in problems.values()):
        warns.append("alguna forma sobresale de la zona util del bin: muevela o aumenta la rejilla")
    if any("overlap" in v for v in problems.values()):
        warns.append("hay formas que se solapan")

    solid = make_shell(nw, nd, u, H, S["magnet_holes"], S["magnet_diameter_mm"], S["magnet_depth_mm"])

    # --- bolsillos agrupados por profundidad
    ins = S["insert_thickness_mm"] if S["contrast_insert"] else 0.0
    floor_min = FOOT_H + MIN_FLOOR + ins
    groups: dict[float, list] = {}
    for it in items:
        z_bot = H - it["depth"]
        if z_bot < floor_min:
            z_bot = floor_min
            warns.append(f"profundidad recortada a {H - z_bot:.1f} mm para dejar suelo ({it['id']})")
        groups.setdefault(round(z_bot, 3), []).append(it)

    pockets_all, inserts, cutters, chamfer_src = [], [], [], []
    c = S["chamfer_mm"] if S["pocket_chamfer"] else 0.0
    hollow = bool(S["hollow"])
    if hollow:   # el rebaje de la tapa baja `lid_t` desde el borde: el bolsillo debe ser algo mas profundo
        lid_need = S["lid_thickness_mm"] + 1.0
        if any(H - z > 0 and (H - z) < lid_need for z in groups):
            warns.append(f"en modo hueco los bolsillos deben tener al menos {lid_need:.1f} mm de profundidad: se ajustaron")
            fixed: dict[float, list] = {}
            for z, its in groups.items():
                fixed.setdefault(round(min(z, H - lid_need), 3), []).extend(its)
            groups = fixed
    for z_bot, its in groups.items():
        full = _poly_union([i["full"] for i in its]).simplify(0.03)
        cutters.append(extrude_poly(full, z_bot, H + 1.0))
        if c > 0 and H - z_bot > c + 0.4:
            chamfer_src.append(full)
        if ins > 0:
            shape_only = _poly_union([i["pocket"] for i in its]).simplify(0.03)
            # 5 um mas chico que el bolsillo: paredes exactamente coplanares generan triangulos de area 0 (colineales)
            # que el STL pierde al releerlo y dejan un agujero triangular en la malla
            recess = shape_only.buffer(-0.005, 8)
            cutters.append(extrude_poly(recess, z_bot - ins, z_bot + 0.01))
            ip = shape_only.buffer(-S["fit_clearance_mm"], 12)
            if not ip.is_empty:
                inserts.append(extrude_poly(ip, z_bot - ins, z_bot))
        pockets_all.append(full)

    if chamfer_src:   # un unico juego de rebanadas para todos los grupos: bordes consistentes aunque las formas se solapen
        cu = _poly_union(chamfer_src).simplify(0.03)
        for k in range(CHAMFER_STEPS):
            z0 = H - c + c * k / CHAMFER_STEPS
            cutters.append(extrude_poly(_clean(cu.buffer(c * (k + 0.5) / CHAMFER_STEPS, 12)), z0, H + 1.0))
    cutters = [c_ for c_ in cutters if c_ is not None]
    lid = None
    stats = {}
    if hollow:
        pk_all = _poly_union([it["full"] for it in items])
        hparts, zone_ext, lid_t, l_out, rim = _hollow_parts(nw, nd, u, H, zone, pk_all, S, c)
        if hparts:
            stats["lid_z"] = round(H - lid_t, 3)
            stats["solid_cm3"] = round((solid - Manifold.batch_boolean(cutters, 0)).volume() / 1000, 2)
            lid_cl = S["lid_clearance_mm"]
            plate = zone_ext.buffer(-lid_cl, 12).difference(pk_all.buffer(rim + lid_cl, 12)).buffer(-0.05, 4).buffer(0.05, 4).simplify(0.03)
            lid = extrude_poly(plate, H - lid_t, H)
            lid = lid.translate((0, 0, -(H - lid_t)))
            cutters += hparts
        else:
            warns.append("no queda espacio para vaciar el bin (las costillas ocupan todo): reduce el grosor de costilla")
    solid = solid - Manifold.batch_boolean(cutters, 0)   # un solo corte: evita intermedios degenerados
    if hollow and lid is not None:
        stats["bin_cm3"] = round(solid.volume() / 1000, 2)
        stats["lid_cm3"] = round(lid.volume() / 1000, 2)

    total_h = H
    if S["stacking_lip"]:
        solid = solid + lip_ring(W, D, H)
        total_h = H + LIP_H
    insert = Manifold.batch_boolean([m for m in inserts if m is not None], 0) if inserts else None

    return DesignResult(solid, insert, (nw, nd), (W, D), total_h, pockets_all, warns,
                        {k: sorted(v) for k, v in problems.items()}, lid, stats)
