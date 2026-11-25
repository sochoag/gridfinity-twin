import os
import sys
import tempfile

import numpy as np
import trimesh

os.environ["TWIN_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from fastapi.testclient import TestClient  # noqa: E402

from server.main import app  # noqa: E402

c = TestClient(app)
PHOTO = os.path.join(os.path.dirname(__file__), "data", "cuchillas.jpg")


def _session():
    with open(PHOTO, "rb") as f:
        r = c.post("/api/sessions", files={"file": ("Cuchillas.jpg", f, "image/jpeg")}, data={"paper": "A4"})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow():
    st = _session()
    sid = st["id"]
    assert st["corners_detected"] is True
    exp = np.array([[389, 114], [1058, 139], [1047, 603], [384, 597]])
    assert np.abs(np.array(st["corners"]) - exp).max() < 6, st["corners"]

    r = c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert r.status_code == 200, r.text
    cont = r.json()
    assert len(cont["components"]) == 4
    assert cont["paper"] == {"width_mm": 297.0, "height_mm": 210.0}
    xs = [min(p[0] for p in k["outer"]) for k in cont["components"]]
    assert xs == sorted(xs)                       # orden izquierda -> derecha
    assert any(k["holes"] for k in cont["components"])

    assert c.get(f"/api/sessions/{sid}/rectified.jpg").status_code == 200
    r = c.post(f"/api/sessions/{sid}/bin", json={"components": [2], "height_units": 4, "cutout_depth_mm": 14,
                                                  "magnet_holes": True, "finger_notches": "one_end"})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["grid"] == [3, 4] and meta["warnings"] == []
    assert meta["mesh"]["watertight"] and meta["mesh"]["degenerate_faces"] == 0
    stl = c.get(f"/api/sessions/{sid}/bin.stl")
    assert stl.status_code == 200
    import io
    tm = trimesh.load(io.BytesIO(stl.content), file_type="stl")
    assert tm.is_watertight and abs(tm.bounds[1][2] - 32.4) < 0.05
    assert c.get(f"/api/sessions/{sid}/bin.svg").text.startswith("<svg")

    # biblioteca
    lst = c.get("/api/sessions").json()
    assert lst[0]["id"] == sid and lst[0]["traced"] and lst[0]["has_bin"] and lst[0]["n_components"] == 4
    assert c.patch(f"/api/sessions/{sid}", json={"name": "Pinzas"}).json()["name"] == "Pinzas"
    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert c.get(f"/api/sessions/{sid}").status_code == 404


def test_validation():
    st = _session()
    sid = st["id"]
    assert c.post(f"/api/sessions/{sid}/bin", json={}).status_code == 409           # sin trazado
    assert c.get("/api/sessions/../../etc/passwd/source.jpg").status_code in (400, 404)
    assert c.get("/api/sessions/xyz/source.jpg").status_code == 400
    bad = c.post(f"/api/sessions/{sid}/trace", json={"corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "paper": "A4"})
    assert bad.status_code == 422
    assert c.post("/api/sessions", files={"file": ("x.txt", b"no es una imagen", "text/plain")}).status_code == 415
    ok = c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert ok.status_code == 200
    assert c.post(f"/api/sessions/{sid}/bin", json={"components": [9]}).status_code == 422
    assert c.post(f"/api/sessions/{sid}/bin", json={"magnet_diameter_mm": 20}).status_code == 422


def test_basic_auth(monkeypatch):
    import server.main as m
    monkeypatch.setattr(m, "_PASS", "s3cret")
    monkeypatch.setattr(m, "_USER", "davo")
    assert c.get("/api/sessions").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/health").status_code == 200                       # healthcheck sin credenciales
    assert c.get("/api/sessions", auth=("davo", "mala")).status_code == 401
    assert c.get("/api/sessions", auth=("otro", "s3cret")).status_code == 401
    assert c.get("/api/sessions", auth=("davo", "s3cret")).status_code == 200
    assert c.get("/", auth=("davo", "s3cret")).status_code == 200
