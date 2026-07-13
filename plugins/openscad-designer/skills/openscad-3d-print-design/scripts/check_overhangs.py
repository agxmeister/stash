#!/usr/bin/env python3
"""
Find surfaces that will need support to print, in the model's export
orientation.

FDM printers build up in +Z from the bed at z=0. A downward-facing exterior
surface has air beneath it, and if it leans more than ~45 degrees away from
vertical it sags or fails outright without support material under it. The
design goal for a good print is to keep those overhangs within the
self-supporting range wherever possible, and to know about the ones that
remain BEFORE slicing - so you can reorient the part, add a chamfer/fillet,
or split it, rather than discovering it as a drooping mess on the plate.

This is genuinely hard to judge by eye: a 60-degrees-from-vertical underside
looks fine in most camera angles, and OpenSCAD's manifold check says nothing
about print orientation at all. This script measures the actual angle of
every downward-facing surface against the build axis and reports the ones
that need support, grouped into regions with a location and area for each.

Important: this measures the mesh EXACTLY as it sits in the file. The STL
must be in its intended print orientation (the way it rests on the bed) for
the result to mean anything - if the .scad models the part in some other
pose, rotate it flat before exporting the file you check here.

What is and isn't flagged:
  - Only DOWNWARD-facing surfaces (normal pointing below horizontal) are
    overhang candidates. An upward-facing slope is never an overhang.
  - The model's bottom face (resting on the bed) is excluded via a small
    bed-clearance band above the lowest point - it sits on the plate, it
    doesn't need support.
  - Steepness is measured from VERTICAL: a vertical wall is 0 degrees (fine),
    a flat horizontal ceiling is 90 degrees (worst case). Anything past the
    threshold (default 45) is reported.

Bridges are the honest caveat: a short, flat, downward span between two
supports (or a short tapered tip) often prints clean even near-horizontal,
because the slicer bridges it. This script reports each region's horizontal
span so you can judge that, but it cannot know for certain whether a given
overhang will bridge - a large flat ceiling reported with a small span is a
bridge candidate; the same angle over a wide span is not.

No third-party dependencies - parses STL (binary or ASCII) directly.

Usage:
    python3 check_overhangs.py model.stl
    python3 check_overhangs.py model.stl --threshold 50
    python3 check_overhangs.py model.stl --up -y     # part prints Y-down
    python3 check_overhangs.py model.stl --bed-clearance 1.0
    python3 check_overhangs.py model.stl --min-area 2.0

Exit code 0 if nothing past the threshold needs support, 1 if overhangs were
found (or the file couldn't be parsed). A non-zero exit is a review signal,
not necessarily a defect: some parts genuinely cannot avoid all support, and
that's a fine outcome to accept deliberately - the point is to know.
"""

import argparse
import math
import struct
import sys

EPS = 1e-9


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


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def parse_up(text):
    axes = {
        "x": (1.0, 0.0, 0.0),
        "y": (0.0, 1.0, 0.0),
        "z": (0.0, 0.0, 1.0),
        "+x": (1.0, 0.0, 0.0),
        "+y": (0.0, 1.0, 0.0),
        "+z": (0.0, 0.0, 1.0),
        "-x": (-1.0, 0.0, 0.0),
        "-y": (0.0, -1.0, 0.0),
        "-z": (0.0, 0.0, -1.0),
    }
    key = text.strip().lower()
    if key not in axes:
        raise argparse.ArgumentTypeError(
            f"--up must be one of x,y,z,+x,+y,+z,-x,-y,-z (got {text!r})"
        )
    return axes[key]


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


