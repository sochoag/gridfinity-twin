"""Lectura de contornos desde SVG y DXF -> poligonos en mm.

Cada resultado: {"outer": [[x, y], ...], "holes": [[[x, y], ...], ...]} con Y hacia abajo (como el editor).
"""
from __future__ import annotations

import io
import math

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

MIN_AREA_MM2 = 4.0
MAX_SHAPES = 40
PX_MM = 25.4 / 96.0


class VectorError(ValueError):
    pass


def _ring(poly_or_pts, simp=0.04):
    pts = list(poly_or_pts)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _shapes_from_rings(rings):
    """Anidamiento par/impar: profundidad par = contorno exterior, impar = hueco del exterior mas pequeno que lo contiene."""
    polys = []
    for r in rings:
        if len(r) < 3:
            continue
        p = Polygon(r)
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty or p.geom_type != "Polygon" or p.area < 1e-3:
            continue
        polys.append(p)
    polys.sort(key=lambda p: -p.area)
    depth = []
    parent = []
    for i, p in enumerate(polys):
        rp = p.representative_point()
        cont = [j for j in range(i) if polys[j].contains(rp)]
        depth.append(len(cont))
        parent.append(cont[-1] if cont else None)     # el mas pequeno (ultimo, por area descendente)
    out = {}
    for i, p in enumerate(polys):
        if depth[i] % 2 == 0:
            out[i] = {"poly": p, "holes": []}
    for i, p in enumerate(polys):
        if depth[i] % 2 == 1 and parent[i] in out:
            out[parent[i]]["holes"].append(p)
    return [(v["poly"], v["holes"]) for v in out.values()]


def _pack(items, scale=1.0, flip=False):
    res = []
    for poly, holes in items:
        if poly.area * scale * scale < MIN_AREA_MM2:
            continue
        def conv(p):
            p = p.simplify(0.03 / max(scale, 1e-9))
            pts = [(x * scale, (-y if flip else y) * scale) for x, y in list(p.exterior.coords)[:-1]]
            return [[round(x, 3), round(y, 3)] for x, y in pts]
        res.append({"outer": conv(poly), "holes": [conv(h) for h in holes if h.area * scale * scale >= 1.0]})
    res.sort(key=lambda s: -abs(Polygon(s["outer"]).area))
    return res[:MAX_SHAPES]


def parse_svg(raw: bytes):
    try:
        from svgelements import SVG, Shape, Path, Move
    except ImportError as e:  # pragma: no cover
        raise VectorError("falta la libreria svgelements") from e
    try:
        svg = SVG.parse(io.BytesIO(raw), reify=True, ppi=96.0)
    except Exception as e:  # noqa: BLE001
        raise VectorError(f"SVG no valido: {e}")
    rings = []
    for el in svg.elements():
        if not isinstance(el, Shape) or isinstance(el, SVG):
            continue
        try:
            path = Path(el) if not isinstance(el, Path) else el
            path = abs(path)
            for sub in path.as_subpaths():
                sp = Path(sub)
                if not sp.length(error=1e-3) > 1e-6:
                    continue
                n = max(24, int(sp.length(error=1e-2) / 0.7))      # ~0.7 px entre puntos (curvas incluidas)
                pts = [(float(q[0]), float(q[1])) for q in sp.npoint(np.linspace(0, 1, min(n, 4000)))]
                closed = sp.closed() or math.dist(pts[0], pts[-1]) < 0.5
                if closed:
                    rings.append(_ring(pts))
        except Exception:  # noqa: BLE001
            continue
    if not rings:
        raise VectorError("no encontre contornos cerrados en el SVG (convierte los trazos a rutas cerradas)")
    out = _pack(_shapes_from_rings(rings), PX_MM)
    if not out:
        raise VectorError("los contornos del SVG son demasiado pequenos o no encierran area")
    return out


_INS_UNITS = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 3: 1609344.0, 8: 0.0000254, 9: 0.0254, 10: 914.4}


def parse_dxf(raw: bytes):
    try:
        import ezdxf
        from ezdxf import path as ezpath
    except ImportError as e:  # pragma: no cover
        raise VectorError("falta la libreria ezdxf") from e
    try:
        text = raw.decode("utf-8", errors="ignore") if b"\x00" not in raw[:64] else raw.decode("latin-1")
        doc = ezdxf.read(io.StringIO(text))
    except Exception as e:  # noqa: BLE001
        raise VectorError(f"DXF no valido (guarda como ASCII DXF): {e}")
    scale = _INS_UNITS.get(int(doc.header.get("$INSUNITS", 4) or 4), 1.0) or 1.0
    lines, rings = [], []
    for e in doc.modelspace():
        t = e.dxftype()
        if t not in ("LINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "LWPOLYLINE", "POLYLINE"):
            continue
        try:
            pth = ezpath.make_path(e)
            pts = [(v.x, v.y) for v in pth.flattening(0.05, segments=8)]
        except Exception:  # noqa: BLE001
            continue
        if len(pts) < 2:
            continue
        if t in ("CIRCLE",) or (t in ("LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE") and math.dist(pts[0], pts[-1]) < 1e-6):
            rings.append(_ring(pts))
        else:
            lines.append(LineString(pts))
    if lines:                           # lineas/arcos sueltos: cerrar cadenas con polygonize
        import shapely
        merged = shapely.set_precision(unary_union(lines), 0.002)    # une extremos casi coincidentes
        faces = list(polygonize(merged))
        fills = set()
        for i, g in enumerate(faces):
            for r in g.interiors:
                hp = Polygon(r)
                for j, f in enumerate(faces):
                    if j != i and hp.contains(f.representative_point()):
                        fills.add(j)
        for j, f in enumerate(faces):
            if j in fills:
                continue
            rings.append(_ring(f.exterior.coords))
            rings.extend(_ring(r.coords) for r in f.interiors)
    if not rings:
        raise VectorError("no encontre contornos cerrados en el DXF (usa polilineas cerradas, circulos o cadenas de lineas/arcos conectadas)")
    out = _pack(_shapes_from_rings(rings), scale, flip=True)
    if not out:
        raise VectorError("los contornos del DXF son demasiado pequenos o no encierran area (revisa las unidades)")
    return out


def parse_vector(name: str, raw: bytes):
    low = name.lower()
    if low.endswith(".svg") or raw.lstrip()[:5].lower() in (b"<?xml", b"<svg ") or b"<svg" in raw[:2000]:
        return parse_svg(raw), "svg"
    if low.endswith(".dxf") or b"SECTION" in raw[:4000]:
        return parse_dxf(raw), "dxf"
    raise VectorError("formato no soportado: usa .svg o .dxf")
