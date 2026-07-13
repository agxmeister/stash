#!/usr/bin/env python3
"""
Check an STL for walls thinner than an FDM printer can reliably lay down.

A wall thinner than about two nozzle-widths doesn't get two full perimeter
lines - the slicer either prints a single, weak, poorly-fused line, or drops
the wall entirely. This is invisible to OpenSCAD's own checks (a paper-thin
shell is still perfectly manifold) and easy to miss in a render, where a
0.3mm wall on a 100mm model is a handful of pixels. This script measures
actual local wall thickness across the mesh and flags anything under a
threshold, regardless of how it looks on screen.

Method: for a sample of triangles across the mesh, cast a ray from the
triangle's centroid inward along its (inverted) normal and find the nearest
opposite surface the ray hits. That hit distance is the wall thickness at
that point. This is a sampled heuristic, not an exhaustive solve - it will
reliably find thin walls and thin protrusions that make up a reasonable
fraction of the surface, but can miss a thin feature that's a tiny fraction
of a large mesh's triangle count. For anything safety- or load-bearing,
treat this as a strong signal to investigate, not a formal guarantee.

The ray search is capped a few multiples past the threshold (see
--probe-cap): a wall is only "thin" if its far side is within the threshold,
so there's no point tracing a ray across a thick solid just to measure how
thick it is. Capping keeps the check fast on chunky parts (without it, a ray
fired into a 50mm block scans its way across the whole block) and never
changes a pass/fail - it only skips exact measurement of walls that already
clear the minimum comfortably.

No third-party dependencies.

Usage:
    python3 check_wall_thickness.py model.stl
    python3 check_wall_thickness.py model.stl --min-thickness 1.2
    python3 check_wall_thickness.py model.stl --nozzle 0.4
    python3 check_wall_thickness.py model.stl --sample 5000

Exit code 0 if no sampled point is thinner than the threshold, 1 if any are
(or the file couldn't be parsed).
"""

import argparse
import struct
import sys
from collections import defaultdict

EPS = 1e-6


def read_binary_stl(data):
    if len(data) < 84:
        return None
    count = struct.unpack_from("<I", data, 80)[0]
    if 84 + count * 50 != len(data):
        return None
    triangles = []
    offset = 84
    for _ in range(count):
        v1 = struct.unpack_from("<3f", data, offset + 12)
        v2 = struct.unpack_from("<3f", data, offset + 24)
        v3 = struct.unpack_from("<3f", data, offset + 36)
        triangles.append((v1, v2, v3))
        offset += 50
    return triangles


def read_ascii_stl(text):
    triangles = []
    verts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()[1:4]
            verts.append(tuple(float(p) for p in parts))
            if len(verts) == 3:
                triangles.append(tuple(verts))
                verts = []
    return triangles


def load_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    triangles = read_binary_stl(data)
    if triangles is not None:
        return triangles
    try:
        text = data.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None
    if "facet normal" not in text:
        return None
    return read_ascii_stl(text)


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a):
    length = dot(a, a) ** 0.5
    if length < EPS:
        return None
    return scale(a, 1.0 / length)


def centroid(tri):
    return scale(add(add(tri[0], tri[1]), tri[2]), 1.0 / 3.0)


def triangle_normal(tri):
    e1 = sub(tri[1], tri[0])
    e2 = sub(tri[2], tri[0])
    n = cross(e1, e2)
    return norm(n)


def ray_triangle_intersect(origin, direction, tri):
    # Moeller-Trumbore
    v0, v1, v2 = tri
    e1 = sub(v1, v0)
    e2 = sub(v2, v0)
    h = cross(direction, e2)
    a = dot(e1, h)
    if -EPS < a < EPS:
        return None
    f = 1.0 / a
    s = sub(origin, v0)
    u = f * dot(s, h)
    if u < -1e-5 or u > 1 + 1e-5:
        return None
    q = cross(s, e1)
    v = f * dot(direction, q)
    if v < -1e-5 or u + v > 1 + 1e-5:
        return None
    t = f * dot(e2, q)
    if t > EPS:
        return t
    return None


