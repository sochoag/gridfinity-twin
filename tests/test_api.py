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
    # diseno: init -> editar -> construir
    r = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0, 2]})
    assert r.status_code == 200, r.text
    des = r.json()
    assert [s["comp"] for s in des["shapes"]] == [0, 2] and des["settings"]["auto_size"] is True
    des["settings"].update(height_units=4, pocket_depth_mm=14, magnet_holes=True)
    des["shapes"][0]["notch"]["on"] = True
    des["shapes"][1]["depth_mm"] = 9
    r = c.put(f"/api/sessions/{sid}/design", json=des)
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["problems"] == {} and meta["warnings"] == []
    assert meta["mesh"]["watertight"] and meta["mesh"]["degenerate_faces"] == 0
    assert meta["has_insert"] and meta["insert"]["watertight"]
    import io
    import zipfile
    tm = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.stl").content), file_type="stl")
    assert tm.is_watertight and abs(tm.bounds[1][2] - 32.4) < 0.05
    ti = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/insert.stl").content), file_type="stl")
    assert ti.is_watertight and ti.volume > 1000   # dos profundidades -> dos alturas de inserto
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.zip").content))
    assert sorted(n.rsplit("_", 1)[1] for n in z.namelist()) == ["bin.stl", "insert.stl"]
    assert c.get(f"/api/sessions/{sid}/bin.svg").text.startswith("<svg")
    assert c.get(f"/api/sessions/{sid}/design").json()["shapes"][1]["depth_mm"] == 9

    # mover una forma fuera del bin fijo y solapar: debe avisarse
    des["settings"].update(auto_size=False, grid_width=2, grid_depth=2)
    des["shapes"][1].update(x=des["shapes"][0]["x"], y=des["shapes"][0]["y"])
    m2 = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert "overlap" in m2["problems"]["s1"] and "outside" in m2["problems"]["s2"] and m2["warnings"]
    # sin inserto no debe quedar el STL viejo
    des["settings"]["contrast_insert"] = False
    assert c.put(f"/api/sessions/{sid}/design", json=des).json()["has_insert"] is False
    assert c.get(f"/api/sessions/{sid}/insert.stl").status_code == 404
    # auto-ordenar
    ar = c.post(f"/api/sessions/{sid}/design/arrange", json=des).json()
    assert [a["id"] for a in ar] == ["s1", "s2"]

    # biblioteca
    lst = c.get("/api/sessions").json()
    assert lst[0]["id"] == sid and lst[0]["traced"] and lst[0]["has_bin"] and lst[0]["n_components"] == 4
    assert c.patch(f"/api/sessions/{sid}", json={"name": "Pinzas"}).json()["name"] == "Pinzas"
    assert c.delete(f"/api/sessions/{sid}").status_code == 200
    assert c.get(f"/api/sessions/{sid}").status_code == 404


def test_validation():
    st = _session()
    sid = st["id"]
    assert c.post(f"/api/sessions/{sid}/design/init", json={}).status_code == 409   # sin trazado
    assert c.get("/api/sessions/../../etc/passwd/source.jpg").status_code in (400, 404)
    assert c.get("/api/sessions/xyz/source.jpg").status_code == 400
    bad = c.post(f"/api/sessions/{sid}/trace", json={"corners": [[0, 0], [1, 0], [1, 1], [0, 1]], "paper": "A4"})
    assert bad.status_code == 422
    assert c.post("/api/sessions", files={"file": ("x.txt", b"no es una imagen", "text/plain")}).status_code == 415
    ok = c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert ok.status_code == 200
    assert c.post(f"/api/sessions/{sid}/design/init", json={"components": [9]}).status_code == 422
    sh = {"id": "a", "comp": 0}
    assert c.put(f"/api/sessions/{sid}/design", json={"settings": {"magnet_diameter_mm": 20}, "shapes": [sh]}).status_code == 422
    assert c.put(f"/api/sessions/{sid}/design", json={"shapes": [sh, sh]}).status_code == 422       # ids repetidos
    assert c.put(f"/api/sessions/{sid}/design", json={"shapes": [{"id": "a", "comp": 99}]}).status_code == 422
    assert c.put(f"/api/sessions/{sid}/design", json={"shapes": []}).status_code == 422


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
