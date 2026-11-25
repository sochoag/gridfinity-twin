"""Diseno multi-forma al estilo ShapeAny: cada forma tiene posicion, rotacion, espejo, profundidad,
muesca para el dedo y (opcionalmente) un contorno editado; el bin tiene chaflan de bolsillo e inserto de contraste.

Marco de coordenadas del diseno (el mismo que dibuja el editor 2D): origen en el centro del bin, X a la derecha,
Y HACIA ABAJO, angulos en grados en sentido horario en pantalla. Cada forma vive en coordenadas locales centradas
en el centro del bbox de su contorno original; su transformacion es  local -> (espejo) -> rotar(rot) -> trasladar(x, y).
Solo al construir la geometria 3D se voltea Y (Gridfinity usa Y hacia arriba).
"""
from __future__ import annotations

import math
import shapely
from dataclasses import dataclass, field

import numpy as np
from manifold3d import Manifold
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box as sbox
from shapely.ops import unary_union

from manifold3d import CrossSection, FillRule

from .gridfinity import (BIN_GAP, CORNER_R, FOOT_H, FOOT_PROFILE, HEIGHT_UNIT, LIP_H, cylinder, extrude_poly, lip_ring,
                         loft_rrect, make_shell, rounded_rect, footprint_poly, lip_ring_poly)

DEFAULT_SETTINGS = {
    "auto_size": True, "grid_width": 4, "grid_depth": 4, "height_units": 3, "grid_unit_mm": 42.0,
    "pocket_depth_mm": 12.0, "pocket_chamfer": True, "chamfer_mm": 1.0, "clearance_mm": 1.0,
    "contrast_insert": True, "insert_thickness_mm": 0.4, "fit_clearance_mm": 0.2,
    "wall_thickness_mm": 1.6, "stacking_lip": True,
    "magnet_holes": False, "magnet_diameter_mm": 6.5, "magnet_depth_mm": 2.4,
    "partial": False, "cells_off": [],
    "bed_x_mm": 256.0, "bed_y_mm": 256.0, "nozzle_mm": 0.4,
    "hollow": False, "hollow_rib_mm": 2.4, "hollow_floor_mm": 1.2,
    "lid_thickness_mm": 1.2, "lid_ledge_mm": 0.8, "lid_clearance_mm": 0.2,
    "dividers_x": [], "dividers_y": [], "divider_thickness_mm": 1.2, "divider_clearance_mm": 0.2, "divider_slot_depth_mm": 0.8,
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
    checks: list = field(default_factory=list)     # chequeo de imprimibilidad: {level, code, msg, shapes}
    dividers: Manifold | None = None               # divisores sueltos (laminas planas, en fila)


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


def _hollow_parts(nw, nd, u, H, zone, pk_all, S, c_chamfer, cells=None, fp=None):
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
        if fp is not None:
            return shapely.set_precision(fp.buffer(-inset, 14).buffer(0).simplify(0.005), 1e-4)
        return Polygon(rounded_rect(nw * u - BIN_GAP - 2 * inset, nd * u - BIN_GAP - 2 * inset, max(CORNER_R - inset, 0.5))).buffer(0)
    zone_ext = _zone(r_in)
    zone = _zone(r_in + l_out + (0.03 if fp is not None else 0.0))   # parcial: evita aristas casi coincidentes con las copas
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
    inc = [(i, j) for i in range(nw) for j in range(nd) if cells is None or (i, j) in cells]
    for i, j in inc:
        if True:
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
                  for i, j in inc for sx in (-1, 1) for sy in (-1, 1)]
        cup = cup - Manifold.batch_boolean(bosses, 0)
    parts.append(cup)
    # rebaje de la tapa (toda la zona util + escalon sobre la pared exterior)
    seat = zone_ext.difference(pk_all.buffer(rim, 12)).buffer(-0.05, 4).buffer(0.05, 4).simplify(0.03)
    parts.append(extrude_poly(seat, H - lid_t, H + 1.0))
    return [p for p in parts if p is not None], zone_ext, lid_t, l_out, rim


