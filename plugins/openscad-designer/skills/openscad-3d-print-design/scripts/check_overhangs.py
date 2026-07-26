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

Not every flagged region means the same thing, and the difference matters far
more than the flag does - so each region is classified rather than just
listed:

  RAMP    - the region's own material reaches down to the bed. Every layer is
            anchored to the one below along the region's full length, so it
            grows off the plate instead of starting in air. Printable; the
            per-layer step says how rough the underside will be.
  BRIDGE  - starts above the bed, but is narrow enough across its minor
            extent that the slicer bridges it. Not worth chasing.
  SUPPORT - starts above the bed and is too wide to bridge. It prints, with
            support material under it. Worth trying to design out - and when
            the feature could simply sit lower, seating it on the plate turns
            it into a RAMP for free.

SCOPE: this check is about print QUALITY and support cost, not about whether
the part can be printed at all. It is desirable, not a gate - a part with
SUPPORT regions is printable, it just costs support material and surface
finish. Whether a part will physically fail on the plate - too little
footprint, too much leverage, or material laid where nothing holds it - is
check_bed_stability.py's job, and that one is the must-pass.

The distinction the classifier is built around is the one that is easiest to
miss by reading angles alone: a blade pitched 9 degrees off horizontal reports
81 degrees from vertical whether it rests on the plate or floats 1.7mm above
it. Same angle, same span, same area - one grows off the plate, one needs
support under its whole length. Only the region's lowest height tells them
apart, so that is measured and stated as a verdict rather than left to
careful reading.

Both horizontal extents of a region are reported, but the decision uses the
LONGER one, deliberately. It is tempting to judge a 1.2mm x 21mm strip by its
1.2mm width - but the distance that matters is the gap between the anchors
the material spans, and that is not something a downward-facing surface patch
knows. A 40 x 10mm flat ceiling held by posts at its two far ends must be
bridged across 40mm, not 10. Erring toward the longer extent over-reports
some thin strips; since this check only advises, that is the safe direction.
Judge a flagged strip by its AREA: 25 area units of tangent strip under a
rounded arm is a different animal from 1500 under a rotor blade.

No third-party dependencies - parses STL (binary or ASCII) directly.

Usage:
    python3 check_overhangs.py model.stl
    python3 check_overhangs.py model.stl --threshold 50
    python3 check_overhangs.py model.stl --up=-y    # part prints Y-down
                                                   # (the = is required for a
                                                   # negative axis: argparse
                                                   # reads a bare -y as a flag)
    python3 check_overhangs.py model.stl --bed-clearance 1.0
    python3 check_overhangs.py model.stl --min-area 2.0
    python3 check_overhangs.py model.stl --bridge-span 20 --layer-height 0.3

Exit code 0 if nothing past the threshold needs support, 1 if any region was
found (or the file could not be parsed). A non-zero exit is a review signal,
never a gate: some parts genuinely cannot avoid all support, and accepting it
deliberately is a fine outcome. The point is to know, and to have spent a
moment on whether a cheaper design was available.
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


def horizontal_basis(up):
    """Two orthonormal axes spanning the plane perpendicular to `up`, so a
    region's span can be measured across the bed rather than along the build
    axis. Taking max(dx, dy, dz) would let a tall thin region masquerade as a
    wide one (or a wide one hide behind its height)."""
    seed = (1.0, 0.0, 0.0) if abs(up[0]) < 0.9 else (0.0, 1.0, 0.0)
    a = cross(up, seed)
    alen = dot(a, a) ** 0.5
    a = (a[0] / alen, a[1] / alen, a[2] / alen)
    b = cross(up, a)
    blen = dot(b, b) ** 0.5
    b = (b[0] / blen, b[1] / blen, b[2] / blen)
    return a, b


