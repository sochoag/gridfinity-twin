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
from shapely.geometry import Polygon

from geometry.gridfinity import BinConfig, build_bin, export_stl, pockets_to_svg
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


class BinParams(BaseModel):
    components: Optional[list[int]] = None
    grid_width: Optional[int] = Field(None, ge=1, le=12)
    grid_depth: Optional[int] = Field(None, ge=1, le=12)
    grid_unit_mm: float = Field(42.0, ge=20, le=80)
    height_units: int = Field(4, ge=1, le=12)
    clearance_mm: float = Field(1.0, ge=0, le=5)
    cutout_depth_mm: float = Field(12.0, ge=1, le=80)
    stacking_lip: bool = True
    magnet_holes: bool = False
    magnet_diameter_mm: float = Field(6.5, ge=4, le=8.2)
    magnet_depth_mm: float = Field(2.4, ge=1, le=4)
    finger_notches: Literal["none", "one_end", "both_ends"] = "none"
    finger_diameter_mm: float = Field(16.0, ge=8, le=30)
    keep_holes: bool = False


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
    write_state(d, st)
    return st["contours"]


@app.post("/api/sessions/{sid}/bin")
def make_bin(sid: str, body: BinParams):
    d = sdir(sid)
    st = read_state(d)
    if not st.get("contours"):
        raise HTTPException(409, "primero traza la silueta")
    comps = st["contours"]["components"]
    sel = body.components if body.components is not None else [c["id"] for c in comps]
    if not sel or any(i < 0 or i >= len(comps) for i in sel):
        raise HTTPException(422, "seleccion de componentes invalida")
    outer = [comps[i]["outer"] for i in range(len(comps))]
    holes = [h for c in comps for h in c["holes"]]
    cfg = BinConfig(components=sel, **body.model_dump(exclude={"components"}))
    t0 = time.time()
    try:
        r = build_bin(outer, holes, cfg)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"no se pudo generar el bin: {e}")
    mesh_check = export_stl(r.manifold, str(d / "bin.stl"))
    if not mesh_check["watertight"]:
        r.warnings.append("la malla exportada no es cerrada: revisa los parametros o repara el STL en tu slicer")
    pockets_to_svg(r.pockets, str(d / "bin.svg"))
    meta = {"grid": list(r.grid), "size_mm": [round(r.size_mm[0], 1), round(r.size_mm[1], 1)],
            "total_height_mm": round(r.total_height_mm, 1), "warnings": r.warnings,
            "mesh": mesh_check, "build_seconds": round(time.time() - t0, 2), "stamp": int(time.time() * 1000)}
    st["bin_params"], st["bin_meta"] = body.model_dump(), meta
    write_state(d, st)
    return meta


@app.get("/api/sessions/{sid}/bin.stl")
def bin_stl(sid: str):
    f = sdir(sid) / "bin.stl"
    if not f.exists():
        raise HTTPException(404, "aun no hay bin")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", read_state(sdir(sid))["name"]) or "bin"
    return FileResponse(f, media_type="model/stl", filename=f"{name}_bin.stl")


@app.get("/api/sessions/{sid}/bin.svg")
def bin_svg(sid: str):
    f = sdir(sid) / "bin.svg"
    if not f.exists():
        raise HTTPException(404, "aun no hay bin")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", read_state(sdir(sid))["name"]) or "bin"
    return FileResponse(f, media_type="image/svg+xml", filename=f"{name}_shape.svg")


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
