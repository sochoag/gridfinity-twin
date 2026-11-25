# Gridfinity Twin

Sube la foto de una herramienta sobre una hoja de papel y obtén un bin Gridfinity con la silueta recortada
(STL listo para el slicer + SVG del contorno). Todo corre en tu máquina.

## Uso

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./run.sh                    # http://127.0.0.1:8000
```

Flujo: **Foto → Papel** (ajusta las 4 esquinas si hace falta) **→ Silueta** (elige qué objetos entran)
**→ Bin** (editor como ShapeAny: formas arrastrables/rotables, muescas, bolsillo por forma, edición de contorno, chaflán, inserto de contraste, vista 3D y ZIP de STLs). Todo queda guardado en `data/` y reaparece en *Mis assets*.

> Sin autenticación: déjalo en `127.0.0.1`. Si lo expones en una red, ponle un proxy con login delante.

## Cómo tomar la foto
Cámara lo más vertical posible, papel completo y plano, buena luz sin sombras duras, un objeto por foto
(varios funcionan pero la silueta pierde precisión). Papel blanco o claro sobre una superficie de otro color.

## Estructura

| Carpeta | Qué hace |
|---|---|
| `vision/pipeline.py` | Detecta el papel (croma + ajuste de rectas), rectifica a mm con homografía, segmenta (distancia de color al papel + bordes débiles) y extrae contornos simplificados |
| `geometry/gridfinity.py` | Pie con perfil estándar, cuerpo, cutout con holgura (offset de polígonos), imanes, stacking lip, muescas; exporta STL/SVG y valida la malla |
| `server/main.py` | API FastAPI y sesiones en disco (`data/<id>/`) |
| `server/static/` | Interfaz sin build (JS nativo + three.js incluido en `vendor/`, funciona sin internet) |
| `tests/` | `test_api.py` (pytest) y `e2e_ui.py` (Chrome real con Playwright) |
| `vision/validate.py`, `ref/` | Herramienta de desarrollo: compara con los contornos de referencia de la foto de prueba |

## API (resumen)
`POST /api/sessions` (foto) · `POST /api/sessions/{id}/trace` · `POST /api/sessions/{id}/design/init|arrange` · `GET|PUT /api/sessions/{id}/design` ·
`GET /api/sessions/{id}/bin.stl|insert.stl|bin.zip|bin.svg` · `GET|PATCH|DELETE /api/sessions[/{id}]`. Documentación interactiva en `/docs`.

## Pruebas
```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/test_api.py          # API con la foto de ejemplo
python3 tests/e2e_ui.py                      # interfaz completa en Chrome headless (capturas en out/ui)
```

## Valores de Gridfinity usados
Paso 42 mm · bin = 42·n − 0.5 mm · altura 7 mm/unidad · pie 0.8 + 1.8 + 2.15 mm (4.75 mm) · esquina R 3.75 ·
stacking lip 4.4 mm (pared base 2.6 mm) · imanes Ø6.5 × 2.4 mm a (paso/2 − 8) mm del centro de cada celda.
Son los valores habituales de la comunidad, no una especificación oficial: haz una **impresión de prueba** de un bin
pequeño antes de imprimir uno grande, y ajusta la holgura según tu impresora.

Notas de ingeniería:
- El cuerpo es 0.002 mm más pequeño que el borde superior del pie y el labio termina con 0.2 mm de plano. Es a propósito:
  con bordes exactamente coincidentes o un filo de 0 mm, el kernel de mallas deja triángulos degenerados que un slicer ve como
  malla no cerrada. Cada STL se relee como lo haría un slicer y se valida (`mesh.watertight` en la respuesta).
- Límite del trazado: umbrales calibrados con fotos de papel claro sobre madera. En fondos muy claros o papeles de color
  puede hacer falta ajustar `detect_paper` / `segment` en `vision/pipeline.py`.

## Desplegar en un VPS (Docker + Caddy)

Requisitos: un VPS con Docker y el plugin Compose (en Ubuntu: `curl -fsSL https://get.docker.com | sh`), 1 GB de RAM como mínimo.

```bash
# desde tu máquina
scp gridfinity-twin.zip usuario@IP_DEL_VPS:~
# en el VPS
unzip gridfinity-twin.zip -d gridfinity-twin && cd gridfinity-twin
cp .env.example .env && nano .env     # define TWIN_PASSWORD (obligatoria) y, si tienes dominio, DOMAIN
docker compose up -d --build
docker compose ps                      # app y caddy deben quedar "running/healthy"
```

- **Con dominio** (`DOMAIN=twin.tudominio.com`, registro DNS tipo A hacia la IP del VPS): Caddy obtiene y renueva el certificado HTTPS solo.
- **Sin dominio** (`DOMAIN` vacío): queda en `http://IP_DEL_VPS` sin cifrar, y la contraseña viajaría en claro. Úsalo solo para probar.
- La app pide usuario y contraseña (`TWIN_USER`/`TWIN_PASSWORD`, HTTP Basic). Sin `TWIN_PASSWORD` la app quedaría abierta: el compose no arranca si falta.
- Datos persistentes en el volumen `twin-data`. Respaldo: `docker run --rm -v gridfinity-twin_twin-data:/d -v $PWD:/b alpine tar czf /b/twin-data.tgz -C /d .`
- Actualizar: sube el código nuevo y `docker compose up -d --build`. Logs: `docker compose logs -f app`.
- Abre solo los puertos 80 y 443 en el firewall del VPS.