def horizontal_span(verts, axis_a, axis_b):
    """(major, minor) horizontal extent of a region, along its own principal
    axes rather than the model's.

    The minor extent is what decides bridgeability: it is the distance the
    slicer actually has to carry material across. A rounded arm's underside
    ridge is a 1.2mm-wide strip 21mm long - reporting 21mm calls a trivial
    strip unbridgeable, while the blade of a rotor is wide in BOTH directions
    and stays flagged either way."""
    pts = [(dot(v, axis_a), dot(v, axis_b)) for v in verts]
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts) / n
    syy = sum((p[1] - my) ** 2 for p in pts) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts) / n
    # principal direction of the 2x2 covariance
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ct, st = math.cos(theta), math.sin(theta)
    u = [p[0] * ct + p[1] * st for p in pts]
    v = [-p[0] * st + p[1] * ct for p in pts]
    e1, e2 = max(u) - min(u), max(v) - min(v)
    return (max(e1, e2), min(e1, e2))


def layer_step(angle_deg, layer_height):
    """How far each layer sticks out past the one below, for a surface at
    `angle_deg` from vertical. This is the number that decides whether an
    overhang droops: compare it to the extrusion width (~nozzle diameter)."""
    if angle_deg >= 89.5:
        return float("inf")
    return layer_height * math.tan(math.radians(angle_deg))


def solid_columns(triangles, up, axis_a, axis_b, cell):
    """Vertical columns through the solid, as (bottom, top) height intervals.

    Every triangle is rasterised onto the bed grid and its height sampled at
    each cell centre; sorting a column's hits and pairing them gives the
    z-ranges where the column is inside the part. This sees the SOLID, which
    a surface-angle scan cannot: it is how we find material that begins in
    mid-air even when no face is steep enough to be flagged."""
    cols = {}
    for tri in triangles:
        p = [(dot(v, axis_a), dot(v, axis_b), dot(v, up)) for v in tri]
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = p
        det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(det) < 1e-12:
            continue  # edge-on triangle contributes no column hits
        i0 = int(math.floor(min(x1, x2, x3) / cell))
        i1 = int(math.ceil(max(x1, x2, x3) / cell))
        j0 = int(math.floor(min(y1, y2, y3) / cell))
        j1 = int(math.ceil(max(y1, y2, y3) / cell))
        for i in range(i0, i1 + 1):
            u = (i + 0.5) * cell
            for j in range(j0, j1 + 1):
                v = (j + 0.5) * cell
                l1 = ((y2 - y3) * (u - x3) + (x3 - x2) * (v - y3)) / det
                if l1 < 0.0:
                    continue
                l2 = ((y3 - y1) * (u - x3) + (x1 - x3) * (v - y3)) / det
                if l2 < 0.0 or l1 + l2 > 1.0:
                    continue
                l3 = 1.0 - l1 - l2
                cols.setdefault((i, j), []).append(l1 * z1 + l2 * z2 + l3 * z3)
    intervals = {}
    for k, hits in cols.items():
        hits.sort()
        if len(hits) < 2 or len(hits) % 2:
            continue  # grazing or non-manifold column - skip rather than guess
        intervals[k] = [(hits[t], hits[t + 1]) for t in range(0, len(hits), 2)]
    return intervals


def project_cells(tri_verts, axis_a, axis_b, cell):
    """Bed-plane cells covered by a run of triangles (flat list, 3 verts per
    triangle). Rasterising the triangles matters: a swept blade's underside
    is long thin facets whose CORNERS cluster at the sweep stations, so
    projecting vertices alone leaves most of the surface uncovered."""
    cells = set()
    for t in range(0, len(tri_verts) - 2, 3):
        p = [(dot(v, axis_a), dot(v, axis_b))
             for v in tri_verts[t:t + 3]]
        (x1, y1), (x2, y2), (x3, y3) = p
        det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        i0 = int(math.floor(min(x1, x2, x3) / cell))
        i1 = int(math.ceil(max(x1, x2, x3) / cell))
        j0 = int(math.floor(min(y1, y2, y3) / cell))
        j1 = int(math.ceil(max(y1, y2, y3) / cell))
        if abs(det) < 1e-12:
            # edge-on facet: cover its bounding box rather than dropping it
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    cells.add((i, j))
            continue
        for i in range(i0, i1 + 1):
            u = (i + 0.5) * cell
            for j in range(j0, j1 + 1):
                v = (j + 0.5) * cell
                l1 = ((y2 - y3) * (u - x3) + (x3 - x2) * (v - y3)) / det
                l2 = ((y3 - y1) * (u - x3) + (x1 - x3) * (v - y3)) / det
                if l1 >= -0.02 and l2 >= -0.02 and l1 + l2 <= 1.02:
                    cells.add((i, j))
    # one cell of slop, so a region's edge doesn't leak a rim of false starts
    grown = set()
    for i, j in cells:
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                grown.add((i + di, j + dj))
    return grown