def _printability(items, S, W, D, H, lid_t, hollow, stats, lid, warns_n):
    """Chequeos de imprimibilidad. level: 'warn' (probable fallo) | 'info'."""
    out = []
    nz = float(S["nozzle_mm"])
    min_wall = 2 * nz
    bx, by = float(S["bed_x_mm"]), float(S["bed_y_mm"])
    if not ((W <= bx and D <= by) or (W <= by and D <= bx)):
        out.append({"level": "warn", "code": "bed", "shapes": [],
                    "msg": f"el bin ({W:.0f} × {D:.0f} mm) no cabe en la cama ({bx:.0f} × {by:.0f} mm): reduce la rejilla o activa Partial Bin"})
    if S["wall_thickness_mm"] < min_wall:
        out.append({"level": "warn", "code": "outer_wall", "shapes": [],
                    "msg": f"la pared exterior ({S['wall_thickness_mm']:.1f} mm) es menor que 2 pasadas de boquilla ({min_wall:.1f} mm)"})
    thin = {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            d = items[i]["full"].distance(items[j]["full"])
            if 0 < d < min_wall:
                thin[(items[i]["id"], items[j]["id"])] = d
    for (a, b), d in sorted(thin.items()):
        out.append({"level": "warn", "code": "thin_wall", "shapes": [a, b],
                    "msg": f"solo {d:.2f} mm de material entre '{a}' y '{b}' (minimo {min_wall:.1f} mm): sepáralas o reduce la holgura"})
    for it in items:
        if it["pocket"].buffer(-nz / 2).is_empty:
            out.append({"level": "warn", "code": "narrow", "shapes": [it["id"]],
                        "msg": f"el bolsillo '{it['id']}' es mas angosto que la boquilla"})
    if hollow:
        rib = S["hollow_rib_mm"]
        if rib < min_wall + 0.4:
            out.append({"level": "warn", "code": "rib", "shapes": [],
                        "msg": f"costilla de {rib:.1f} mm: con boquilla de {nz:.1f} mm queda de solo {int(rib // nz)} pasadas"})
        if lid is not None and (W > bx and W > by or D > bx and D > by):
            out.append({"level": "warn", "code": "lid_bed", "shapes": [], "msg": "la tapa no cabe en la cama"})
    return out


def _divider_parts(S, zone, fp, floor_min, H, wall, hollow, warns):
    """Ranuras (se restan del bin, abiertas por arriba) y laminas sueltas que encajan en ellas.

    Las posiciones estan en el marco del editor (origen en el centro, Y hacia abajo). Cada ranura penetra
    `divider_slot_depth_mm` en la pared (siempre dejando >= 0.8 mm de pared) y baja hasta justo encima del suelo."""
    dx = [float(v) for v in (S.get("dividers_x") or [])]
    dy = [float(v) for v in (S.get("dividers_y") or [])]
    if not dx and not dy:
        return [], []
    if hollow:
        warns.append("los divisores sueltos no estan disponibles en modo hueco: se ignoraron")
        return [], []
    t, cl = float(S["divider_thickness_mm"]), float(S["divider_clearance_mm"])
    sd = max(0.0, min(float(S["divider_slot_depth_mm"]), wall - 0.8))
    sw = t + 2 * cl
    reach = zone.buffer(sd, 12)
    if fp is not None:
        reach = reach.intersection(fp.buffer(-0.8, 12))
    slot_z = floor_min + 0.07
    big = 5000.0
    cuts, blades = [], []
    bh = H - 1.0 - slot_z - 0.1                      # la lamina queda 1 mm por debajo del borde
    if bh < 3.0:
        warns.append("el bin es demasiado bajo para divisores sueltos")
        return [], []
    for axis, vals in (("x", dx), ("y", dy)):
        for pos in vals:
            wpos = pos if axis == "x" else -pos      # editor Y hacia abajo -> mundo Y hacia arriba
            r = (sbox(wpos - sw / 2, -big, wpos + sw / 2, big) if axis == "x"
                 else sbox(-big, wpos - sw / 2, big, wpos + sw / 2)).intersection(reach)
            if r.is_empty or r.area < 1.0:
                warns.append(f"un divisor en {axis} = {pos:.1f} mm cae fuera del bin: se ignoro")
                continue
            r = unary_union([g for g in (r.geoms if hasattr(r, "geoms") else [r]) if g.area > 1.0])
            b = r.bounds
            L = (b[3] - b[1]) if axis == "x" else (b[2] - b[0])
            cuts.append(extrude_poly(r.simplify(0.01), slot_z, H + LIP_H + 2.0))
            blades.append(Manifold.cube((max(L - 0.4, 2.0), bh, t)))   # 0.4 mm de holgura en los extremos
    return cuts, blades


def _blade_row(blades):
    """Laminas tumbadas (grosor en Z), en fila con 4 mm de separacion, sobre z = 0."""
    if not blades:
        return None
    out, y = [], 0.0
    for bl in blades:
        bb = bl.bounding_box()
        w, h = bb[3] - bb[0], bb[4] - bb[1]
        out.append(bl.translate((-bb[0] - w / 2, y - bb[1], -bb[2])))
        y += h + 4.0
    return Manifold.batch_boolean(out, 0) if len(out) > 1 else out[0]


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

    partial = bool(S["partial"])
    cells_off = set()
    for t in S.get("cells_off") or []:
        try:
            a, b = str(t).split(",")
            cells_off.add((int(a), int(b)))
        except ValueError:
            pass
    if partial:
        S["auto_size"] = False                      # las celdas se numeran desde la esquina: la rejilla debe ser fija

    def _cells(nw_, nd_):
        # cells_off usa la fila del editor 2D (Y hacia abajo); el bin se construye con Y invertida
        return {(i, j) for i in range(nw_) for j in range(nd_) if (i, nd_ - 1 - j) not in cells_off} if partial else None

    def inner_zone(nw_, nd_):
        if partial:
            cs = _cells(nw_, nd_)
            if not cs:
                raise ValueError("el bin parcial necesita al menos una celda activa")
            return footprint_poly(cs, nw_, nd_, u).buffer(-wall, 12).buffer(1e-6)
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

    cells = _cells(nw, nd)
    if partial and not cells:
        raise ValueError("el bin parcial necesita al menos una celda activa")
    is_partial = cells is not None and len(cells) < nw * nd
    fp = footprint_poly(cells, nw, nd, u) if is_partial else None
    solid = make_shell(nw, nd, u, H, S["magnet_holes"], S["magnet_diameter_mm"], S["magnet_depth_mm"], cells if is_partial else None)

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
        hparts, zone_ext, lid_t, l_out, rim = _hollow_parts(nw, nd, u, H, zone, pk_all, S, c, cells if is_partial else None, fp)
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
    slot_cut, blades = _divider_parts(S, zone, fp, floor_min, H, wall, hollow, warns)
    if hollow and lid is not None:
        stats["bin_cm3"] = round(solid.volume() / 1000, 2)
        stats["lid_cm3"] = round(lid.volume() / 1000, 2)

    total_h = H
    if S["stacking_lip"]:
        solid = solid + (lip_ring_poly(fp, H) if is_partial else lip_ring(W, D, H))
        total_h = H + LIP_H
    if slot_cut:   # las ranuras atraviesan tambien el labio de apilado: la lamina se inserta desde arriba
        solid = solid - Manifold.batch_boolean(slot_cut, 0)
    if solid.is_empty():
        raise ValueError("el bin quedo vacio: reduce el grosor de pared / costilla o aumenta la rejilla")
    if lid is not None and lid.is_empty():
        lid = None
        warns.append("la tapa quedo vacia (la zona es demasiado estrecha): ajusta asiento o holgura")
    insert = Manifold.batch_boolean([m for m in inserts if m is not None], 0) if inserts else None

    checks = _printability(items, S, W, D, H, S["lid_thickness_mm"], hollow, stats, lid, len(warns))
    if blades:
        nz = float(S["nozzle_mm"])
        if S["divider_thickness_mm"] < 2 * nz:
            checks.append({"level": "warn", "code": "divider_thin", "shapes": [],
                           "msg": f"el divisor ({S['divider_thickness_mm']:.1f} mm) es menor que 2 pasadas de boquilla ({2 * nz:.1f} mm)"})
        checks.append({"level": "info", "code": "dividers", "shapes": [],
                       "msg": f"{len(blades)} divisor(es) suelto(s) de {S['divider_thickness_mm']:.1f} mm: se imprimen tumbados (archivo aparte)"})
    return DesignResult(solid, insert, (nw, nd), (W, D), total_h, pockets_all, warns,
                        {k: sorted(v) for k, v in problems.items()}, lid, stats, checks, _blade_row(blades))
