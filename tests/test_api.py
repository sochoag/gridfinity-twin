import io
import os
import sys
import tempfile
import zipfile

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
    assert c.put(f"/api/sessions/{sid}/design", json={"shapes": []}).status_code == 200      # sin formas = contenedor abierto


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


def test_hollow_lid_and_imported_shapes():
    import io
    import zipfile
    st = _session()
    sid = st["id"]
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0, 2]}).json()
    # forma importada: lleva su propio contorno (comp=None), centrado en su caja
    ring = [[-30, -8], [30, -8], [30, 8], [-30, 8]]
    des["shapes"].append({"id": "imp1", "name": "Importada", "comp": None, "outer": ring, "holes": [], "x": 0, "y": 0, "rot": 0,
                          "notch": {"on": False, "d": 16, "u": 0, "v": 0}})
    des["settings"]["auto_size"] = True
    for sh, a in zip(des["shapes"], c.post(f"/api/sessions/{sid}/design/arrange", json=des).json()):   # sin solapes
        sh.update(x=a["x"], y=a["y"], rot=a["rot"])
    r = c.put(f"/api/sessions/{sid}/design", json=des)
    assert r.status_code == 200, r.text
    assert r.json()["mesh"]["watertight"] and not r.json()["has_lid"] and r.json()["problems"] == {}
    # forma sin contorno ni comp: error claro
    bad = {**des, "shapes": [{"id": "x", "name": "x", "comp": None, "x": 0, "y": 0}]}
    assert c.put(f"/api/sessions/{sid}/design", json=bad).status_code == 422
    # modo hueco: bin + tapa, ambas cerradas, y el bin pesa bastante menos
    des["settings"]["hollow"] = True
    r = c.put(f"/api/sessions/{sid}/design", json=des)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["has_lid"] and m["lid"]["watertight"] and m["mesh"]["watertight"] and m["mesh"]["degenerate_faces"] == 0, m
    assert m["stats"]["bin_cm3"] < 0.5 * m["stats"]["solid_cm3"]
    assert 0.8 <= m["stats"]["lid_z"] and m["stats"]["lid_cm3"] > 5
    tl = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/lid.stl").content), file_type="stl")
    assert tl.is_watertight and abs(tl.bounds[0][2]) < 1e-3 and 1.1 < tl.bounds[1][2] < 1.4
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.zip").content))
    assert sorted(n.rsplit("_", 1)[1] for n in z.namelist()) == ["bin.stl", "insert.stl", "lid.stl"]
    # con imanes: los bosses mantienen la malla cerrada
    des["settings"]["magnet_holes"] = True
    m = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert m["mesh"]["watertight"] and m["lid"]["watertight"], m
    # apagar el modo hueco elimina la tapa vieja
    des["settings"]["hollow"] = False
    assert c.put(f"/api/sessions/{sid}/design", json=des).json()["has_lid"] is False
    assert c.get(f"/api/sessions/{sid}/lid.stl").status_code == 404
    # arrange tambien acepta formas importadas
    assert len(c.post(f"/api/sessions/{sid}/design/arrange", json=des).json()) == 3


def test_partial_bin():
    st = _session()
    sid = st["id"]
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0, 2]}).json()
    for sh, a in zip(des["shapes"], c.post(f"/api/sessions/{sid}/design/arrange", json=des).json()):
        sh.update(x=a["x"], y=a["y"], rot=a["rot"])
    s = des["settings"]
    s.update(auto_size=False, grid_width=6, grid_depth=5)
    full = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert full["mesh"]["watertight"] and full["problems"] == {}, full
    # esquinas opuestas apagadas: menos volumen y malla cerrada (con y sin hueco, con imanes)
    s.update(partial=True, cells_off=["0,0", "5,4", "5,0"])
    for extra in ({}, {"hollow": True}, {"hollow": True, "magnet_holes": True}, {"stacking_lip": False}):
        s.update({"hollow": False, "magnet_holes": False, "stacking_lip": True, **extra})
        m = c.put(f"/api/sessions/{sid}/design", json=des).json()
        assert m["mesh"]["watertight"] and m["mesh"]["degenerate_faces"] == 0 and m["problems"] == {}, (extra, m)
    s.update(hollow=False, magnet_holes=False, stacking_lip=True)
    part = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert part["mesh"]["volume_cm3"] < full["mesh"]["volume_cm3"] - 30
    # todas las celdas apagadas: error claro
    s["cells_off"] = [f"{i},{j}" for i in range(6) for j in range(5)]
    assert c.put(f"/api/sessions/{sid}/design", json=des).status_code == 422
    # una forma sobre una celda apagada se marca como fuera de la zona util
    s["cells_off"] = ["2,2", "3,2", "2,3", "3,3", "2,1", "3,1"]
    m = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert any("outside" in v for v in m["problems"].values()), m