def occupied(intervals, key, z, eps):
    for lo, hi in intervals.get(key, ()):  # few intervals per column
        if lo - eps <= z <= hi + eps:
            return True
    return False


def lifted_features(triangles, up, axis_a, axis_b, min_h, bed_band, cell,
                    covered):
    """Patches of material that START above the plate with nothing under them.

    A column's interval bottom is a place where material begins. It is fine
    if a neighbouring column already holds material one cell lower - that is
    a surface growing outward at 45 degrees or steeper, which self-supports.
    What is left over is material laid where the previous layer put nothing:
    the first bead of a blade floating between a hub and a rim.

    `covered` is the footprint of the regions the angle scan already
    reported, so the two passes don't say the same thing twice. This pass
    exists for what the angle scan structurally cannot see - a feature
    pitched well inside the self-supporting cone that simply starts in the
    air."""
    intervals = solid_columns(triangles, up, axis_a, axis_b, cell)
    eps = cell * 0.5
    starts = {}
    for key, ivs in intervals.items():
        i, j = key
        for lo, _hi in ivs:
            if lo - min_h <= bed_band:
                continue  # begins on the plate
            if key in covered:
                continue  # the angle scan already owns this
            supported = False
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    if di == 0 and dj == 0:
                        continue
                    if occupied(intervals, (i + di, j + dj), lo - cell, eps):
                        supported = True
                        break
                if supported:
                    break
            if not supported:
                starts[key] = min(starts.get(key, lo), lo)
    # group neighbouring starts that begin at a similar height
    patches = []
    seen = set()
    for key in starts:
        if key in seen:
            continue
        stack, comp = [key], []
        seen.add(key)
        while stack:
            k = stack.pop()
            comp.append(k)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (k[0] + di, k[1] + dj)
                    if (nb in starts and nb not in seen
                            and abs(starts[nb] - starts[k]) <= 2 * cell):
                        seen.add(nb)
                        stack.append(nb)
        patches.append(comp)
    out = []
    for comp in patches:
        pts = [((i + 0.5) * cell, (j + 0.5) * cell) for i, j in comp]
        us = [q[0] for q in pts]
        vs = [q[1] for q in pts]
        out.append({
            "area": len(comp) * cell * cell,
            "gap": min(starts[k] for k in comp) - min_h,
            "span": max(max(us) - min(us), max(vs) - min(vs)) + cell,
            "cu": sum(us) / len(us),
            "cv": sum(vs) / len(vs),
        })
    out.sort(key=lambda d: d["area"], reverse=True)
    return out


