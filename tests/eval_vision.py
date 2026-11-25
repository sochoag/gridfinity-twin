"""Evalua la deteccion de hoja y siluetas sobre una carpeta de fotos y guarda imagenes de revision.
Uso: python3 tests/eval_vision.py <carpeta_fotos> <carpeta_salida>"""
import glob, os, sys, time
import cv2, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision import pipeline as P

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
files = sorted(glob.glob(os.path.join(src, "*.jpg")) + glob.glob(os.path.join(src, "*.jpeg")))
rows = []
for f in files:
    name = os.path.splitext(os.path.basename(f))[0]
    img = cv2.imread(f)
    if img is None: continue
    h, w = img.shape[:2]
    t0 = time.time()
    row = {"name": name, "size": f"{w}x{h}"}
    try:
        corners = P.detect_paper(img)
        sc = 900 / max(h, w)
        vis = cv2.resize(img, None, fx=sc, fy=sc)
        cv2.polylines(vis, [np.round(corners * sc).astype(np.int32)], True, (0, 255, 0), 3)
        for i, c in enumerate(corners): cv2.circle(vis, tuple(np.round(c * sc).astype(int)), 8, (0, 0, 255), -1); cv2.putText(vis, str(i), tuple(np.round(c * sc).astype(int) + 8), 0, 1, (0, 0, 255), 2)
        top = np.linalg.norm(corners[1] - corners[0]); left = np.linalg.norm(corners[3] - corners[0])
        bot = np.linalg.norm(corners[2] - corners[3]); right = np.linalg.norm(corners[2] - corners[1])
        ar = max((top + bot) / 2, (left + right) / 2) / min((top + bot) / 2, (left + right) / 2)
        row["ratio"] = round(ar, 3)          # A4 = 1.414
        res = P.run_image(img, P.PAPERS["A4"], corners)
        rect = res.rectified.copy()
        for c in res.outer: cv2.polylines(rect, [np.round(np.array(c) * P.PX_PER_MM).astype(np.int32)], True, (0, 255, 255), 3)
        for c in res.holes: cv2.polylines(rect, [np.round(np.array(c) * P.PX_PER_MM).astype(np.int32)], True, (255, 0, 255), 2)
        s2 = 900 / max(rect.shape[:2]); rect = cv2.resize(rect, None, fx=s2, fy=s2)
        row["objs"] = len(res.outer); row["holes"] = len(res.holes)
        H = 700
        a = cv2.resize(vis, (int(vis.shape[1] * H / vis.shape[0]), H)); b = cv2.resize(rect, (int(rect.shape[1] * H / rect.shape[0]), H))
        cv2.imwrite(os.path.join(out, name + ".jpg"), np.hstack([a, b]), [cv2.IMWRITE_JPEG_QUALITY, 80])
    except Exception as e:
        row["error"] = str(e)[:60]
    row["t"] = round(time.time() - t0, 2)
    rows.append(row); print(row)