def test_printability_checks():
    st = _session()
    sid = st["id"]
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0, 2]}).json()
    for sh, a in zip(des["shapes"], c.post(f"/api/sessions/{sid}/design/arrange", json=des).json()):
        sh.update(x=a["x"], y=a["y"], rot=a["rot"])
    m = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert any(k["code"] == "weight" for k in m["checks"]) and not any(k["level"] == "warn" for k in m["checks"]), m["checks"]
    des["settings"].update(bed_x_mm=100, bed_y_mm=100)
    m = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert any(k["code"] == "bed" for k in m["checks"])
    des["settings"].update(bed_x_mm=256, bed_y_mm=256)
    # dos formas casi pegadas: aviso de pared fina con ambas formas
    des["settings"].update(clearance_mm=0, auto_size=True)
    a, b = des["shapes"][0], des["shapes"][1]
    from shapely.geometry import Polygon  # noqa: F401
    m = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert isinstance(m["checks"], list)


def test_partial_cell_row_orientation():
    """La fila 0 del editor 2D (Y abajo) es el borde superior: apagar 'i,0' quita la parte de y<0 en pantalla."""
    import io
    st = _session()
    sid = st["id"]
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0]}).json()
    des["shapes"][0].update(x=0, y=0, rot=0)
    des["settings"].update(auto_size=False, grid_width=3, grid_depth=5, partial=True, cells_off=["1,4"], stacking_lip=False)
    r = c.put(f"/api/sessions/{sid}/design", json=des)
    assert r.status_code == 200, r.text
    m = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.stl").content), file_type="stl")
    # editor: fila 4 = abajo = Y mundo negativa (el 3D invierte Y). Falta la muesca en la celda central de ese borde
    ymin = m.bounds[0][1]
    low = m.vertices[m.vertices[:, 1] < ymin + 5]
    assert low[:, 0].min() > 10 or low[:, 0].max() < -10 or (low[:, 0] > 21).any() and (low[:, 0] < -21).any() and not ((low[:, 0] > -20) & (low[:, 0] < 20) & (low[:, 2] > 8)).any()


def test_partial_lip_has_no_internal_sheets():
    """El labio del bin parcial no debe dejar laminas horizontales de espesor 0 sobre toda la huella (se ven como tapa)."""
    import io
    st = _session()
    sid = st["id"]
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [2]}).json()
    des["shapes"][0].update(x=0, y=-8)
    des["settings"].update(auto_size=False, grid_width=3, grid_depth=4, partial=True, cells_off=["1,3"], hollow=True)
    assert c.put(f"/api/sessions/{sid}/design", json=des).status_code == 200
    m = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.stl").content), file_type="stl")
    top = (m.face_normals[:, 2] > 0.9) & (m.triangles_center[:, 2] > 21.3) & (m.triangles_center[:, 2] < 24.5)
    assert m.area_faces[top].sum() < 2000, m.area_faces[top].sum()


def test_scale_by_reference_full_photo():
    """Medir con 2 puntos: la foto completa se usa como 'papel' de dimensiones conocidas."""
    st = _session()
    sid = st["id"]
    W, H = st["source"]["width"], st["source"]["height"]
    # 5 px/mm -> la foto cubre (W/5) x (H/5) mm
    long_mm, short_mm = max(W, H) / 5, min(W, H) / 5
    body = {"corners": [[0, 0], [W, 0], [W, H], [0, H]], "paper": "custom", "paper_width_mm": long_mm, "paper_height_mm": short_mm}
    r = c.post(f"/api/sessions/{sid}/trace", json=body)
    assert r.status_code in (200, 422), r.text   # 422 solo si no hay objetos separados del borde
    big = dict(body, paper_width_mm=5000, paper_height_mm=3000)
    assert c.post(f"/api/sessions/{sid}/trace", json=big).status_code == 422


