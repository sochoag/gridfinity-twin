"""Prueba de punta a punta de la interfaz con Chrome real (Playwright).
Uso: python3 tests/e2e_ui.py [carpeta_capturas]"""
import os, subprocess, sys, tempfile, time, urllib.request
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "out", "ui")
os.makedirs(SHOTS, exist_ok=True)
PHOTO = os.path.join(ROOT, "tests", "data", "cuchillas.jpg")
PORT = 8765
env = dict(os.environ, TWIN_DATA=tempfile.mkdtemp(), PYTHONPATH=ROOT)
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--port", str(PORT), "--log-level", "warning"], cwd=ROOT, env=env)
errors, results, failed = [], [], []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("  OK   " if cond else "  FALLO"), name, extra)
try:
    for _ in range(50):
        try: urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health"); break
        except Exception: time.sleep(0.2)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                              args=["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        pg = b.new_page(viewport={"width": 1366, "height": 860})
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("response", lambda r: failed.append((r.status, r.url.replace(f"http://127.0.0.1:{PORT}", ""))) if r.status >= 400 else None)
        pg.goto(f"http://127.0.0.1:{PORT}/")
        check("home visible", pg.is_visible("#view-home") and pg.inner_text("h1").startswith("De foto"))
        pg.screenshot(path=f"{SHOTS}/1_home.png")

        # 1) subir foto
        pg.set_input_files("#file", PHOTO)
        pg.wait_for_selector("#view-corners:not(.hidden)", timeout=20000)
        pg.wait_for_selector(".corner-handle")
        check("4 esquinas arrastrables", pg.locator(".corner-handle").count() == 4)
        check("hoja detectada (sin aviso)", not pg.is_visible("#corner-note"))
        check("A4 seleccionado, 297x210", pg.input_value("#pw") == "297" and pg.input_value("#ph") == "210")
        pg.wait_for_timeout(400); pg.screenshot(path=f"{SHOTS}/2_corners.png")

        # arrastrar una esquina con el raton y comprobar que se mueve
        h = pg.locator(".corner-handle").first
        box = h.bounding_box(); cx0 = float(h.get_attribute("cx"))
        pg.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2); pg.mouse.down()
        pg.mouse.move(box["x"] + box["width"]/2 + 20, box["y"] + box["height"]/2 + 10, steps=5); pg.mouse.up()
        cx1 = float(h.get_attribute("cx"))
        check("arrastrar esquina la mueve", cx1 > cx0 + 5, f"({cx0:.0f} -> {cx1:.0f})")
        # reponer con teclado (flechas) para dejar la esquina donde estaba y probar accesibilidad
        h.focus(); pg.keyboard.press("Shift+ArrowLeft"); pg.keyboard.press("Shift+ArrowLeft")
        check("esquina responde a teclado", float(h.get_attribute("cx")) < cx1)
        pg.evaluate("() => { const S = window.__twin; }")

        # volver a poner las esquinas detectadas con precision antes de trazar
        det = pg.evaluate("async () => (await (await fetch('/api/sessions/' + window.__twin.st.id)).json()).corners")
        pg.evaluate("c => { window.__twin.st.corners = c; }", det)
        # 2) trazar
        pg.click("#btn-trace")
        pg.wait_for_selector("#view-shape:not(.hidden) .comp", timeout=30000)
        n = pg.locator("#shape-svg .comp").count()
        check("4 objetos detectados", n == 4, f"(n={n})")
        check("lista de objetos", pg.locator("#comp-list li").count() == 4)
        pg.wait_for_timeout(600); pg.screenshot(path=f"{SHOTS}/3_shape.png")
        # alternar el objeto 1 (clic en lista) y volver a activarlo
        pg.locator("#comp-list li").first.click()
        check("excluir objeto", pg.locator("#shape-svg .comp.off").count() == 1)
        pg.locator("#shape-svg .comp").first.click(force=True)
        check("incluir objeto (clic en el SVG)", pg.locator("#shape-svg .comp.off").count() == 0)
        # dejar solo las pinzas (3er objeto)
        for i in (0, 1, 3): pg.locator("#comp-list li").nth(i).click()
        check("solo 1 objeto seleccionado", pg.locator("#shape-svg .comp.off").count() == 3)

        # 3) bin
        pg.click("#btn-to-bin")
        pg.wait_for_selector("#view-bin:not(.hidden)")
        pg.wait_for_selector("#bin-info span", timeout=60000)
        info = pg.inner_text("#bin-info")
        check("info del bin con malla cerrada", "cerrada" in info, info.replace("\n", " | "))
        pg.wait_for_timeout(1500)
        px = pg.evaluate("""() => { const c = document.querySelector('#viewer canvas'); if (!c) return null;
            const t = document.createElement('canvas'); t.width = c.width; t.height = c.height; return null; }""")
        check("canvas WebGL presente", pg.locator("#viewer canvas").count() == 1)
        pg.screenshot(path=f"{SHOTS}/4_bin.png")
        # el modelo debe aparecer: comparar el viewer con y sin pieza mediante captura
        shot = pg.locator("#viewer").screenshot()
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(shot)).convert("RGB"); w, hh = im.size
        orange = sum(1 for x in range(0, w, 4) for y in range(0, hh, 4) if (lambda r, g, b: r > 150 and 60 < g < 140 and b < 90)(*im.getpixel((x, y))))
        check("el modelo naranja se ve en el visor", orange > 300, f"(pixeles naranja muestreados: {orange})")

        # cambiar parametros: imanes + muescas + 5 unidades de alto -> regenerar
        pg.check("#b-mag"); pg.select_option("#b-finger", "one_end"); pg.fill("#b-hu", "5")
        pg.fill("#b-depth", "20")
        with pg.expect_response(lambda r: r.url.endswith("/bin") and r.request.method == "POST") as ri:
            pg.click("#btn-build")
        check("regenerar con otros parametros (HTTP 200)", ri.value.status == 200)
        pg.wait_for_function("document.querySelector('#bin-info').innerText.includes('40.4') || document.querySelector('#bin-info').innerText.includes('39.4')", timeout=60000) if False else pg.wait_for_timeout(1500)
        info2 = pg.inner_text("#bin-info")
        check("alto = 5*7 + 4.4 = 39.4 mm", "39.4" in info2, info2.replace("\n", " | "))
        # descargas
        with pg.expect_download() as d1: pg.click("#dl-stl")
        f1 = d1.value; path = f1.path()
        data = open(path, "rb").read()
        check("descarga STL binario", len(data) > 50000 and not data[:5] == b"solid", f"({len(data)//1024} KB, {f1.suggested_filename})")
        with pg.expect_download() as d2: pg.click("#dl-svg")
        check("descarga SVG", open(d2.value.path()).read().startswith("<svg"), d2.value.suggested_filename)
        # validacion: parametro fuera de rango -> mensaje de error visible
        pg.fill("#b-md", "30"); pg.check("#b-mag"); pg.click("#btn-build")
        pg.wait_for_selector("#toast.err", timeout=10000)
        check("error de validacion se muestra", "magnet_diameter_mm" in pg.inner_text("#toast"), pg.inner_text("#toast"))
        pg.fill("#b-md", "6.5")

        # 4) assets
        pg.click(".step[data-step=home]")
        pg.wait_for_selector(".asset")
        check("asset guardado en 'Mis assets'", pg.locator(".asset").count() == 1)
        check("asset muestra estado", "bin listo" in pg.inner_text(".asset .sub"), pg.inner_text(".asset .sub"))
        pg.locator(".asset .name").fill("Pinzas pequenas"); pg.locator(".asset .name").press("Tab")
        pg.wait_for_timeout(400)
        pg.screenshot(path=f"{SHOTS}/5_assets.png")
        pg.reload(); pg.wait_for_selector(".asset")
        check("renombrar persiste tras recargar", pg.input_value(".asset .name") == "Pinzas pequenas")
        pg.click(".asset .btn.primary")
        pg.wait_for_selector("#view-bin:not(.hidden)", timeout=20000); pg.wait_for_selector("#bin-info span")
        check("reabrir asset lleva al bin con parametros guardados", pg.input_value("#b-hu") == "5" and pg.is_checked("#b-mag"))
        pg.wait_for_timeout(1200); pg.screenshot(path=f"{SHOTS}/6_reopened.png")
        # eliminar (dos clics)
        pg.click(".step[data-step=home]"); pg.wait_for_selector(".asset")
        pg.click(".asset .btn.danger"); pg.click(".asset .btn.danger")
        pg.wait_for_function("!document.querySelector('.asset')", timeout=5000)
        check("eliminar asset", pg.locator(".asset").count() == 0)

        # movil
        pg.set_viewport_size({"width": 390, "height": 800}); pg.wait_for_timeout(300)
        check("sin desbordamiento horizontal en movil", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
        pg.screenshot(path=f"{SHOTS}/7_mobile.png")
        b.close()
finally:
    srv.terminate(); srv.wait(timeout=10)
print("respuestas >=400:", failed)
# el unico 422 esperado es el de la prueba deliberada de validacion
bad = [e for e in errors if "422" not in e]
check("sin errores de consola/JS", not bad, str(bad[:3]))
fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} comprobaciones OK" + (f" — FALLARON: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