def report_lifted(triangles, up, axis_a, axis_b, min_h, bed_band, args,
                  covered, standalone=False):
    """Run and print the angle-independent pass. Returns True if it found
    anything, so the caller can set the exit code."""
    span_uv = max(
        max(dot(v, ax) for tri in triangles for v in tri)
        - min(dot(v, ax) for tri in triangles for v in tri)
        for ax in (axis_a, axis_b)
    )
    cell = max(0.3, min(1.0, span_uv / 200.0))
    lifted = [f for f in lifted_features(triangles, up, axis_a, axis_b, min_h,
                                         bed_band, cell, covered)
              if f["area"] >= args.min_area]
    if not lifted:
        return False
    if standalone:
        print(
            f"REVIEW: no surface is past {args.threshold:.0f} degrees from "
            f"vertical, but {len(lifted)} feature(s) in {args.stl_path} have "
            f"material that STARTS above the plate with nothing under it. "
            f"Found by scanning the solid, not surface angles: a surface can "
            f"sit well inside the self-supporting cone and still begin in "
            f"mid-air, and that first bead has nothing to land on."
        )
    else:
        print(
            f"\nALSO: {len(lifted)} feature(s) whose material STARTS above "
            f"the plate with nothing under it, found by scanning the solid "
            f"rather than surface angles - so these are not in the list "
            f"above."
        )
    for i, f in enumerate(lifted):
        kind = "BRIDGE" if f["span"] <= args.bridge_span else "SUPPORT"
        print(
            f"  {kind} - lifted feature {i + 1}: {f['area']:.1f} area units "
            f"of first-layer material, starting {f['gap']:.2f}mm above the "
            f"plate, {f['span']:.1f}mm across, near "
            f"({f['cu']:.1f}, {f['cv']:.1f}) in the bed plane"
        )
    print(
        "  If such a feature could simply sit lower, dropping it until its "
        "lowest edge touches the plate is the cheapest fix there is - it "
        "turns a span in mid-air into a ramp and adds first-layer area "
        "instead of spending it."
    )
    return True


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
        "--up=-y if the STL rests on its +Y face on the bed - write it with "
        "an = sign, or argparse reads the leading minus as a flag.",
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
        "--flat-angle",
        type=float,
        default=80.0,
        help="angle from vertical (default 80) past which a facet counts as "
        "near-flat rather than sloped. A region's near-flat part is what has "
        "to bridge or be supported, so it is classified on its own.",
    )
    parser.add_argument(
        "--bridge-span",
        type=float,
        default=15.0,
        help="longest near-flat span (model units, default 15) a slicer is "
        "assumed to bridge unsupported between anchors. Anything wider that "
        "does not reach the bed is reported as AIRBORNE.",
    )
    parser.add_argument(
        "--layer-height",
        type=float,
        default=0.2,
        help="layer height used to report each region's per-layer step "
        "(default 0.2) - how far one layer overhangs the one below.",
    )
    parser.add_argument(
        "--extrusion-width",
        type=float,
        default=0.4,
        help="extrusion width (default 0.4) the per-layer step is judged "
        "against: a step wider than this leaves the new bead with nothing "
        "under it and the underside droops.",
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
        axis_a, axis_b = horizontal_basis(up)
        if report_lifted(triangles, up, axis_a, axis_b, min_h, bed_band,
                         args, set(), standalone=True):
            sys.exit(1)
        print(
            f"OK: no downward surface past {args.threshold:.0f} degrees from "
            f"vertical above the bed - this should print with no (or "
            f"minimal) support in its current orientation."
        )
        sys.exit(0)

    # group flagged facets into connected regions, keeping the near-flat
    # facets separately: within one connected region the near-flat part is
    # what has to bridge or be supported, and it can sit at a completely
    # different height from the sloped part it is joined to.
    regions = {}
    for idx, angle, area, ks in flagged:
        root = uf.find(ks[0])
        r = regions.setdefault(
            root,
            {
                "area": 0.0, "worst": 0.0, "count": 0, "verts": [],
                "flat_area": 0.0, "flat_worst": 0.0, "flat_verts": [],
                "slope_area": 0.0, "slope_worst": 0.0, "slope_verts": [],
            },
        )
        r["area"] += area
        r["worst"] = max(r["worst"], angle)
        r["count"] += 1
        r["verts"].extend(triangles[idx])
        part = "flat" if angle >= args.flat_angle else "slope"
        r[part + "_area"] += area
        r[part + "_worst"] = max(r[part + "_worst"], angle)
        r[part + "_verts"].extend(triangles[idx])

    regions = [r for r in regions.values() if r["area"] >= args.min_area]
    if not regions:
        axis_a, axis_b = horizontal_basis(up)
        if report_lifted(triangles, up, axis_a, axis_b, min_h, bed_band,
                         args, set(), standalone=True):
            sys.exit(1)
        print(
            f"OK: the only downward surfaces past {args.threshold:.0f} "
            f"degrees are sliver facets below the {args.min_area:.1f} "
            f"area filter - no real overhang to support."
        )
        sys.exit(0)

    axis_a, axis_b = horizontal_basis(up)

    # Classify each region. The question a verdict answers is the one that
    # decides printability: when the nozzle reaches this region's first layer,
    # is there material under it? A region whose own geometry descends to the
    # bed answers yes by construction - it grows off the plate. One that
    # starts in the air answers no, and then only its span decides whether the
    # slicer can bridge the gap or the model has to change.
    for r in regions:
        parts = []
        for tag, label in (("flat", "near-flat part"), ("slope", "sloped part")):
            part_area = r[tag + "_area"]
            if part_area < args.min_area:
                continue
            verts = r[tag + "_verts"]
            gap = min(dot(v, up) for v in verts) - min_h
            parts.append({
                "label": label,
                "area": part_area,
                "worst": r[tag + "_worst"],
                "gap": gap,
                "span": horizontal_span(verts, axis_a, axis_b)[0],
                "span_min": horizontal_span(verts, axis_a, axis_b)[1],
            })
        if not parts:
            # small region split just below both part filters - judge it whole
            parts = [{
                "label": "whole region",
                "area": r["area"],
                "worst": r["worst"],
                "gap": min(dot(v, up) for v in r["verts"]) - min_h,
                "span": horizontal_span(r["verts"], axis_a, axis_b)[0],
                "span_min": horizontal_span(r["verts"], axis_a, axis_b)[1],
            }]
        for p in parts:
            if p["gap"] <= bed_band:
                p["verdict"] = "RAMP"
            elif p["span"] <= args.bridge_span:
                p["verdict"] = "BRIDGE"
            else:
                p["verdict"] = "SUPPORT"
        # A region is AIRBORNE if any part of it is, however small - that is
        # the verdict that must never be averaged away. Otherwise the region
        # is characterised by its largest part, so a sliver of near-flat mesh
        # doesn't relabel a whole ramp as a bridge.
        driver = max(parts, key=lambda p: (p["verdict"] == "SUPPORT",
                                           p["area"]))
        parts.sort(key=lambda p: p["area"], reverse=True)
        r["parts"] = parts
        r["driver"] = driver
        r["verdict"] = driver["verdict"]

    counts = {v: sum(1 for r in regions if r["verdict"] == v)
              for v in ("SUPPORT", "BRIDGE", "RAMP")}
    needs_support = counts["SUPPORT"]

    # worst verdict first, then by area - the region you must act on leads
    order = {"SUPPORT": 0, "BRIDGE": 1, "RAMP": 2}
    regions.sort(key=lambda r: (order[r["verdict"]], -r["area"]))
    total_area = sum(r["area"] for r in regions)

    summary = ", ".join(f"{counts[v]} {v.lower()}" for v in
                        ("SUPPORT", "BRIDGE", "RAMP") if counts[v])
    if needs_support:
        print(
            f"REVIEW: {needs_support} of {len(regions)} overhang region(s) in "
            f"{args.stl_path} would need support in this orientation - they "
            f"start above the plate and are too wide to bridge. Worth trying "
            f"to design out; not a blocker if you accept support knowingly."
        )
    else:
        print(
            f"REVIEW: {len(regions)} overhang region(s) past "
            f"{args.threshold:.0f} degrees from vertical in "
            f"{args.stl_path} - each one either reaches the bed or is short "
            f"enough to bridge. Nothing here needs support."
        )
    print(
        f"  {len(regions)} region(s) past {args.threshold:.0f} deg from "
        f"vertical, {total_area:.1f} total area units: {summary}."
    )
    print(
        f"  Seat tolerance {bed_band:.2f}mm: a region whose lowest material "
        f"is within that of the plate counts as reaching it. Anything higher "
        f"hangs from its anchors, and then its span decides.\n"
        f"  This check is about print QUALITY and support, not whether the "
        f"part can be printed at all - check_bed_stability.py owns that."
    )
    print()

    for i, r in enumerate(regions):
        d = r["driver"]
        xs = [v[0] for v in r["verts"]]
        ys = [v[1] for v in r["verts"]]
        zs = [v[2] for v in r["verts"]]
        cx, cy, cz = (
            (min(xs) + max(xs)) / 2,
            (min(ys) + max(ys)) / 2,
            (min(zs) + max(zs)) / 2,
        )
        print(
            f"  {r['verdict']} - region {i + 1}: {r['area']:.1f} area units, "
            f"worst {r['worst']:.0f} deg from vertical, center ~= "
            f"({cx:.2f}, {cy:.2f}, {cz:.2f})"
        )
        for p in r["parts"]:
            print(
                f"      {p['verdict']:<8} {p['label']}: {p['area']:.1f} "
                f"area units, worst {p['worst']:.0f} deg, spans "
                f"{p['span']:.1f} x {p['span_min']:.1f}mm across the bed, "
                f"lowest material {p['gap']:.2f}mm above the plate"
            )
        if r["verdict"] == "RAMP":
            step = layer_step(d["worst"], args.layer_height)
            if step > args.extrusion_width:
                note = (
                    f"steps {step:.2f}mm per {args.layer_height:.2f}mm layer, "
                    f"wider than a {args.extrusion_width:.1f}mm bead, so the "
                    f"underside will droop and look rough - shallow the slope "
                    f"if the finish matters, but it will not fail"
                )
            else:
                note = (
                    f"steps {step:.2f}mm per {args.layer_height:.2f}mm layer, "
                    f"within a {args.extrusion_width:.1f}mm bead's overlap - "
                    f"clean self-supporting ramp"
                )
            print(f"      -> reaches the bed, so every layer lands on the one "
                  f"below; {note}.")
        elif r["verdict"] == "BRIDGE":
            print(f"      -> starts above the plate but spans only "
                  f"{d['span']:.1f}mm between anchors - the slicer bridges "
                  f"this. Not worth chasing.")
        else:
            print(f"      -> {d['gap']:.2f}mm of air below it and "
                  f"{d['span']:.1f}mm across, past the "
                  f"{args.bridge_span:.0f}mm bridging limit - support "
                  f"material unless the design changes. If this feature "
                  f"could simply sit lower, seating it on the plate turns it "
                  f"into a ramp and costs nothing.")

    if needs_support:
        print(
            "\nThe SUPPORT regions are the ones worth design effort, roughly "
            "in order of preference:\n"
            "  - Seat the feature on the bed if it can sit lower: drop it "
            "until its lowest edge touches z=0 and it becomes a ramp growing "
            "off the plate, adding first-layer area rather than spending it. "
            "Anchor it by the edge that must touch the bed, not by its "
            "centreline. This is free when it is available.\n"
            "  - Reorient the part so the overhang faces up or becomes a "
            "wall, or chamfer/fillet the underside into the self-supporting "
            "angle.\n"
            "  - Split it so each piece prints in a good pose.\n"
            "  - Accept support - fine when it is unavoidable, as a "
            "deliberate call you report, and you should be able to say what "
            "each layer lands on."
        )
    else:
        print(
            "\nNothing above is airborne, but each region still needs a "
            "deliberate call. Options, roughly in order of preference:\n"
            "  - Reorient the part so the overhang faces up or becomes a wall "
            "(often the single biggest win - re-export in the new pose and "
            "re-run this check).\n"
            "  - Add a chamfer or fillet so the underside stays within the "
            "self-supporting angle instead of going flat.\n"
            "  - Split it into separately-printed parts, each with a good "
            "orientation (see the multi-part section of the skill).\n"
            "  - Accept it - fine when it's unavoidable, just make it a "
            "deliberate call and tell the user where it lands."
        )

    span_uv = max(
        max(dot(v, ax) for tri in triangles for v in tri)
        - min(dot(v, ax) for tri in triangles for v in tri)
        for ax in (axis_a, axis_b)
    )
    cell = max(0.3, min(1.0, span_uv / 200.0))
    covered = set()
    for r in regions:
        covered |= project_cells(r["verts"], axis_a, axis_b, cell)
    report_lifted(triangles, up, axis_a, axis_b, min_h, bed_band, args,
                  covered)
    sys.exit(1)


if __name__ == "__main__":
    main()