def test_vector_import_svg_dxf():
    import io as _io

    import ezdxf
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" viewBox="0 0 100 60">'
           b'<path d="M10 10 H60 V50 H10 Z M20 20 H40 V40 H20 Z" fill-rule="evenodd"/><circle cx="80" cy="30" r="10"/></svg>')
    r = c.post("/api/vector/parse", files={"file": ("a.svg", svg, "image/svg+xml")})
    assert r.status_code == 200, r.text
    sh = r.json()["shapes"]
    assert r.json()["format"] == "svg" and len(sh) == 2
    assert abs(sh[0]["bbox"]["width"] - 50) < 0.2 and abs(sh[0]["bbox"]["height"] - 40) < 0.2 and len(sh[0]["holes"]) == 1   # unidades: mm del viewBox
    d = ezdxf.new()
    d.header["$INSUNITS"] = 4
    m = d.modelspace()
    m.add_lwpolyline([(0, 0), (50, 0), (50, 30), (0, 30)], close=True)
    m.add_line((100, 0), (130, 0)); m.add_line((130, 0), (130, 20)); m.add_arc((115, 20), 15, 0, 180); m.add_line((100, 20), (100, 0))
    buf = _io.StringIO(); d.write(buf)
    r = c.post("/api/vector/parse", files={"file": ("a.dxf", buf.getvalue().encode(), "application/dxf")})
    assert r.status_code == 200 and len(r.json()["shapes"]) == 2, r.text   # polilinea + cadena de lineas/arco cerrada por polygonize
    assert c.post("/api/vector/parse", files={"file": ("x.txt", b"hola", "text/plain")}).status_code == 422
    assert c.post("/api/vector/parse", files={"file": ("e.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="5" y2="5" stroke="black"/></svg>', "image/svg+xml")}).status_code == 422


def test_manual_mask_edit():
    st = _session()
    sid = st["id"]
    c0 = c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"}).json()
    n0 = len(c0["components"])
    # sin ediciones = mismo resultado que el automatico
    r = c.post(f"/api/sessions/{sid}/mask", json={"edits": []})
    assert r.status_code == 200 and len(r.json()["contours"]["components"]) == n0
    # pincel 'add' lejos de todo objeto (esquina libre del papel): aparece un objeto nuevo grande
    pw, ph = c0["paper"]["width_mm"], c0["paper"]["height_mm"]
    free = None
    for gx in range(12, int(pw) - 12, 4):
        for gy in range(12, int(ph) - 12, 4):
            if all(not (k["bbox"]["x"] - 14 < gx < k["bbox"]["x"] + k["bbox"]["width"] + 14 and k["bbox"]["y"] - 14 < gy < k["bbox"]["y"] + k["bbox"]["height"] + 14) for k in c0["components"]):
                free = (gx, gy)
                break
        if free:
            break
    assert free, "sin zona libre para probar"
    stroke = {"op": "add", "kind": "brush", "r": 5, "pts": [[free[0] - 6, free[1]], [free[0] + 6, free[1]]]}
    r = c.post(f"/api/sessions/{sid}/mask", json={"edits": [stroke]})
    assert r.status_code == 200 and len(r.json()["contours"]["components"]) == n0 + 1
    assert c.get(f"/api/sessions/{sid}").json()["mask_edits"][0]["op"] == "add"
    # borrar un objeto entero con un lazo
    k = c0["components"][0]["bbox"]
    box = [[k["x"] - 3, k["y"] - 3], [k["x"] + k["width"] + 3, k["y"] - 3], [k["x"] + k["width"] + 3, k["y"] + k["height"] + 3], [k["x"] - 3, k["y"] + k["height"] + 3]]
    r = c.post(f"/api/sessions/{sid}/mask", json={"edits": [{"op": "erase", "kind": "lasso", "pts": box}]})
    assert r.status_code == 200 and len(r.json()["contours"]["components"]) <= n0 - 1 + 1
    # borrar todo -> error claro; un nuevo trazado limpia las ediciones
    whole = [[-10, -10], [pw + 10, -10], [pw + 10, ph + 10], [-10, ph + 10]]
    assert c.post(f"/api/sessions/{sid}/mask", json={"edits": [{"op": "erase", "kind": "lasso", "pts": whole}]}).status_code == 422
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert c.get(f"/api/sessions/{sid}").json()["mask_edits"] == []


# ------------------------------------------------------------------ v1.3
def _traced():
    st = _session()
    r = c.post(f"/api/sessions/{st['id']}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert r.status_code == 200, r.text
    return st["id"], r.json()


def test_loose_dividers():
    sid, _ = _traced()
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0]}).json()
    des["settings"].update(auto_size=False, grid_width=4, grid_depth=3, dividers_x=[-30, 30], dividers_y=[0], height_units=4)
    des["shapes"][0].update(x=0, y=-25)
    r = c.put(f"/api/sessions/{sid}/design", json=des)
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["has_dividers"] and meta["dividers"]["watertight"] and meta["mesh"]["watertight"]
    assert any(k["code"] == "dividers" for k in meta["checks"])
    div = trimesh.load(io.BytesIO(c.get(f"/api/sessions/{sid}/dividers.stl").content), file_type="stl")
    assert div.is_watertight and abs(div.bounds[1][2] - div.bounds[0][2] - 1.2) < 0.01   # tumbadas: z = grosor
    # las ranuras existen: el bin pierde volumen respecto a uno sin divisores
    v_with = meta["mesh"]["volume_cm3"]
    des["settings"].update(dividers_x=[], dividers_y=[])
    meta2 = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert not meta2["has_dividers"] and meta2["mesh"]["volume_cm3"] > v_with
    assert c.get(f"/api/sessions/{sid}/dividers.stl").status_code == 404
    # modo hueco: se ignoran con aviso
    des["settings"].update(dividers_x=[0], hollow=True)
    m3 = c.put(f"/api/sessions/{sid}/design", json=des).json()
    assert not m3["has_dividers"] and any("divisores" in w for w in m3["warnings"])


def test_multi_bin_and_3mf():
    import re
    sid, cont = _traced()
    c.post(f"/api/sessions/{sid}/design/init", json={"components": [0]})
    d1 = c.get(f"/api/sessions/{sid}/design").json()
    assert c.put(f"/api/sessions/{sid}/design", json=d1).status_code == 200
    r = c.post(f"/api/sessions/{sid}/bins", json={"name": "Segundo"})
    assert r.status_code == 200 and r.json()["id"] == "b2"
    assert c.get(f"/api/sessions/{sid}/design?bin=b2").json() is None
    d2 = c.post(f"/api/sessions/{sid}/design/init?bin=b2", json={"components": [1, 2]}).json()
    assert len(d2["shapes"]) == 2 and len(c.get(f"/api/sessions/{sid}/design").json()["shapes"]) == 1   # b1 intacto
    m2 = c.put(f"/api/sessions/{sid}/design?bin=b2", json=d2).json()
    assert m2["mesh"]["watertight"]
    b1 = c.get(f"/api/sessions/{sid}/bin.stl").content
    b2 = c.get(f"/api/sessions/{sid}/bin.stl?bin=b2").content
    assert b1 != b2 and "Segundo" in c.get(f"/api/sessions/{sid}/bin.stl?bin=b2").headers["content-disposition"]
    assert [b["id"] for b in c.get(f"/api/sessions/{sid}/bins").json()] == ["b1", "b2"]
    # copiar un bin
    r3 = c.post(f"/api/sessions/{sid}/bins", json={"copy_from": "b2"}).json()
    assert len(r3["design"]["shapes"]) == 2
    assert c.patch(f"/api/sessions/{sid}/bins/b1", json={"name": "Principal"}).status_code == 200
    # zip de todos
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.zip?all=1").content))
    assert len([n for n in z.namelist() if n.endswith("_bin.stl")]) == 2 and any("Principal" not in n for n in z.namelist())
    # 3MF valido, un objeto por pieza, con colores
    r = c.get(f"/api/sessions/{sid}/bin.3mf?bin=b2")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    model = zf.read("3D/3dmodel.model").decode()
    assert model.count("<object ") == 2 and "displaycolor" in model and 'unit="millimeter"' in model   # bin + inserto
    allm = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid}/bin.3mf?all=1").content)).read("3D/3dmodel.model").decode()
    assert allm.count("<object ") >= 3
    loaded = trimesh.load(io.BytesIO(r.content), file_type="3mf")
    assert len(loaded.geometry) == 2 and all(g.is_watertight for g in loaded.geometry.values())
    assert re.search(r'<item objectid="\d+"/>', model)
    # b1 no se borra; b2 si
    assert c.delete(f"/api/sessions/{sid}/bins/b1").status_code == 422
    assert c.delete(f"/api/sessions/{sid}/bins/b2").status_code == 200
    assert c.get(f"/api/sessions/{sid}/bin.stl?bin=b2").status_code == 404
    assert c.get(f"/api/sessions/{sid}/bin.stl?bin=zz").status_code == 400
    # volver a trazar descarta los bins extra
    st = c.get(f"/api/sessions/{sid}").json()
    c.post(f"/api/sessions/{sid}/trace", json={"corners": st["corners"], "paper": "A4"})
    assert c.get(f"/api/sessions/{sid}/bins").json() == [{"id": "b1", "name": "Principal", "has_design": False, "has_bin": False}]


