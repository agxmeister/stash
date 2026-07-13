#!/usr/bin/env python3
"""
Measure how badly two mating parts collide, not just whether they do.

For parts meant to fuse into one printed piece, overlap is required (see
check_connectivity.py). For parts meant to print as SEPARATE pieces and be
assembled afterward (a peg into a socket, a wheel on an axle, a lid onto a
box), the opposite failure mode applies: if the two parts' nominal solids
collide by more than the small clearance/interference the joint was designed
for, the printed parts will not physically go together at all - too much
material is in the way, jammed at the point of contact.

This isn't something a render can catch either: a render of the assembled
preview typically shows a union() of both parts, which silently absorbs any
amount of overlap into one smooth-looking shape, however much the peg
actually collides with the socket walls.

The precise way to check this: don't union() the two mating parts - take
their INTERSECTION instead, at the exact relative position they'll be
assembled in. `intersection() { partA(); partB(); }` in OpenSCAD computes
the literal 3D volume where the two nominal solids overlap. Export that
intersection to an STL (a separate, throwaway check file, not part of the
real print output) and run this script on it:

  - If OpenSCAD's export fails with "Current top level object is empty",
    there is ZERO overlap - the parts don't collide at all at that position.
    That's the correct outcome for a clearance-fit moving joint, and is only
    a concern if the design specifically wanted a small interference press
    fit (in which case zero overlap likely means the fit is too loose, a
    separate, much less severe problem than the one this script exists to
    catch).
  - If the export succeeds, this script first splits the interference solid
    into separate connected pieces (the same union-find approach
    check_connectivity.py uses) before measuring anything. This matters
    because a single aggregate measurement over the whole file can hide a
    real problem: if one joint's collision is small but another joint
    checked in the same export has a genuine jam, or if a big legitimate
    near-zero-clearance area sits alongside one small localized collision, a
    single global average can dilute the bad spot below any reasonable
    threshold and report a false "OK". Measuring each connected piece on its
    own catches that a render (or an unsplit average) would miss.
  - For each piece, thickness is estimated as a "characteristic thickness" =
    2 x volume / surface area. For any predominantly thin shape - a flat
    slab OR a thin ring (which is what a slightly-oversized ROUND peg
    produces: a band of interference wrapping all the way around, whose
    bounding box is misleadingly the full peg diameter even when the band
    itself is a fraction of a millimeter thick) - this ratio converges to
    within ~15-20% of the true thickness, verified against both slab and
    cylindrical-ring reference geometry. It's a heuristic, not an exact
    local measurement, but it's fast, robust to shape, and doesn't get
    fooled by curvature the way a bounding box does.

No third-party dependencies.

Usage:
    python3 check_interference.py intersection.stl
    python3 check_interference.py intersection.stl --max-penetration 0.6
    python3 check_interference.py intersection.stl --tolerance 0.001

Exit code 0 if every connected piece is within the threshold (or the file is
empty/has no geometry), 1 if any piece exceeds it (or the file couldn't be
parsed).
"""

import argparse
import struct
import sys


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


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def triangle_area(tri):
    e1 = sub(tri[1], tri[0])
    e2 = sub(tri[2], tri[0])
    cx, cy, cz = cross(e1, e2)
    return 0.5 * (cx * cx + cy * cy + cz * cz) ** 0.5


def triangle_signed_volume(tri):
    v0, v1, v2 = tri
    return (
        v0[0] * (v1[1] * v2[2] - v1[2] * v2[1])
        - v0[1] * (v1[0] * v2[2] - v1[2] * v2[0])
        + v0[2] * (v1[0] * v2[1] - v1[1] * v2[0])
    ) / 6.0


def bbox_center(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        (min(zs) + max(zs)) / 2,
    )


def bbox_dims(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def vertex_key(vertex, tolerance):
    return tuple(round(c / tolerance) for c in vertex)


def find_components(triangles, tolerance):
    uf = UnionFind()
    tri_keys = []
    for tri in triangles:
        ks = [vertex_key(v, tolerance) for v in tri]
        tri_keys.append(ks)
        uf.union(ks[0], ks[1])
        uf.union(ks[1], ks[2])

    groups = {}
    for tri, ks in zip(triangles, tri_keys):
        root = uf.find(ks[0])
        groups.setdefault(root, []).append(tri)

    return list(groups.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl_path")
    parser.add_argument(
        "--max-penetration",
        type=float,
        default=0.6,
        help="flag a connected piece of the interference solid if its "
        "characteristic thickness (2 x volume / surface area) exceeds this "
        "(model units, usually mm). Default 0.6mm - a bit above the top of "
        "a typical few-tenths interference fit.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="distance (model units, usually mm) within which two vertices "
        "are treated as the same point when splitting into connected "
        "pieces (default: 0.001)",
    )
    args = parser.parse_args()

    triangles = load_stl(args.stl_path)
    if triangles is None:
        print(f"Could not parse {args.stl_path} as STL.", file=sys.stderr)
        sys.exit(1)
    if not triangles:
        print(
            f"OK: {args.stl_path} has no geometry - zero overlap between "
            "the parts at this position. Correct for a clearance/moving "
            "joint; if a press fit was intended, this likely means it's "
            "too loose rather than too tight."
        )
        sys.exit(0)

    components = find_components(triangles, args.tolerance)
    components.sort(key=len, reverse=True)

    results = []
    for comp_triangles in components:
        volume = abs(sum(triangle_signed_volume(t) for t in comp_triangles))
        area = sum(triangle_area(t) for t in comp_triangles)
        verts = [v for tri in comp_triangles for v in tri]
        center = bbox_center(verts)
        dims = bbox_dims(verts)
        thickness = (2.0 * volume / area) if area > 1e-9 else 0.0
        results.append(
            {
                "triangles": len(comp_triangles),
                "volume": volume,
                "area": area,
                "center": center,
                "dims": dims,
                "thickness": thickness,
            }
        )

    print(
        f"Interference solid found in {args.stl_path}: {len(results)} "
        f"connected piece(s) of overlap."
    )
    bad = [r for r in results if r["thickness"] > args.max_penetration]
    for i, r in enumerate(results):
        verdict = "COLLISION" if r["thickness"] > args.max_penetration else "ok"
        cx, cy, cz = r["center"]
        dx, dy, dz = r["dims"]
        print(
            f"  piece {i + 1} [{verdict}]: characteristic thickness="
            f"{r['thickness']:.2f}mm, volume={r['volume']:.3f}mm^3, "
            f"bounding box {dx:.2f} x {dy:.2f} x {dz:.2f}mm, "
            f"center ~= ({cx:.2f}, {cy:.2f}, {cz:.2f})"
        )

    if bad:
        print(
            f"\nWARNING: {len(bad)} of {len(results)} piece(s) exceed the "
            f"{args.max_penetration:.2f}mm threshold - not a small "
            "intentional interference fit, a real collision. The parts "
            "will not physically assemble as designed at the location(s) "
            "flagged above. Check the mating feature's dimensions in the "
            ".scad source (peg vs. socket size, an unrelated bystander "
            "feature protruding into the joint, or the parts' relative "
            "position) and reduce the overlap to a deliberate few tenths "
            "of a millimeter."
        )
        sys.exit(1)

    print(
        f"\nOK: all piece(s) are within the {args.max_penetration:.2f}mm "
        "threshold - reads as an intentional small interference fit, not "
        "a collision."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
