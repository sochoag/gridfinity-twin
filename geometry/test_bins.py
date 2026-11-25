"""Genera bins de prueba a partir de out/contours.json y verifica la malla."""
import json
import os
import sys

sys.path.insert(0, "/home/claude/twin")
from geometry.gridfinity import BinConfig, build_bin, export_stl, pockets_to_svg, to_trimesh  # noqa: E402

OUT = "/home/claude/twin/out"
doc = json.load(open(f"{OUT}/contours.json"))["document"]
outer, holes = doc["outer_contours"], doc["hole_contours"]

CASES = {
    "pinzas_3x3": BinConfig(components=[0], height_units=4, cutout_depth_mm=14, stacking_lip=True,
                            magnet_holes=True, finger_notches="one_end"),
    "pinzas_sin_lip": BinConfig(components=[0], height_units=3, cutout_depth_mm=10, stacking_lip=False),
    "cutter_rojo": BinConfig(components=[2], height_units=3, cutout_depth_mm=12, finger_notches="both_ends"),
    "todas": BinConfig(height_units=4, cutout_depth_mm=14),
}

for name, cfg in CASES.items():
    r = build_bin(outer, holes, cfg)
    tm = to_trimesh(r.manifold)
    stl = f"{OUT}/{name}.stl"
    export_stl(r.manifold, stl)
    b = tm.bounds
    print(f"{name:<15} rejilla={r.grid} tam={r.size_mm[0]:.1f}x{r.size_mm[1]:.1f}x{r.total_height_mm:.1f} "
          f"watertight={tm.is_watertight} vol={tm.volume/1000:.1f}cm3 caras={len(tm.faces)} "
          f"bounds_z=({b[0][2]:.2f},{b[1][2]:.2f}) stl={os.path.getsize(stl)//1024}KB")
    for w in r.warnings:
        print("   AVISO:", w)
    if name == "pinzas_3x3":
        pockets_to_svg(r.pockets, f"{OUT}/{name}.svg")