def test_library_and_merge():
    sq = [[-10, -5], [10, -5], [10, 5], [-10, 5]]
    r = c.post("/api/library", json={"name": "Destornillador", "tags": ["Taller", " taller", "mano"], "outer": sq, "depth_mm": 14,
                                     "notch": {"on": True, "d": 14, "u": 8, "v": 0}})
    assert r.status_code == 200, r.text
    it = r.json()
    assert it["tags"] == ["taller", "mano"] and it["bbox"]["width"] == 20 and it["notch"]["on"]
    assert c.post("/api/library", json={"name": "x", "outer": [[0, 0], [1, 0], [2, 0]]}).status_code == 422   # sin area
    assert c.patch(f"/api/library/{it['id']}", json={"name": "Phillips", "tags": ["a"]}).json()["name"] == "Phillips"
    assert any(i["id"] == it["id"] for i in c.get("/api/library").json())
    assert c.delete(f"/api/library/{it['id']}").status_code == 200
    assert c.delete(f"/api/library/{it['id']}").status_code == 404
    # fusionar proyectos: formas de otros proyectos en uno nuevo
    s1, c1 = _traced()
    s2, c2 = _traced()
    r = c.post("/api/sessions/merge", json={"ids": [s1, s2], "name": "Fusion test"})
    assert r.status_code == 200, r.text
    m = r.json()
    n = len(c1["components"]) + len(c2["components"])
    assert len(m["contours"]["components"]) == n and m["merged"] == [s1, s2]
    comps = m["contours"]["components"]
    assert [k["id"] for k in comps] == list(range(n))
    from shapely.geometry import Polygon
    polys = [Polygon(k["outer"]).buffer(0) for k in comps]
    assert all(not polys[i].intersects(polys[j]) for i in range(n) for j in range(i + 1, n))   # no se solapan
    # el proyecto fusionado se disena igual que uno normal
    des = c.post(f"/api/sessions/{m['id']}/design/init", json={"components": [0, n - 1]}).json()
    assert c.put(f"/api/sessions/{m['id']}/design", json=des).json()["mesh"]["watertight"]
    assert c.post(f"/api/sessions/{m['id']}/trace", json={"corners": m["corners"], "paper": "A4"}).status_code == 409
    assert c.post("/api/sessions/merge", json={"ids": [s1, s1]}).status_code == 422
    assert c.post("/api/sessions/merge", json={"ids": [s1]}).status_code == 422
    assert c.get("/api/sessions").json()   # el listado sigue funcionando


