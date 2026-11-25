"""Escritor minimo de 3MF multi-objeto (unidades mm, un color por objeto) a partir de mallas de trimesh."""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

import numpy as np

_CT = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
         'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')


def write_3mf(objects: list[tuple[str, np.ndarray, np.ndarray, str]], gap: float = 10.0) -> bytes:
    """objects: (nombre, vertices Nx3, caras Mx3, color '#RRGGBB'). Los objetos se colocan en fila (eje X)
    sobre la cama, con `gap` mm entre ellos, cada uno con su minimo en z = 0."""
    mats, res, items = [], [], []
    x = 0.0
    for k, (name, v, f, color) in enumerate(objects):
        v = np.asarray(v, float)
        mn, mx = v.min(0), v.max(0)
        dx, dy, dz = x - mn[0], -(mn[1] + mx[1]) / 2, -mn[2]
        v = v + (dx, dy, dz)
        x += (mx[0] - mn[0]) + gap
        mats.append(f'<base name="{escape(name)}" displaycolor="{color.upper()}FF"/>')
        vs = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in v)
        ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in np.asarray(f, int))
        oid = k + 2
        res.append(f'<object id="{oid}" type="model" name="{escape(name)}" pid="1" pindex="{k}">'
                   f'<mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><basematerials id="1">{"".join(mats)}</basematerials>{"".join(res)}</resources>'
             f'<build>{"".join(items)}</build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()
