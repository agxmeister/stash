#!/usr/bin/env python3
"""
Check an STL for disconnected pieces (floating parts).

An STL that unions several sub-parts can validate and export perfectly cleanly
(each disjoint shell is individually manifold, so OpenSCAD's own checks won't
flag it) while still being print-garbage: a piece that was meant to attach to
the body but was positioned or rotated a hair off doesn't get merged by
union() at all, and comes out as a second, separate shell in the STL. On the
printer that means either a loose blob sitting unattached in the middle of
the model, or - if it doesn't touch the bed - unsupported plastic printed
into thin air. A render can miss this if the gap is small or hidden behind
other geometry; this script catches it exactly, regardless of gap size.

No third-party dependencies - parses STL (binary or ASCII) directly and
finds connected components via union-find over shared vertices.

Usage:
    python3 check_connectivity.py model.stl
    python3 check_connectivity.py model.stl --tolerance 0.001

Exit code 0 if the mesh is a single connected piece, 1 if it found more than
one (or the file couldn't be parsed).
"""

import argparse
import struct
import sys


def read_binary_stl(data):
    triangles = []
    if len(data) < 84:
        return None
    count = struct.unpack_from("<I", data, 80)[0]
    expected_len = 84 + count * 50
    if expected_len != len(data):
        return None
    offset = 84
    for _ in range(count):
        # normal (3 floats) + 3 vertices (3 floats each) + 2-byte attr count
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


def key(vertex, tolerance):
    return tuple(round(c / tolerance) for c in vertex)


def find_components(triangles, tolerance):
    uf = UnionFind()
    tri_keys = []
    for tri in triangles:
        ks = [key(v, tolerance) for v in tri]
        tri_keys.append(ks)
        uf.union(ks[0], ks[1])
        uf.union(ks[1], ks[2])

    groups = {}
    for tri, ks in zip(triangles, tri_keys):
        root = uf.find(ks[0])
        bucket = groups.setdefault(root, {"triangles": 0, "verts": []})
        bucket["triangles"] += 1
        bucket["verts"].extend(tri)

    return list(groups.values())


def bbox_center(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (
        (min(xs) + max(xs)) / 2,
        (min(ys) + max(ys)) / 2,
        (min(zs) + max(zs)) / 2,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl_path")
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="distance (model units, usually mm) within which two vertices "
        "are treated as touching (default: 0.001)",
    )
    args = parser.parse_args()

    triangles = load_stl(args.stl_path)
    if triangles is None:
        print(f"Could not parse {args.stl_path} as STL.", file=sys.stderr)
        sys.exit(1)
    if not triangles:
        print(f"{args.stl_path} contains no triangles.", file=sys.stderr)
        sys.exit(1)

    components = find_components(triangles, args.tolerance)
    components.sort(key=lambda c: c["triangles"], reverse=True)

    if len(components) == 1:
        print(f"OK: single connected piece ({len(triangles)} triangles).")
        sys.exit(0)

    print(
        f"WARNING: {len(components)} disconnected pieces found in {args.stl_path} "
        f"- this will NOT print as one solid object."
    )
    for i, comp in enumerate(components):
        cx, cy, cz = bbox_center(comp["verts"])
        print(
            f"  piece {i + 1}: {comp['triangles']} triangles, "
            f"bounding-box center ~= ({cx:.2f}, {cy:.2f}, {cz:.2f})"
        )
    print(
        "\nEach piece above is individually manifold, which is why "
        "validate-model/export-model report no errors - this check is the "
        "only way to catch that they don't actually touch each other. Find "
        "the part(s) whose position/rotation math places them near, but not "
        "overlapping, their neighbor, and fix the geometry so they union "
        "into one shell before re-exporting."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