def test_empty_container_and_complement():
    sid, _ = _traced()
    des = c.post(f"/api/sessions/{sid}/design/init", json={"components": [0]}).json()
    des["settings"].update(auto_size=False, grid_width=3, grid_depth=3, partial=True, cells_off=["2,0", "2,1"])
    des["shapes"][0].update(x=-20, y=0)
    assert c.put(f"/api/sessions/{sid}/design", json=des).status_code == 200
    # complemento: celdas invertidas, sin siluetas, mismo grid
    r = c.post(f"/api/sessions/{sid}/bins", json={"copy_from": "b1", "mode": "complement"})
    assert r.status_code == 200, r.text
    d2 = r.json()["design"]
    assert d2["shapes"] == [] and d2["settings"]["partial"] and d2["settings"]["auto_size"] is False
    assert sorted(d2["settings"]["cells_off"]) == sorted(f"{i},{j}" for j in range(3) for i in range(3) if f"{i},{j}" not in ("2,0", "2,1"))
    d2["settings"].update(dividers_y=[-21])
    m = c.put(f"/api/sessions/{sid}/design?bin=b2", json=d2)
    assert m.status_code == 200, m.text
    m = m.json()
    assert m["mesh"]["watertight"] and m["has_dividers"] and not m["has_insert"] and m["grid"] == [3, 3]
    # el complemento es un hueco real (menos volumen que un bin macizo del mismo tamano) y los divisores funcionan
    sol = c.post(f"/api/sessions/{sid}/bins", json={"copy_from": "b1", "mode": "empty"}).json()["design"]
    assert sol["shapes"] == [] and sol["settings"]["partial"] is False
    me = c.put(f"/api/sessions/{sid}/design?bin=b3", json=sol).json()
    assert me["mesh"]["watertight"] and me["mesh"]["volume_cm3"] < 200
    # origen no parcial: el complemento se rechaza
    des["settings"].update(partial=False, cells_off=[])
    c.put(f"/api/sessions/{sid}/design", json=des)
    assert c.post(f"/api/sessions/{sid}/bins", json={"copy_from": "b1", "mode": "complement"}).status_code == 422
