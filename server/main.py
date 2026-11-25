"""Gridfinity Twin: foto -> silueta -> bin Gridfinity (STL/SVG).

Ejecutar:  uvicorn server.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import Point, Polygon

import io
import threading
import zipfile

from geometry.threemf import write_3mf
from geometry.vector import VectorError, parse_vector
from geometry.design import DEFAULT_SETTINGS, FINGER_D, arrange, build_design, comp_of, default_notch, local_polygon
from geometry.gridfinity import export_stl, pockets_to_svg
from vision import pipeline as P

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("TWIN_DATA", ROOT / "data"))
DATA.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"

MAX_UPLOAD = 15 * 1024 * 1024        # bytes
MAX_EDGE = 2400                      # px: la foto se reduce al guardarla
SID_RE = re.compile(r"^[0-9a-f]{32}$")

app = FastAPI(title="Gridfinity Twin")

# Autenticacion basica opcional: define TWIN_PASSWORD (y TWIN_USER, por defecto "admin") para proteger todo salvo /api/health.
_PASS = os.environ.get("TWIN_PASSWORD", "")
_USER = os.environ.get("TWIN_USER", "admin")


@app.middleware("http")
async def basic_auth(request, call_next):
    if not _PASS or request.url.path == "/api/health":
        return await call_next(request)
    hdr = request.headers.get("authorization", "")
    ok = False
    if hdr.lower().startswith("basic "):
        try:
            user, _, pwd = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
            ok = hmac.compare_digest(user, _USER) & hmac.compare_digest(pwd, _PASS)
        except Exception:  # noqa: BLE001
            ok = False
    if ok:
        return await call_next(request)
    return Response("Autenticacion requerida", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Gridfinity Twin"'})


# ------------------------------------------------------------------ utilidades
def sdir(sid: str) -> Path:
    if not SID_RE.match(sid):
        raise HTTPException(400, "id invalido")
    d = DATA / sid
    if not d.is_dir():
        raise HTTPException(404, "sesion no encontrada")
    return d


def read_state(d: Path) -> dict:
    return json.loads((d / "state.json").read_text())


def write_state(d: Path, st: dict):
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(st))
    tmp.replace(d / "state.json")


def paper_size(name: str, w: Optional[float], h: Optional[float]):
    if name.upper() in P.PAPERS:
        return P.PAPERS[name.upper()]
    if name.lower() == "custom" and w and h and 50 <= w <= 2000 and 50 <= h <= 2000:
        if w * h > 450_000:
            raise HTTPException(422, "el area de referencia es demasiado grande (max. ~900 x 500 mm): acerca la camara o usa una referencia mas cercana")
        return (float(w), float(h))
    raise HTTPException(422, "papel no valido (A4, LETTER o custom con ancho/alto en mm)")


def decode_image(raw: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)  # aplica orientacion EXIF
    if img is None:
        try:  # HEIC (opcional: pip install pillow-heif)
            import io

            import pillow_heif
            from PIL import Image

            pillow_heif.register_heif_opener()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            img = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
        except Exception:
            raise HTTPException(415, "formato de imagen no soportado (usa JPG o PNG)")
    h, w = img.shape[:2]
    if max(h, w) > MAX_EDGE:
        f = MAX_EDGE / max(h, w)
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return img


def contours_payload(r: P.Result) -> dict:
    comps = []
    for i, o in enumerate(r.outer):
        poly = Polygon(o).buffer(0)
        hs = [h for h in r.holes if poly.contains(Polygon(h).buffer(0).representative_point())]
        minx, miny, maxx, maxy = poly.bounds
        comps.append({
            "id": i,
            "outer": [[round(x, 2), round(y, 2)] for x, y in o],
            "holes": [[[round(x, 2), round(y, 2)] for x, y in h] for h in hs],
            "area_mm2": round(poly.area, 1),
            "bbox": {"x": round(minx, 2), "y": round(miny, 2),
                     "width": round(maxx - minx, 2), "height": round(maxy - miny, 2)},
        })
    return {"paper": {"width_mm": r.paper_mm[0], "height_mm": r.paper_mm[1]}, "components": comps}


# ------------------------------------------------------------------ modelos
class TraceParams(BaseModel):
    corners: list[list[float]] = Field(..., min_length=4, max_length=4)   # px de la foto guardada
    paper: str = "A4"
    paper_width_mm: Optional[float] = None
    paper_height_mm: Optional[float] = None
    simplification_mm: float = Field(0.35, ge=0.05, le=2.0)
    keep_holes: bool = True


class Notch(BaseModel):
    on: bool = False
    d: float = Field(FINGER_D, ge=6, le=40)
    u: float = Field(0.0, ge=-1000, le=1000)
    v: float = Field(0.0, ge=-1000, le=1000)


class ShapeIn(BaseModel):
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,40}$")
    name: str = Field("Forma", max_length=80)
    comp: Optional[int] = Field(None, ge=0, le=200)   # None: forma importada de otro proyecto (lleva su contorno)
    x: float = Field(0.0, ge=-2000, le=2000)
    y: float = Field(0.0, ge=-2000, le=2000)
    rot: float = Field(0.0, ge=-3600, le=3600)
    mirror: bool = False
    offset_mm: float = Field(0.0, ge=-5, le=10)
    depth_mm: Optional[float] = Field(None, ge=1, le=80)
    keep_holes: bool = False
    notch: Notch = Notch()
    outer: Optional[list[list[float]]] = Field(None, max_length=5000)
    holes: Optional[list[list[list[float]]]] = Field(None, max_length=60)


class SettingsIn(BaseModel):
    auto_size: bool = True
    grid_width: int = Field(4, ge=1, le=12)
    grid_depth: int = Field(4, ge=1, le=12)
    height_units: int = Field(3, ge=1, le=12)
    grid_unit_mm: float = Field(42.0, ge=20, le=80)
    pocket_depth_mm: float = Field(12.0, ge=1, le=80)
    pocket_chamfer: bool = True
    chamfer_mm: float = Field(1.0, ge=0.2, le=3)
    clearance_mm: float = Field(1.0, ge=0, le=5)
    contrast_insert: bool = True
    insert_thickness_mm: float = Field(0.4, ge=0.2, le=2)
    fit_clearance_mm: float = Field(0.2, ge=0, le=1)
    wall_thickness_mm: float = Field(1.6, ge=0.8, le=6)
    stacking_lip: bool = True
    magnet_holes: bool = False
    magnet_diameter_mm: float = Field(6.5, ge=4, le=8.2)
    magnet_depth_mm: float = Field(2.4, ge=1, le=4)
    partial: bool = False
    cells_off: list[str] = Field(default_factory=list, max_length=144)
    bed_x_mm: float = Field(256.0, ge=50, le=1000)
    bed_y_mm: float = Field(256.0, ge=50, le=1000)
    nozzle_mm: float = Field(0.4, ge=0.2, le=1.2)
    hollow: bool = False
    hollow_rib_mm: float = Field(2.4, ge=1.2, le=6)
    hollow_floor_mm: float = Field(1.2, ge=0.8, le=4)
    lid_thickness_mm: float = Field(1.2, ge=0.8, le=3)
    lid_ledge_mm: float = Field(0.8, ge=0, le=3)
    lid_clearance_mm: float = Field(0.2, ge=0, le=0.6)
    dividers_x: list[float] = Field(default_factory=list, max_length=20)
    dividers_y: list[float] = Field(default_factory=list, max_length=20)
    divider_thickness_mm: float = Field(1.2, ge=0.6, le=4)
    divider_clearance_mm: float = Field(0.2, ge=0, le=0.6)
    divider_slot_depth_mm: float = Field(0.8, ge=0, le=3)
    dividers_mode: Literal["loose", "fixed"] = "loose"


class DesignIn(BaseModel):
    settings: SettingsIn = SettingsIn()
    shapes: list[ShapeIn] = Field(default_factory=list, max_length=40)


class InitIn(BaseModel):
    components: Optional[list[int]] = None


class ArrangeIn(BaseModel):
    settings: SettingsIn = SettingsIn()
    shapes: list[ShapeIn] = Field(..., min_length=1, max_length=40)


class Rename(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


# ------------------------------------------------------------------ API
@app.post("/api/sessions")
def create_session(file: UploadFile = File(...), paper: str = Form("A4"),
                   paper_width_mm: Optional[float] = Form(None), paper_height_mm: Optional[float] = Form(None)):
    raw = file.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "la imagen supera 15 MB")
    img = decode_image(raw)
    psize = paper_size(paper, paper_width_mm, paper_height_mm)
    sid = uuid.uuid4().hex
    d = DATA / sid
    d.mkdir()
    cv2.imwrite(str(d / "source.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    h, w = img.shape[:2]
    try:
        corners = P.detect_paper(img)
        detected = True
    except Exception:
        m = 0.1
        corners = np.array([[w * m, h * m], [w * (1 - m), h * m], [w * (1 - m), h * (1 - m)], [w * m, h * (1 - m)]], np.float32)
        detected = False
    name = Path(file.filename or "Sin nombre").stem[:80] or "Sin nombre"
    st = {"id": sid, "name": name, "created": time.time(), "source": {"width": w, "height": h},
          "paper": {"name": paper.upper() if paper.upper() in P.PAPERS else "custom",
                    "width_mm": psize[0], "height_mm": psize[1]},
          "corners": corners.round(1).tolist(), "corners_detected": detected,
          "contours": None, "bin_params": None, "bin_meta": None}
    write_state(d, st)
    return st


@app.get("/api/sessions")
def list_sessions():
    out = []
    for d in DATA.iterdir():
        if d.is_dir() and SID_RE.match(d.name) and (d / "state.json").exists():
            st = read_state(d)
            out.append({"id": st["id"], "name": st["name"], "created": st["created"],
                        "traced": st.get("contours") is not None, "has_bin": st.get("bin_meta") is not None or any(b.get("bin_meta") for b in (st.get("bins") or {}).values()),
                        "n_components": len(st["contours"]["components"]) if st.get("contours") else 0})
    return sorted(out, key=lambda s: -s["created"])


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    return read_state(sdir(sid))


@app.patch("/api/sessions/{sid}")
def rename_session(sid: str, body: Rename):
    d = sdir(sid)
    st = read_state(d)
    st["name"] = body.name.strip()
    write_state(d, st)
    return {"ok": True, "name": st["name"]}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    shutil.rmtree(sdir(sid))
    return {"ok": True}


@app.get("/api/sessions/{sid}/source.jpg")
def source_image(sid: str):
    return FileResponse(sdir(sid) / "source.jpg", media_type="image/jpeg")


@app.get("/api/sessions/{sid}/rectified.jpg")
def rectified_image(sid: str):
    f = sdir(sid) / "rectified.jpg"
    if not f.exists():
        raise HTTPException(404, "aun no hay trazado")
    return FileResponse(f, media_type="image/jpeg")


@app.post("/api/sessions/{sid}/trace")
def trace(sid: str, body: TraceParams):
    d = sdir(sid)
    st = read_state(d)
    if st.get("merged"):
        raise HTTPException(409, "este proyecto es una fusion de otros: no tiene foto propia que volver a trazar")
    img = cv2.imread(str(d / "source.jpg"))
    h, w = img.shape[:2]
    pts = np.array(body.corners, np.float32)
    if pts.shape != (4, 2) or (pts < -w).any() or (pts > 2 * w).any():
        raise HTTPException(422, "esquinas invalidas")
    if cv2.contourArea(P.order_corners(pts)) < 0.02 * w * h:
        raise HTTPException(422, "el area del papel es demasiado pequena")
    psize = paper_size(body.paper, body.paper_width_mm, body.paper_height_mm)
    try:
        r = P.run_image(img, psize, pts, eps_mm=body.simplification_mm, keep_holes=body.keep_holes)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"no se pudo trazar: {e}")
    if not r.outer:
        raise HTTPException(422, "no se detecto ningun objeto sobre el papel")
    cv2.imwrite(str(d / "rectified.jpg"), r.rectified, [cv2.IMWRITE_JPEG_QUALITY, 88])
    cv2.imwrite(str(d / "mask_auto.png"), r.mask)      # mascara automatica: base para las ediciones manuales
    st["mask_edits"] = []
    st["corners"] = pts.round(1).tolist()
    st["paper"] = {"name": body.paper.upper() if body.paper.upper() in P.PAPERS else "custom",
                   "width_mm": r.paper_mm[0], "height_mm": r.paper_mm[1]}
    st["contours"] = contours_payload(r)
    st["bin_meta"] = None
    st["design"] = None
    _drop_extra_bins(d, st)        # los demas bins apuntaban a objetos del trazado anterior
    write_state(d, st)
    return st["contours"]


class MaskEdit(BaseModel):
    op: Literal["add", "erase"]
    kind: Literal["brush", "lasso"] = "brush"
    r: float = Field(5.0, ge=0.5, le=80)
    pts: list[list[float]] = Field(..., min_length=1, max_length=4000)


class MaskParams(BaseModel):
    edits: list[MaskEdit] = Field(default_factory=list, max_length=400)
    simplification_mm: float = Field(0.35, ge=0.05, le=2.0)
    keep_holes: bool = True


@app.post("/api/sessions/{sid}/mask")
def mask_edit(sid: str, body: MaskParams):
    """Recalcula los contornos aplicando pinceladas / lazos sobre la mascara automatica."""
    d = sdir(sid)
    st = read_state(d)
    f = d / "mask_auto.png"
    if not st.get("contours") or not f.exists():
        raise HTTPException(409, "vuelve a trazar la silueta para poder editarla a mano")
    auto = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
    pw, ph = st["contours"]["paper"]["width_mm"], st["contours"]["paper"]["height_mm"]
    edits = [e.model_dump() for e in body.edits]
    for e in edits:
        e["pts"] = [[min(max(x, -50.0), pw + 50.0), min(max(y, -50.0), ph + 50.0)] for x, y in e["pts"]]
    mask = P.apply_edits(auto, edits)
    outer, holes = P.extract_contours(mask, eps_mm=body.simplification_mm, keep_holes=body.keep_holes)
    if not outer:
        raise HTTPException(422, "con esas ediciones no queda ningun objeto")
    res = P.sort_components(P.Result((pw, ph), np.zeros((4, 2), np.float32), np.zeros((1, 1, 3), np.uint8), mask, outer, holes))
    st["contours"] = contours_payload(res)
    st["mask_edits"] = edits
    st["bin_meta"] = None
    st["design"] = None
    write_state(d, st)
    return {"contours": st["contours"], "edits": edits}


_LOCKS: dict[str, threading.Lock] = {}


def _drop_extra_bins(d: Path, st: dict):
    for k in list((st.get("bins") or {})):
        shutil.rmtree(d / k, ignore_errors=True)
    st["bins"] = {}
    for f in ("bin.stl", "insert.stl", "lid.stl", "dividers.stl", "bin.svg"):
        (d / f).unlink(missing_ok=True)


def _swap(tmp: Path, final: Path):
    tmp.replace(final)   # atomico: una descarga en curso sigue leyendo el archivo anterior


def _comps(st: dict) -> list[dict]:
    if not st.get("contours"):
        raise HTTPException(409, "primero traza la silueta")
    return st["contours"]["components"]


def _check_shapes(shapes: list[dict], comps: list[dict]):
    for s in shapes:
        if s["comp"] is None:
            if not s.get("outer"):
                raise HTTPException(422, f"la forma '{s['name']}' no tiene contorno")
        elif s["comp"] >= len(comps):
            raise HTTPException(422, "componente inexistente")


BIN_RE = re.compile(r"^b[0-9]{1,3}$")
MAIN_BIN = "b1"


def bin_id(b: Optional[str]) -> str:
    b = b or MAIN_BIN
    if not BIN_RE.match(b):
        raise HTTPException(400, "id de bin invalido")
    return b


def bin_dir(d: Path, bid: str, create: bool = False) -> Path:
    """b1 (el bin principal) vive en la carpeta de la sesion (compatible con proyectos antiguos);
    los demas, en d/<id>/."""
    if bid == MAIN_BIN:
        return d
    p = d / bid
    if create:
        p.mkdir(exist_ok=True)
    return p


def holder(st: dict, bid: str) -> dict:
    """Donde se guardan design / bin_meta de un bin: el propio estado (b1) o st['bins'][id]."""
    if bid == MAIN_BIN:
        return st
    h = (st.get("bins") or {}).get(bid)
    if h is None:
        raise HTTPException(404, "bin no encontrado")
    return h


def bin_name(st: dict, bid: str) -> str:
    return st.get("bin1_name", "Bin 1") if bid == MAIN_BIN else holder(st, bid).get("name", bid)


def _safe_name(st: dict, bid: str = MAIN_BIN) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", st["name"]).strip("_") or "bin"
    if bid != MAIN_BIN:
        extra = re.sub(r"[^A-Za-z0-9_-]+", "_", bin_name(st, bid)).strip("_")
        base = f"{base}_{extra or bid}"
    return base


class BinIn(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    copy_from: Optional[str] = None
    mode: Optional[Literal["complement", "empty"]] = None   # complement: Partial Bin que rellena el hueco de copy_from; empty: contenedor sin siluetas


@app.get("/api/sessions/{sid}/bins")
def bins_list(sid: str):
    st = read_state(sdir(sid))
    out = [{"id": MAIN_BIN, "name": bin_name(st, MAIN_BIN), "has_design": st.get("design") is not None,
            "has_bin": st.get("bin_meta") is not None}]
    for k in sorted((st.get("bins") or {}), key=lambda x: int(x[1:])):
        h = st["bins"][k]
        out.append({"id": k, "name": h.get("name", k), "has_design": h.get("design") is not None,
                    "has_bin": h.get("bin_meta") is not None})
    return out


@app.post("/api/sessions/{sid}/bins")
def bins_create(sid: str, body: BinIn):
    d = sdir(sid)
    st = read_state(d)
    bins = st.setdefault("bins", {})
    if len(bins) >= 11:
        raise HTTPException(422, "maximo 12 bins por proyecto")
    n = 2
    while f"b{n}" in bins:
        n += 1
    bid = f"b{n}"
    design = None
    if body.mode:
        src = holder(st, bin_id(body.copy_from))
        sd = src.get("design")
        if not sd:
            raise HTTPException(422, "el bin de origen aun no tiene diseno")
        S0 = {**DEFAULT_SETTINGS, **sd["settings"]}
        meta = src.get("bin_meta")
        gw, gd = (meta["grid"] if (S0["auto_size"] and meta) else (S0["grid_width"], S0["grid_depth"]))
        S1 = {**S0, "auto_size": False, "grid_width": int(gw), "grid_depth": int(gd), "hollow": False,
              "dividers_x": [], "dividers_y": []}
        if body.mode == "complement":
            off = set(S0["cells_off"]) if S0["partial"] else set()
            if not off:
                raise HTTPException(422, "el bin de origen no es parcial: activa Partial bin y apaga alguna celda")
            S1.update(partial=True, cells_off=[f"{i},{j}" for j in range(gd) for i in range(gw) if f"{i},{j}" not in off])
        else:
            S1.update(partial=False, cells_off=[])
        design = {"settings": S1, "shapes": []}
    elif body.copy_from:
        src = holder(st, bin_id(body.copy_from)).get("design")
        design = json.loads(json.dumps(src)) if src else None
    bins[bid] = {"name": (body.name or f"Bin {n}").strip() or f"Bin {n}", "design": design, "bin_meta": None}
    write_state(d, st)
    return {"id": bid, "name": bins[bid]["name"], "design": design}


@app.patch("/api/sessions/{sid}/bins/{bid}")
def bins_rename(sid: str, bid: str, body: Rename):
    d = sdir(sid)
    st = read_state(d)
    bid = bin_id(bid)
    if bid == MAIN_BIN:
        st["bin1_name"] = body.name.strip()
    else:
        holder(st, bid)["name"] = body.name.strip()
    write_state(d, st)
    return {"ok": True}


@app.delete("/api/sessions/{sid}/bins/{bid}")
def bins_delete(sid: str, bid: str):
    d = sdir(sid)
    st = read_state(d)
    bid = bin_id(bid)
    if bid == MAIN_BIN:
        raise HTTPException(422, "el bin principal no se puede borrar")
    holder(st, bid)
    del st["bins"][bid]
    shutil.rmtree(d / bid, ignore_errors=True)
    write_state(d, st)
    return {"ok": True}


@app.post("/api/sessions/{sid}/design/init")
def design_init(sid: str, body: InitIn, bin: Optional[str] = None):
    """Crea el diseno inicial: una forma por objeto elegido, ordenadas automaticamente."""
    d = sdir(sid)
    st = read_state(d)
    bid = bin_id(bin)
    comps = _comps(st)
    sel = body.components if body.components is not None else [c["id"] for c in comps]
    if not sel or any(i < 0 or i >= len(comps) for i in sel):
        raise HTTPException(422, "seleccion de componentes invalida")
    S = dict(DEFAULT_SETTINGS)
    shapes = [{"id": f"s{n + 1}", "name": f"{st['name']}·P{n + 1}", "comp": i, "x": 0.0, "y": 0.0, "rot": 0.0,
               "mirror": False, "offset_mm": 0.0, "depth_mm": None, "keep_holes": False} for n, i in enumerate(sel)]
    polys = [local_polygon(comps[sh["comp"]], sh) for sh in shapes]
    for sh, p, (x, y, r) in zip(shapes, polys, arrange(polys, S["clearance_mm"] + 2.0, S["grid_unit_mm"], S["wall_thickness_mm"])):
        u_, v_ = default_notch(p)
        sh.update(x=x, y=y, rot=r, notch={"on": False, "d": FINGER_D, "u": u_, "v": v_})
    design = {"settings": S, "shapes": shapes}
    h = holder(st, bid)
    h["design"], h["bin_meta"] = design, None
    write_state(d, st)
    return design


@app.get("/api/sessions/{sid}/design")
def design_get(sid: str, bin: Optional[str] = None):
    st = read_state(sdir(sid))
    return holder(st, bin_id(bin)).get("design")


@app.post("/api/sessions/{sid}/design/arrange")
def design_arrange(sid: str, body: ArrangeIn):
    st = read_state(sdir(sid))
    comps = _comps(st)
    shapes = [s.model_dump() for s in body.shapes]
    _check_shapes(shapes, comps)
    S = body.settings
    polys = []
    for sh in shapes:
        p = local_polygon(comp_of(comps, sh), sh)
        p = p.buffer(sh["offset_mm"], 12) if sh["offset_mm"] else p
        n = sh["notch"]
        if n["on"]:
            p = p.union(Point(n["u"], n["v"]).buffer(n["d"] / 2, 24))
        polys.append(p)
    res = arrange(polys, S.clearance_mm + 2.0, S.grid_unit_mm, S.wall_thickness_mm)
    return [{"id": sh["id"], "x": x, "y": y, "rot": r} for sh, (x, y, r) in zip(shapes, res)]


@app.put("/api/sessions/{sid}/design")
def design_build(sid: str, body: DesignIn, bin: Optional[str] = None):
    d = sdir(sid)
    st = read_state(d)
    bid = bin_id(bin)
    holder(st, bid)
    bd = bin_dir(d, bid, create=True)
    comps = _comps(st)
    shapes = [s.model_dump() for s in body.shapes]
    S = body.settings.model_dump()
    _check_shapes(shapes, comps)
    if len({s["id"] for s in shapes}) != len(shapes):
        raise HTTPException(422, "ids de forma repetidos")
    t0 = time.time()
    try:
        r = build_design(comps, shapes, S)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"no se pudo generar el bin: {e}")
    try:
        with _LOCKS.setdefault(f"{sid}/{bid}", threading.Lock()):
            chk = export_stl(r.bin, str(bd / "bin.tmp.stl"))
            _swap(bd / "bin.tmp.stl", bd / "bin.stl")
            if not chk["watertight"]:
                r.warnings.append("la malla exportada no es cerrada: revisa los parametros o repara el STL en tu slicer")
            outs = {}
            for key, mesh, label in (("insert", r.insert, "el inserto"), ("lid", r.lid, "la tapa"), ("dividers", r.dividers, "los divisores")):
                f = bd / f"{key}.stl"
                if mesh is not None:
                    outs[key] = export_stl(mesh, str(bd / f"{key}.tmp.stl"))
                    _swap(bd / f"{key}.tmp.stl", f)
                    if key != "insert" and not outs[key]["watertight"]:
                        r.warnings.append(f"la malla de {label} no es cerrada: revisa los parametros o repara el STL en tu slicer")
                elif f.exists():
                    f.unlink()
            ichk, lchk, dchk = outs.get("insert"), outs.get("lid"), outs.get("dividers")
    except ValueError as e:
        raise HTTPException(422, str(e))
    vol = sum((m or {}).get("volume_cm3") or 0 for m in (chk, ichk, lchk, dchk))
    if vol:
        r.checks.append({"level": "info", "code": "weight", "shapes": [],
                         "msg": f"≈ {vol * 1.24:.0f} g de PLA con relleno 100 % ({vol:.0f} cm³ en total)" + ("" if S["hollow"] else ": con Hollow bin gastas menos")})
    pockets_to_svg(r.pockets, str(bd / "bin.svg"))
    meta = {"grid": list(r.grid), "size_mm": [round(r.size_mm[0], 1), round(r.size_mm[1], 1)],
            "total_height_mm": round(r.total_height_mm, 1), "warnings": r.warnings, "problems": r.problems,
            "mesh": chk, "insert": ichk, "has_insert": r.insert is not None,
            "lid": lchk, "has_lid": r.lid is not None, "dividers": dchk, "has_dividers": r.dividers is not None,
            "stats": r.stats, "checks": r.checks,
            "build_seconds": round(time.time() - t0, 2), "stamp": int(time.time() * 1000)}
    h = holder(st, bid)
    h["design"], h["bin_meta"] = {"settings": S, "shapes": shapes}, meta
    write_state(d, st)
    return meta


_PARTS = {"bin": ("bin.stl", "aun no hay bin"), "insert": ("insert.stl", "este bin no tiene inserto"),
          "lid": ("lid.stl", "este bin no tiene tapa (activa Hollow)"),
          "dividers": ("dividers.stl", "este bin no tiene divisores sueltos")}


def _part(sid: str, bin: Optional[str], part: str):
    d = sdir(sid)
    bid = bin_id(bin)
    st = read_state(d)
    holder(st, bid)
    f = bin_dir(d, bid) / _PARTS[part][0]
    if not f.exists():
        raise HTTPException(404, _PARTS[part][1])
    return FileResponse(f, media_type="model/stl", filename=f"{_safe_name(st, bid)}_{part}.stl")


@app.get("/api/sessions/{sid}/bin.stl")
def bin_stl(sid: str, bin: Optional[str] = None):
    return _part(sid, bin, "bin")


@app.get("/api/sessions/{sid}/insert.stl")
def insert_stl(sid: str, bin: Optional[str] = None):
    return _part(sid, bin, "insert")


@app.get("/api/sessions/{sid}/lid.stl")
def lid_stl(sid: str, bin: Optional[str] = None):
    return _part(sid, bin, "lid")


@app.get("/api/sessions/{sid}/dividers.stl")
def dividers_stl(sid: str, bin: Optional[str] = None):
    return _part(sid, bin, "dividers")


def _bin_files(d: Path, st: dict, bid: str) -> list[tuple[str, Path]]:
    bd, n = bin_dir(d, bid), _safe_name(st, bid)
    return [(f"{n}_{k}.stl", bd / _PARTS[k][0]) for k in ("bin", "insert", "lid", "dividers") if (bd / _PARTS[k][0]).exists()]


@app.get("/api/sessions/{sid}/bin.zip")
def bin_zip(sid: str, bin: Optional[str] = None, all: bool = False):
    d = sdir(sid)
    st = read_state(d)
    bid = bin_id(bin)
    ids = [MAIN_BIN] + list(st.get("bins") or {}) if all else [bid]
    files = [x for b in ids for x in _bin_files(d, st, b)]
    if not files:
        raise HTTPException(404, "aun no hay bin")
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", st["name"]).strip("_") or "bin" if all else _safe_name(st, bid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, f in files:
            z.write(f, arc)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{n}_stls.zip"'})


_COLORS = {"bin": "#3A7BD5", "insert": "#E8E8E8", "lid": "#7FB069", "dividers": "#E9A23B"}


@app.get("/api/sessions/{sid}/bin.3mf")
def bin_3mf(sid: str, bin: Optional[str] = None, all: bool = False):
    """3MF con una pieza por objeto (bin, inserto, tapa, divisores), colores distintos y colocadas en fila."""
    import trimesh
    d = sdir(sid)
    st = read_state(d)
    ids = [MAIN_BIN] + list(st.get("bins") or {}) if all else [bin_id(bin)]
    objs = []
    for b in ids:
        bd, n = bin_dir(d, b), _safe_name(st, b)
        for k in ("bin", "insert", "lid", "dividers"):
            f = bd / _PARTS[k][0]
            if f.exists():
                m = trimesh.load(str(f), force="mesh")
                objs.append((f"{n}_{k}", m.vertices, m.faces, _COLORS[k]))
    if not objs:
        raise HTTPException(404, "aun no hay bin")
    n = re.sub(r"[^A-Za-z0-9_-]+", "_", st["name"]).strip("_") or "bin" if all else _safe_name(st, ids[0])
    return Response(write_3mf(objs), media_type="model/3mf",
                    headers={"Content-Disposition": f'attachment; filename="{n}.3mf"'})


@app.get("/api/sessions/{sid}/bin.svg")
def bin_svg(sid: str, bin: Optional[str] = None):
    d = sdir(sid)
    bid = bin_id(bin)
    st = read_state(d)
    holder(st, bid)
    f = bin_dir(d, bid) / "bin.svg"
    if not f.exists():
        raise HTTPException(404, "aun no hay bin")
    return FileResponse(f, media_type="image/svg+xml", filename=f"{_safe_name(st, bid)}_shape.svg")


# ------------------------------------------------------------------ fusionar proyectos
class MergeIn(BaseModel):
    ids: list[str] = Field(..., min_length=2, max_length=12)
    name: Optional[str] = Field(None, max_length=80)


@app.post("/api/sessions/merge")
def merge_sessions(body: MergeIn):
    """Crea un proyecto nuevo con las siluetas de varios proyectos ya trazados (los originales no se tocan)."""
    if len(set(body.ids)) != len(body.ids):
        raise HTTPException(422, "proyectos repetidos")
    srcs = []
    for i in body.ids:
        d0 = sdir(i)
        st0 = read_state(d0)
        if not st0.get("contours"):
            raise HTTPException(422, f"'{st0['name']}' aun no esta trazado")
        srcs.append((d0, st0))
    comps, x, y, rowh, maxw = [], 0.0, 0.0, 0.0, 0.0
    ROW_W, GAP = 400.0, 10.0
    for _, st0 in srcs:
        for c in st0["contours"]["components"]:
            b = c["bbox"]
            if x > 0 and x + b["width"] > ROW_W:
                x, y, rowh = 0.0, y + rowh + GAP, 0.0
            dx, dy = x - b["x"], y - b["y"]

            def mv(ring, dx=dx, dy=dy):
                return [[round(px + dx, 2), round(py + dy, 2)] for px, py in ring]

            comps.append({"id": len(comps), "outer": mv(c["outer"]), "holes": [mv(h) for h in c["holes"]],
                          "area_mm2": c["area_mm2"],
                          "bbox": {"x": round(x, 2), "y": round(y, 2), "width": b["width"], "height": b["height"]}})
            x += b["width"] + GAP
            rowh = max(rowh, b["height"])
            maxw = max(maxw, x - GAP)
    if len(comps) > 200:
        raise HTTPException(422, "demasiadas siluetas (max. 200)")
    sid = uuid.uuid4().hex
    d = DATA / sid
    d.mkdir()
    shutil.copy(srcs[0][0] / "source.jpg", d / "source.jpg")
    st = {"id": sid, "name": (body.name or "Fusion: " + " + ".join(s[1]["name"] for s in srcs))[:80],
          "created": time.time(), "source": srcs[0][1]["source"], "paper": srcs[0][1]["paper"],
          "corners": srcs[0][1]["corners"], "corners_detected": False, "merged": [s[1]["id"] for s in srcs],
          "contours": {"paper": {"width_mm": round(max(maxw, 50.0), 1), "height_mm": round(max(y + rowh, 50.0), 1)}, "components": comps},
          "bin_params": None, "bin_meta": None}
    write_state(d, st)
    return st


# ------------------------------------------------------------------ biblioteca de herramientas (formas con nombre)
LIB = DATA / "library.json"
_LIB_LOCK = threading.Lock()


class LibShape(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=12)
    outer: list[list[float]] = Field(..., min_length=3, max_length=5000)
    holes: list[list[list[float]]] = Field(default_factory=list, max_length=60)
    depth_mm: Optional[float] = Field(None, ge=1, le=80)
    offset_mm: float = Field(0.0, ge=-5, le=10)
    notch: Notch = Notch()


class LibPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    tags: Optional[list[str]] = Field(None, max_length=12)


def _lib_read() -> list[dict]:
    try:
        return json.loads(LIB.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _lib_write(items: list[dict]):
    tmp = LIB.with_suffix(".tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(LIB)


def _lib_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        t = re.sub(r"\s+", " ", t.strip().lower())[:30]
        if t and t not in out:
            out.append(t)
    return out


def _lib_view(it: dict) -> dict:
    xs, ys = [p[0] for p in it["outer"]], [p[1] for p in it["outer"]]
    return {**it, "bbox": {"x": min(xs), "y": min(ys), "width": round(max(xs) - min(xs), 2), "height": round(max(ys) - min(ys), 2)}}


@app.get("/api/library")
def library_list():
    return [_lib_view(i) for i in sorted(_lib_read(), key=lambda i: -i["created"])]


@app.post("/api/library")
def library_add(body: LibShape):
    p = Polygon(body.outer).buffer(0)
    if p.is_empty or p.area < 1.0:
        raise HTTPException(422, "contorno no valido")
    it = {"id": uuid.uuid4().hex[:12], "created": time.time(), "name": body.name.strip(), "tags": _lib_tags(body.tags),
          "outer": [[round(a, 2), round(b, 2)] for a, b in body.outer],
          "holes": [[[round(a, 2), round(b, 2)] for a, b in h] for h in body.holes],
          "depth_mm": body.depth_mm, "offset_mm": body.offset_mm, "notch": body.notch.model_dump()}
    with _LIB_LOCK:
        items = _lib_read()
        if len(items) >= 500:
            raise HTTPException(422, "la biblioteca esta llena (max. 500 formas)")
        items.append(it)
        _lib_write(items)
    return _lib_view(it)


@app.patch("/api/library/{lid}")
def library_patch(lid: str, body: LibPatch):
    with _LIB_LOCK:
        items = _lib_read()
        it = next((i for i in items if i["id"] == lid), None)
        if it is None:
            raise HTTPException(404, "forma no encontrada")
        if body.name is not None:
            it["name"] = body.name.strip()
        if body.tags is not None:
            it["tags"] = _lib_tags(body.tags)
        _lib_write(items)
    return _lib_view(it)


@app.delete("/api/library/{lid}")
def library_delete(lid: str):
    with _LIB_LOCK:
        items = _lib_read()
        keep = [i for i in items if i["id"] != lid]
        if len(keep) == len(items):
            raise HTTPException(404, "forma no encontrada")
        _lib_write(keep)
    return {"ok": True}


@app.post("/api/vector/parse")
async def vector_parse(file: UploadFile = File(...)):
    """SVG / DXF -> contornos en mm (sin guardar nada). El cliente elige cuales importar."""
    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "archivo demasiado grande (max. 8 MB)")
    try:
        shapes, fmt = parse_vector(file.filename or "", raw)
    except VectorError as e:
        raise HTTPException(422, str(e))
    out = []
    for i, sh in enumerate(shapes):
        xs, ys = [p[0] for p in sh["outer"]], [p[1] for p in sh["outer"]]
        out.append({"id": i, "outer": sh["outer"], "holes": sh["holes"],
                    "bbox": {"x": min(xs), "y": min(ys), "width": round(max(xs) - min(xs), 3), "height": round(max(ys) - min(ys), 3)}})
    return {"format": fmt, "shapes": out}


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