def vkey(vertex, tolerance):
    return tuple(round(c / tolerance) for c in vertex)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stl_path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=45.0,
        help="overhang angle from vertical, in degrees, past which a "
        "surface is flagged as needing support (default 45). A vertical "
        "wall is 0; a horizontal ceiling is 90.",
    )
    parser.add_argument(
        "--up",
        type=parse_up,
        default=(0.0, 0.0, 1.0),
        help="build/up axis the part prints along (default +z). Use e.g. "
        "-y if the STL is oriented to rest on its +Y face on the bed.",
    )
    parser.add_argument(
        "--bed-clearance",
        type=float,
        default=None,
        help="height band above the model's lowest point treated as bed "
        "contact and never flagged (model units, usually mm). Default: "
        "0.5mm, or 1%% of the model's build-axis height, whichever is larger.",
    )
    parser.add_argument(
        "--min-area",
        type=float,
        default=1.0,
        help="ignore flagged regions whose total area is below this "
        "(square model units, default 1.0) - filters out sliver facets "
        "that don't represent a real printable overhang.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="distance within which two vertices are treated as shared "
        "when grouping flagged facets into regions (default 0.001)",
    )
    args = parser.parse_args()

    triangles = load_stl(args.stl_path)
    if triangles is None:
        print(f"Could not parse {args.stl_path} as STL.", file=sys.stderr)
        sys.exit(1)
    if not triangles:
        print(f"{args.stl_path} contains no triangles.", file=sys.stderr)
        sys.exit(1)

    up = args.up
    up_len = dot(up, up) ** 0.5
    up = (up[0] / up_len, up[1] / up_len, up[2] / up_len)

    # height of every point along the build axis, to find the bed and the
    # clearance band
    heights = [dot(v, up) for tri in triangles for v in tri]
    min_h, max_h = min(heights), max(heights)
    span_h = max_h - min_h
    if span_h < EPS:
        print(f"{args.stl_path} has zero height along the build axis.",
              file=sys.stderr)
        sys.exit(1)

    if args.bed_clearance is not None:
        bed_band = args.bed_clearance
    else:
        bed_band = max(0.5, 0.01 * span_h)

    # a surface at exactly `threshold` from vertical has an outward normal
    # tilted `threshold` below horizontal, i.e. its component along -up is
    # sin(threshold). Flag downward faces steeper than that.
    sin_thresh = math.sin(math.radians(args.threshold))

    uf = UnionFind()
    flagged = []  # (tri_index, angle_deg, area, vkeys)
    for idx, tri in enumerate(triangles):
        e1 = sub(tri[1], tri[0])
        e2 = sub(tri[2], tri[0])
        n = cross(e1, e2)
        area = 0.5 * (dot(n, n) ** 0.5)
        if area < EPS:
            continue
        nlen = dot(n, n) ** 0.5
        nu = dot(n, up) / nlen  # normal component along build axis, [-1, 1]
        if nu >= -sin_thresh:
            # upward-facing, vertical, or within the self-supporting cone
            continue
        # downward-facing and steeper than the threshold
        centroid_h = (dot(tri[0], up) + dot(tri[1], up) + dot(tri[2], up)) / 3.0
        if centroid_h - min_h <= bed_band:
            continue  # resting on the bed, not an overhang
        angle = math.degrees(math.asin(min(1.0, -nu)))
        ks = [vkey(v, args.tolerance) for v in tri]
        uf.union(ks[0], ks[1])
        uf.union(ks[1], ks[2])
        flagged.append((idx, angle, area, ks))

    if not flagged:
        print(
            f"OK: no downward surface past {args.threshold:.0f} degrees from "
            f"vertical above the bed - this should print with no (or "
            f"minimal) support in its current orientation."
        )
        sys.exit(0)

    # group flagged facets into connected regions
    regions = {}
    for idx, angle, area, ks in flagged:
        root = uf.find(ks[0])
        r = regions.setdefault(
            root,
            {"area": 0.0, "worst": 0.0, "verts": [], "count": 0},
        )
        r["area"] += area
        r["worst"] = max(r["worst"], angle)
        r["count"] += 1
        r["verts"].extend(triangles[idx])

    regions = [r for r in regions.values() if r["area"] >= args.min_area]
    if not regions:
        print(
            f"OK: the only downward surfaces past {args.threshold:.0f} "
            f"degrees are sliver facets below the {args.min_area:.1f} "
            f"area filter - no real overhang to support."
        )
        sys.exit(0)

    regions.sort(key=lambda r: r["area"], reverse=True)
    total_area = sum(r["area"] for r in regions)
    print(
        f"REVIEW: {len(regions)} overhang region(s) past "
        f"{args.threshold:.0f} degrees from vertical found in "
        f"{args.stl_path} ({total_area:.1f} total area units) - these will "
        f"need support in the current print orientation."
    )
    for i, r in enumerate(regions):
        xs = [v[0] for v in r["verts"]]
        ys = [v[1] for v in r["verts"]]
        zs = [v[2] for v in r["verts"]]
        cx, cy, cz = (
            (min(xs) + max(xs)) / 2,
            (min(ys) + max(ys)) / 2,
            (min(zs) + max(zs)) / 2,
        )
        # horizontal span = extent in the plane perpendicular to the build
        # axis, a rough bridge-feasibility hint
        span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        bridge = ""
        if r["worst"] >= 80 and span <= 15:
            bridge = "  (near-flat but short span - may bridge without support)"
        print(
            f"  region {i + 1}: {r['area']:.1f} area units, worst "
            f"{r['worst']:.0f} deg from vertical, span ~{span:.1f}mm, "
            f"center ~= ({cx:.2f}, {cy:.2f}, {cz:.2f}){bridge}"
        )
    print(
        "\nEach region above overhangs more than the printer can bridge "
        "unsupported. Options, roughly in order of preference:\n"
        "  - Reorient the part so the overhang faces up or becomes a wall "
        "(often the single biggest win - re-export in the new pose and "
        "re-run this check).\n"
        "  - Add a chamfer or fillet so the underside stays within the "
        "self-supporting angle instead of going flat.\n"
        "  - Split it into separately-printed parts, each with a good "
        "orientation (see the multi-part section of the skill).\n"
        "  - Accept support for that region - fine when it's unavoidable, "
        "just make it a deliberate call and tell the user where it lands.\n"
        "A short flat span flagged above may bridge fine; a wide one won't."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
