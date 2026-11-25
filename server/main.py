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


class DesignIn(BaseModel):
    settings: SettingsIn = SettingsIn()
    shapes: list[ShapeIn] = Field(..., min_length=1, max_length=40)


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
                        "traced": st.get("contours") is not None, "has_bin": st.get("bin_meta") is not None,
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
    st["corners"] = pts.round(1).tolist()
    st["paper"] = {"name": body.paper.upper() if body.paper.upper() in P.PAPERS else "custom",
                   "width_mm": r.paper_mm[0], "height_mm": r.paper_mm[1]}
    st["contours"] = contours_payload(r)
    st["bin_meta"] = None
    st["design"] = None
    write_state(d, st)
    return st["contours"]


_LOCKS: dict[str, threading.Lock] = {}


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


def _safe_name(st: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", st["name"]).strip("_") or "bin"


@app.post("/api/sessions/{sid}/design/init")
def design_init(sid: str, body: InitIn):
    """Crea el diseno inicial: una forma por objeto elegido, ordenadas automaticamente."""
    d = sdir(sid)
    st = read_state(d)
    comps = _comps(st)
    sel = body.components if body.components is not None else [c["id"] for c in comps]
    if not sel or any(i < 0 or i >= len(comps) for i in sel):
        raise HTTPException(422, "seleccion de componentes invalida")
    S = dict(DEFAULT_SETTINGS)
    shapes = [{"id": f"s{n + 1}", "name": f"{st['name']}\u00b7P{n + 1}", "comp": i, "x": 0.0, "y": 0.0, "rot": 0.0,
               "mirror": False, "offset_mm": 0.0, "depth_mm": None, "keep_holes": False} for n, i in enumerate(sel)]
    polys = [local_polygon(comps[sh["comp"]], sh) for sh in shapes]
    for sh, p, (x, y, r) in zip(shapes, polys, arrange(polys, S["clearance_mm"] + 2.0, S["grid_unit_mm"], S["wall_thickness_mm"])):
        u_, v_ = default_notch(p)
        sh.update(x=x, y=y, rot=r, notch={"on": False, "d": FINGER_D, "u": u_, "v": v_})
    design = {"settings": S, "shapes": shapes}
    st["design"], st["bin_meta"] = design, None
    write_state(d, st)
    return design


@app.get("/api/sessions/{sid}/design")
def design_get(sid: str):
    return read_state(sdir(sid)).get("design")


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
def design_build(sid: str, body: DesignIn):
    d = sdir(sid)
    st = read_state(d)
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
        with _LOCKS.setdefault(sid, threading.Lock()):
            chk = export_stl(r.bin, str(d / "bin.tmp.stl"))
            _swap(d / "bin.tmp.stl", d / "bin.stl")
            if not chk["watertight"]:
                r.warnings.append("la malla exportada no es cerrada: revisa los parametros o repara el STL en tu slicer")
            ichk = None
            ins = d / "insert.stl"
            if r.insert is not None:
                ichk = export_stl(r.insert, str(d / "insert.tmp.stl"))
                _swap(d / "insert.tmp.stl", ins)
            elif ins.exists():
                ins.unlink()
            lchk = None
            lid = d / "lid.stl"
            if r.lid is not None:
                lchk = export_stl(r.lid, str(d / "lid.tmp.stl"))
                _swap(d / "lid.tmp.stl", lid)
                if not lchk["watertight"]:
                    r.warnings.append("la malla de la tapa no es cerrada: revisa los parametros o repara el STL en tu slicer")
            elif lid.exists():
                lid.unlink()
    except ValueError as e:
        raise HTTPException(422, str(e))
    vol = sum((m or {}).get("volume_cm3") or 0 for m in (chk, ichk, lchk))
    if vol:
        r.checks.append({"level": "info", "code": "weight", "shapes": [],
                         "msg": f"≈ {vol * 1.24:.0f} g de PLA con relleno 100 % ({vol:.0f} cm³ en total)" + ("" if S["hollow"] else ": con Hollow bin gastas menos")})
    pockets_to_svg(r.pockets, str(d / "bin.svg"))
    meta = {"grid": list(r.grid), "size_mm": [round(r.size_mm[0], 1), round(r.size_mm[1], 1)],
            "total_height_mm": round(r.total_height_mm, 1), "warnings": r.warnings, "problems": r.problems,
            "mesh": chk, "insert": ichk, "has_insert": r.insert is not None,
            "lid": lchk, "has_lid": r.lid is not None, "stats": r.stats, "checks": r.checks,
            "build_seconds": round(time.time() - t0, 2), "stamp": int(time.time() * 1000)}
    st["design"], st["bin_meta"] = {"settings": S, "shapes": shapes}, meta
    write_state(d, st)
    return meta


@app.get("/api/sessions/{sid}/bin.stl")
def bin_stl(sid: str):
    d = sdir(sid)
    if not (d / "bin.stl").exists():
        raise HTTPException(404, "aun no hay bin")
    return FileResponse(d / "bin.stl", media_type="model/stl", filename=f"{_safe_name(read_state(d))}_bin.stl")


@app.get("/api/sessions/{sid}/insert.stl")
def insert_stl(sid: str):
    d = sdir(sid)
    if not (d / "insert.stl").exists():
        raise HTTPException(404, "este bin no tiene inserto")
    return FileResponse(d / "insert.stl", media_type="model/stl", filename=f"{_safe_name(read_state(d))}_insert.stl")


@app.get("/api/sessions/{sid}/lid.stl")
def lid_stl(sid: str):
    d = sdir(sid)
    if not (d / "lid.stl").exists():
        raise HTTPException(404, "este bin no tiene tapa (activa Hollow)")
    return FileResponse(d / "lid.stl", media_type="model/stl", filename=f"{_safe_name(read_state(d))}_lid.stl")


@app.get("/api/sessions/{sid}/bin.zip")
def bin_zip(sid: str):
    d = sdir(sid)
    if not (d / "bin.stl").exists():
        raise HTTPException(404, "aun no hay bin")
    n = _safe_name(read_state(d))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(d / "bin.stl", f"{n}_bin.stl")
        if (d / "insert.stl").exists():
            z.write(d / "insert.stl", f"{n}_insert.stl")
        if (d / "lid.stl").exists():
            z.write(d / "lid.stl", f"{n}_lid.stl")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{n}_stls.zip"'})


@app.get("/api/sessions/{sid}/bin.svg")
def bin_svg(sid: str):
    d = sdir(sid)
    if not (d / "bin.svg").exists():
        raise HTTPException(404, "aun no hay bin")
    return FileResponse(d / "bin.svg", media_type="image/svg+xml", filename=f"{_safe_name(read_state(d))}_shape.svg")


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