def build_grid(triangles, cell_size):
    grid = defaultdict(list)
    for idx, tri in enumerate(triangles):
        cx, cy, cz = centroid(tri)
        cell = (int(cx // cell_size), int(cy // cell_size), int(cz // cell_size))
        grid[cell].append(idx)
    return grid


def cells_within_radius(center_cell, radius):
    # yield only the cubic SHELL at exactly this radius, not the whole cube.
    # find_thickness expands radius outward from 0, so scanning the full cube
    # each ring would re-examine every inner cell over and over (the old bug
    # that made this check O(radius^4) per probe and pathologically slow on
    # thick solids); a shell means each cell is visited exactly once.
    cx, cy, cz = center_cell
    if radius == 0:
        yield (cx, cy, cz)
        return
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if max(abs(dx), abs(dy), abs(dz)) == radius:
                    yield (cx + dx, cy + dy, cz + dz)


def find_thickness(origin, direction, self_idx, triangles, grid, cell_size, max_dist):
    cx = int(origin[0] // cell_size)
    cy = int(origin[1] // cell_size)
    cz = int(origin[2] // cell_size)
    best = None
    max_radius = int(max_dist // cell_size) + 2
    for radius in range(0, max_radius + 1):
        found_any_candidate = False
        for cell in cells_within_radius((cx, cy, cz), radius):
            for idx in grid.get(cell, ()):
                if idx == self_idx:
                    continue
                found_any_candidate = True
                t = ray_triangle_intersect(origin, direction, triangles[idx])
                if t is not None and t <= max_dist and (best is None or t < best):
                    best = t
        # once we have a hit, one extra ring guarantees we're not missing a
        # closer hit that straddles a cell boundary
        if best is not None and radius >= (best // cell_size) + 1:
            break
        if radius == max_radius and not found_any_candidate and best is None:
            break
    return best


def bbox_diagonal(triangles):
    xs, ys, zs = [], [], []
    for tri in triangles:
        for v in tri:
            xs.append(v[0])
            ys.append(v[1])
            zs.append(v[2])
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    return (dx * dx + dy * dy + dz * dz) ** 0.5, (dx, dy, dz)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl_path")
    parser.add_argument(
        "--min-thickness",
        type=float,
        default=None,
        help="flag anything thinner than this (model units, usually mm). "
        "Default: 1.2mm, a reasonable general-purpose FDM minimum.",
    )
    parser.add_argument(
        "--nozzle",
        type=float,
        default=None,
        help="alternative to --min-thickness: derive the threshold as "
        "2x this nozzle diameter (e.g. --nozzle 0.4 -> 0.8mm minimum)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=4000,
        help="max number of triangles to probe (default 4000) - large "
        "meshes are subsampled evenly for speed, this is a heuristic "
        "check, not an exhaustive one",
    )
    parser.add_argument(
        "--probe-cap",
        type=float,
        default=None,
        help="how far past a surface to look for the opposite wall before "
        "calling it 'thick enough' (model units). Default: 4x the "
        "threshold. Anything not hit within this is comfortably over the "
        "minimum, so capping never changes a pass/fail - it just keeps the "
        "check fast on chunky solids instead of probing across the whole "
        "part. Raise it only if you want exact measurements of thick walls.",
    )
    args = parser.parse_args()

    if args.min_thickness is not None:
        threshold = args.min_thickness
    elif args.nozzle is not None:
        threshold = args.nozzle * 2
    else:
        threshold = 1.2

    triangles = load_stl(args.stl_path)
    if triangles is None:
        print(f"Could not parse {args.stl_path} as STL.", file=sys.stderr)
        sys.exit(1)
    if not triangles:
        print(f"{args.stl_path} contains no triangles.", file=sys.stderr)
        sys.exit(1)

    diagonal, (dx, dy, dz) = bbox_diagonal(triangles)
    if diagonal < EPS:
        print(f"{args.stl_path} has zero size.", file=sys.stderr)
        sys.exit(1)

    # cell size tuned so each cell holds a handful of triangles on average
    cell_size = max(diagonal / 60.0, threshold)
    grid = build_grid(triangles, cell_size)
    # A wall is only "thin" if its opposite face is within the threshold, so
    # there's no need to trace a ray all the way across a thick solid to
    # measure exactly how thick it is - that's what made this check crawl on
    # chunky parts. Cap the probe a few multiples past the threshold: nothing
    # found within the cap is comfortably over the minimum. This never flips a
    # pass/fail, it only stops measuring the exact depth of walls that already
    # clear the line by a wide margin.
    probe_cap = args.probe_cap if args.probe_cap is not None else threshold * 4.0

    n = len(triangles)
    if n <= args.sample:
        sample_indices = range(n)
    else:
        step = n / args.sample
        sample_indices = [int(i * step) for i in range(args.sample)]

    thin_points = []
    min_found = None
    n_evaluated = 0
    for idx in sample_indices:
        tri = triangles[idx]
        normal = triangle_normal(tri)
        if normal is None:
            continue
        n_evaluated += 1
        c = centroid(tri)
        origin = add(c, scale(normal, -1e-4))
        direction = scale(normal, -1.0)
        t = find_thickness(origin, direction, idx, triangles, grid, cell_size, probe_cap)
        if t is None:
            # no opposing wall within the probe cap -> at least `probe_cap`
            # thick here, comfortably over the threshold; not a thin point
            continue
        if min_found is None or t < min_found:
            min_found = t
        if t < threshold:
            thin_points.append((t, c))

    if not thin_points:
        if n_evaluated == 0:
            print(
                f"Could not measure thickness on {args.stl_path}: no usable "
                "facet normals (mesh may be degenerate or corrupt) - "
                "inconclusive, not a pass."
            )
            sys.exit(1)
        if min_found is None:
            # every sampled wall was thicker than the probe cap: a solid,
            # chunky part with no thin walls to worry about
            print(
                f"OK: no wall thinner than the {probe_cap:.2f}mm probe cap "
                f"anywhere - solidly above the {threshold:.2f}mm minimum."
            )
        else:
            print(
                f"OK: thinnest sampled wall was {min_found:.2f}mm, "
                f"at or above the {threshold:.2f}mm threshold."
            )
        sys.exit(0)

    thin_points.sort(key=lambda p: p[0])
    print(
        f"WARNING: {len(thin_points)} sampled points thinner than "
        f"{threshold:.2f}mm found in {args.stl_path} - these walls may "
        f"print weak, fused poorly, or vanish entirely depending on the "
        f"printer's nozzle and line width."
    )
    for t, c in thin_points[:10]:
        print(f"  {t:.2f}mm thick near ({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f})")
    if len(thin_points) > 10:
        print(f"  ...and {len(thin_points) - 10} more sampled points below threshold")
    print(
        "\nThicken the wall(s) near the points above in the .scad source "
        "(bump the relevant thickness variable, or add material along the "
        "thin direction) and re-export. If a thin feature is intentional "
        "and load-bearing isn't a concern (a decorative fin, a snap-fit "
        "flex tab), it's fine to accept - just make sure it was a "
        "deliberate choice, not an accidental unit/parameter mistake."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
