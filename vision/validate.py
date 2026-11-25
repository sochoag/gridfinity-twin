"""Compara el pipeline propio con los contornos de ShapeAny (IoU por componente) y guarda un overlay."""
import sys

import cv2
import numpy as np

sys.path.insert(0, "/home/claude/twin")
from ref.shapeany_gt import OUTER, HOLES, PAPER  # noqa: E402
from vision import pipeline as P  # noqa: E402

IMG = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/fa61d9ae-image.jpg"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/claude/twin/out"

import os
os.makedirs(OUT, exist_ok=True)

res = P.run(IMG)
H, W = res.mask.shape


def raster(poly, holes=()):
    m = np.zeros((H, W), np.uint8)
    pts = (np.array(poly) * P.PX_PER_MM).round().astype(np.int32)
    cv2.fillPoly(m, [pts], 255)
    for h in holes:
        hp = (np.array(h) * P.PX_PER_MM).round().astype(np.int32)
        cv2.fillPoly(m, [hp], 0)
    return m


def iou(a, b):
    i = np.logical_and(a > 0, b > 0).sum()
    u = np.logical_or(a > 0, b > 0).sum()
    return i / u if u else 0.0


print("papel (mm):", res.paper_mm, "| esquinas foto:", res.corners.round(0).tolist())
print(f"componentes propios: {len(res.outer)} | huecos propios: {len(res.holes)} | puntos: {[len(c) for c in res.outer]}")

names = ["cutter rojo", "cutter negro", "pinzas", "bisturi"]
mine = [raster(c) for c in res.outer]
rows = []
for gi, g in enumerate(OUTER):
    gm = raster(g, [h for h in HOLES if np.mean([p[0] for p in h]) > min(p[0] for p in g)
                    and np.mean([p[0] for p in h]) < max(p[0] for p in g)])
    best = max(range(len(mine)), key=lambda i: iou(gm, mine[i])) if mine else None
    v = iou(gm, mine[best]) if mine else 0
    rows.append((names[gi], best, v))
    print(f"  {names[gi]:<13} IoU={v:.3f}  (mejor componente propio #{best})")

# dilatacion del GT por 1 mm (clearance) por si la API devuelve el contorno ya expandido
k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * P.PX_PER_MM + 1,) * 2)
print("IoU contra GT erosionado 1 mm (por si el GT trae clearance):")
for gi, g in enumerate(OUTER):
    gm = cv2.erode(raster(g), k)
    best = max(range(len(mine)), key=lambda i: iou(gm, mine[i]))
    print(f"  {names[gi]:<13} IoU={iou(gm, mine[best]):.3f}")

# overlay: GT verde, propio rojo
ov = res.rectified.copy()
for g in OUTER:
    cv2.polylines(ov, [(np.array(g) * P.PX_PER_MM).round().astype(np.int32)], True, (0, 220, 0), 2)
for h in HOLES:
    cv2.polylines(ov, [(np.array(h) * P.PX_PER_MM).round().astype(np.int32)], True, (0, 220, 0), 2)
for c in res.outer:
    cv2.polylines(ov, [(np.array(c) * P.PX_PER_MM).round().astype(np.int32)], True, (0, 0, 255), 2)
cv2.imwrite(f"{OUT}/overlay.png", ov)
cv2.imwrite(f"{OUT}/rectified.png", res.rectified)
cv2.imwrite(f"{OUT}/mask.png", res.mask)
print("guardado en", OUT)
