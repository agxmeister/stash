#!/usr/bin/env python3
"""
Measure whether a part can actually stand on the bed long enough to finish
printing, and whether it fits the printer at all.

This is the failure mode the other checks are blind to. A part can be
manifold, single-piece, thick-walled and completely free of overhangs, and
still be impossible to print - because it is tall and stands on almost
nothing. A 192mm shaft that tapers to a 5mm nose has 18mm^2 of bed contact
holding up 192mm of leverage: the toolhead knocks it loose, or it rings
itself into a wobbling mess. Every existing check passes it. Only measuring
the footprint against the height catches it.

Two independent things can go wrong, so both are measured:

  ADHESION - the bond area holding the part down. Reported as
  height / sqrt(footprint_area), a dimensionless slenderness. A 20mm cube
  scores 1. A 100mm tower on a 20x20 base scores 5 and prints fine. Past
  about 8 you want a brim; past about 15 the part is fighting you.

  TIPPING - how far the footprint braces the part sideways. Reported as
  height / minimum_footprint_width, where the width is the narrowest caliper
  measurement across the contact patch. These two differ for long, thin
  footprints: a 100mm-tall fin on a 100 x 3mm edge has a healthy-looking
  300mm^2 of contact, but only 3mm of brace across its thin axis, and it
  will oscillate. Area alone would call that fine.

The footprint is measured as the real cross-section a small distance above
the lowest point (default 0.2mm - one layer), rasterised and filled, so
holes, rings and multiple islands are all handled correctly. A part resting
on a ring (a cup, a rotor rim) gets credit for the ring's true area, and for
the full width the ring braces it across.

Orientation matters completely: this measures the mesh exactly as it sits in
the file, assuming +Z is up and the bed is at the lowest point. Export the
part in the pose it will actually print in, or the numbers mean nothing.

Pass --bed to also check the part fits the machine. Getting this wrong wastes
the whole print, and it is the one constraint you cannot design around after
the fact - you have to split the part or scale it.

No third-party dependencies - parses STL (binary or ASCII) directly.

Usage:
    python3 check_bed_stability.py model.stl
    python3 check_bed_stability.py model.stl --bed 220x220x250
    python3 check_bed_stability.py model.stl --layer 0.3
    python3 check_bed_stability.py model.stl --adhesion-limit 12

Exit code 0 if the part will stand up - including when it is in the brim
band, which is a printable outcome and reported as one, not a failure. Exit
1 only when a score is past a limit, i.e. the base or the orientation has to
change. Like the overhang check this is a review signal rather than a gate:
read the two scores, don't just test the exit code. A part in the brim band
is fine to print deliberately, with a brim. A part past a limit is telling
you the design needs to change - see the "part that cannot be printed as
designed" section of the skill for the escalation ladder.
"""

import argparse
import math
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


def slice_segments(triangles, z):
    """Undirected 2D segments where the mesh crosses the plane at height z."""
    segments = []
    for tri in triangles:
        pts = []
        for i in range(3):
            a, b = tri[i], tri[(i + 1) % 3]
            za, zb = a[2], b[2]
            if (za - z) * (zb - z) < 0:
                f = (z - za) / (zb - za)
                pts.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            elif za == z and zb != z:
                pts.append((a[0], a[1]))
        if len(pts) == 2 and pts[0] != pts[1]:
            segments.append((pts[0], pts[1]))
    return segments


def rasterise(segments, cell):
    """Scanline-fill the cross-section. Returns the set of filled cells.

    Even-odd filling straight off the segment soup, rather than stitching
    loops first - it needs no orientation information and tolerates the
    numerical noise a sliced mesh always has.
    """
    xs = [p[0] for s in segments for p in s]
    ys = [p[1] for s in segments for p in s]
    x0, y0 = min(xs), min(ys)
    ny = max(1, int(math.ceil((max(ys) - y0) / cell)))
    filled = set()
    for row in range(ny):
        y = y0 + (row + 0.5) * cell
        crossings = []
        for (ax, ay), (bx, by) in segments:
            # half-open rule, so a vertex exactly on the scanline counts once
            if (ay <= y < by) or (by <= y < ay):
                crossings.append(ax + (y - ay) / (by - ay) * (bx - ax))
        if len(crossings) < 2:
            continue
        crossings.sort()
        for i in range(0, len(crossings) - 1, 2):
            ca, cb = crossings[i], crossings[i + 1]
            col_a = int(math.floor((ca - x0) / cell))
            col_b = int(math.ceil((cb - x0) / cell))
            for col in range(col_a, col_b):
                cx = x0 + (col + 0.5) * cell
                if ca <= cx <= cb:
                    filled.add((row, col))
    return filled


def island_components(filled):
    """The filled cells split into 4-connected components."""
    seen = set()
    comps = []
    for start in filled:
        if start in seen:
            continue
        comp = {start}
        stack = [start]
        seen.add(start)
        while stack:
            r, c = stack.pop()
            for nb in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if nb in filled and nb not in seen:
                    seen.add(nb)
                    comp.add(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def count_islands(filled):
    return len(island_components(filled))


def merge_height(triangles, z0, z1, cell, samples=24):
    """The height at which the part's cross-section first becomes a single
    connected island.

    Until that height, each first-layer island is standing on its own: the
    toolhead is pushing a separate little tower around, and the part's total
    footprint is not holding it. Above it the islands brace each other. This
    is the height each island's slenderness should be judged against - using
    the full part height would condemn every ring-and-hub part, and using the
    aggregate footprint (the old behaviour) lets two thin towers hide behind
    a shared convex hull."""
    for i in range(1, samples + 1):
        z = z0 + (z1 - z0) * (i / samples) * 0.995
        segs = slice_segments(triangles, z)
        if not segs:
            continue
        filled = rasterise(segs, cell)
        if filled and len(island_components(filled)) == 1:
            return z
    return None


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                (ox, oy), (qx, qy) = out[-2], out[-1]
                if (qx - ox) * (p[1] - oy) - (qy - oy) * (p[0] - ox) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    return half(pts)[:-1] + half(reversed(pts))[:-1]


def min_caliper_width(hull):
    """Narrowest distance between two parallel lines enclosing the hull.

    This is the axis the part is least braced against, which is what decides
    whether it wobbles - not the bounding box, which depends on how the part
    happens to be rotated in XY.
    """
    if len(hull) < 2:
        return 0.0
    if len(hull) == 2:
        return 0.0
    best = float("inf")
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < 1e-9:
            continue
        widest = 0.0
        for px, py in hull:
            d = abs(ex * (py - ay) - ey * (px - ax)) / length
            widest = max(widest, d)
        best = min(best, widest)
    return best


def parse_bed(text):
    parts = text.lower().replace("*", "x").split("x")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "bed must look like 220x220x250 (X by Y by Z, in mm)"
        )
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("bed dimensions must be numbers")


def main():
    ap = argparse.ArgumentParser(
        description="Check a part's bed contact against its height, and "
        "whether it fits the printer."
    )
    ap.add_argument("stl_path")
    ap.add_argument(
        "--layer",
        type=float,
        default=0.2,
        help="height above the lowest point at which the footprint is "
        "measured, i.e. the first layer (default 0.2mm)",
    )
    ap.add_argument(
        "--bed",
        type=parse_bed,
        default=None,
        help="build volume as XxYxZ in mm, e.g. 220x220x250. Omitted by "
        "default because it is printer-specific - pass it when you know "
        "the target machine.",
    )
    ap.add_argument(
        "--adhesion-limit",
        type=float,
        default=15.0,
        help="height/sqrt(area) past which the part is called unstable "
        "(default 15; a brim is advised past 8)",
    )
    ap.add_argument(
        "--tipping-limit",
        type=float,
        default=22.0,
        help="height/min-footprint-width past which the part is called "
        "unstable (default 22; a brim is advised past 12)",
    )
    args = ap.parse_args()

    triangles = load_stl(args.stl_path)
    if not triangles:
        print(f"ERROR: could not read any triangles from {args.stl_path}")
        sys.exit(1)

    zs = [v[2] for t in triangles for v in t]
    xs = [v[0] for t in triangles for v in t]
    ys = [v[1] for t in triangles for v in t]
    zmin, zmax = min(zs), max(zs)
    height = zmax - zmin
    size = (max(xs) - min(xs), max(ys) - min(ys), height)

    if height <= 0:
        print(f"ERROR: {args.stl_path} is flat - nothing to measure.")
        sys.exit(1)

    z = zmin + args.layer
    segments = slice_segments(triangles, z)
    if not segments:
        print(
            f"ERROR: no cross-section {args.layer}mm above the bottom of "
            f"{args.stl_path}. Is the part resting on z-min at all?"
        )
        sys.exit(1)

    cell = max(0.05, min(0.5, max(size[0], size[1]) / 400.0))
    filled = rasterise(segments, cell)
    area = len(filled) * cell * cell
    comps = island_components(filled)
    islands = len(comps)
    hull = convex_hull([p for s in segments for p in s])
    width = min_caliper_width(hull)

    if area <= 0:
        print(
            f"ERROR: the first layer of {args.stl_path} has no measurable "
            f"area - the part is resting on a point or an edge, which "
            f"cannot stick to the bed at all."
        )
        sys.exit(1)

    adhesion = height / math.sqrt(area)
    tipping = height / width if width > 1e-6 else float("inf")

    print(
        f"{args.stl_path}: {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f}mm, "
        f"first layer {area:.1f}mm^2 across {islands} island(s), "
        f"narrowest footprint width {width:.1f}mm"
    )
    print(
        f"  adhesion  height/sqrt(area)  = {adhesion:5.1f}   "
        f"(brim past 8, unstable past {args.adhesion_limit:.0f})"
    )
    print(
        f"  tipping   height/min-width   = {tipping:5.1f}   "
        f"(brim past 12, unstable past {args.tipping_limit:.0f})"
    )

    # Per-island stability. The aggregate numbers above treat every contact
    # patch as one base, which is exactly wrong for a part that stands on
    # several separate feet: two thin towers joined only near the top sum
    # their areas and share one convex hull, so the pair reads as broad and
    # stable while each tower is on its own until they meet.
    island_problems = []
    if islands > 1:
        zm = merge_height(triangles, zmin + args.layer, zmax, cell)
        free_h = (zm - zmin) if zm is not None else height
        print(
            f"  {islands} separate contact patches, joined at "
            + (f"z = {zm:.1f}mm" if zm is not None else "no height - they "
               "never merge into one cross-section")
            + f"; until then each stands alone, so each is scored against "
              f"that {free_h:.1f}mm on its own:"
        )
        x0 = min(p[0] for s_ in segments for p in s_)
        y0 = min(p[1] for s_ in segments for p in s_)
        for i, comp in enumerate(sorted(comps, key=len, reverse=True)):
            pts = [(x0 + (c + 0.5) * cell, y0 + (r + 0.5) * cell)
                   for r, c in comp]
            i_area = len(comp) * cell * cell
            i_width = min_caliper_width(convex_hull(pts)) if len(pts) >= 3 else 0.0
            i_adh = free_h / math.sqrt(i_area) if i_area > 0 else float("inf")
            i_tip = free_h / i_width if i_width > 1e-6 else float("inf")
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            flag = ""
            if i_adh > args.adhesion_limit or i_tip > args.tipping_limit:
                flag = "  <-- past the limit on its own"
                island_problems.append(
                    f"contact patch {i + 1} near ({cx:.0f}, {cy:.0f}) is only "
                    f"{i_area:.1f}mm^2 / {i_width:.1f}mm wide and stands alone "
                    f"for {free_h:.0f}mm (adhesion {i_adh:.1f}, tipping "
                    f"{i_tip:.1f}) - the part's total footprint is not holding "
                    f"it up there"
                )
            print(
                f"    patch {i + 1}: {i_area:7.1f}mm^2, {i_width:5.1f}mm wide, "
                f"near ({cx:6.1f}, {cy:6.1f})  adhesion {i_adh:4.1f} / "
                f"tipping {i_tip:4.1f}{flag}"
            )

    problems = list(island_problems)
    if adhesion > args.adhesion_limit:
        problems.append(
            f"only {area:.1f}mm^2 of bed contact under a {height:.0f}mm-tall "
            f"part (adhesion score {adhesion:.1f})"
        )
    if tipping > args.tipping_limit:
        problems.append(
            f"the footprint is only {width:.1f}mm across its narrowest axis, "
            f"bracing a {height:.0f}mm-tall part (tipping score {tipping:.1f})"
        )

    fits = True
    if args.bed:
        bx, by, bz = args.bed
        if size[2] > bz:
            fits = False
            problems.append(
                f"the part is {size[2]:.0f}mm tall but the machine's Z is "
                f"{bz:.0f}mm - it does not fit and must be split or scaled"
            )
        straight = size[0] <= bx and size[1] <= by
        diagonal = math.hypot(bx, by) >= math.hypot(size[0], size[1])
        if not straight:
            fits = False
            if diagonal:
                problems.append(
                    f"the {size[0]:.0f} x {size[1]:.0f}mm footprint does not "
                    f"fit {bx:.0f} x {by:.0f}mm square-on, though it may fit "
                    f"rotated diagonally - verify in the slicer"
                )
            else:
                problems.append(
                    f"the {size[0]:.0f} x {size[1]:.0f}mm footprint does not "
                    f"fit the {bx:.0f} x {by:.0f}mm bed in any rotation - it "
                    f"must be split or scaled"
                )

    if problems:
        print(f"\nREVIEW: {args.stl_path} will be difficult or impossible to print:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nThe fixes, in order of preference - the first that works is "
            "the one to take:\n"
            "  - Reorient the part so a genuinely broad face is down. This "
            "costs nothing and is very often available; re-export in the "
            "new pose and re-run.\n"
            "  - Give it a real base. A tapered nose or a rounded bottom "
            "resting near a point is usually incidental to the design - "
            "flatten it, or move the taper to the TOP where it prints as a "
            "self-supporting cone instead of a foothold.\n"
            "  - Split it into separately-printed pieces, and put the "
            "length on whichever piece has the broad footprint. Pieces that "
            "must stand on a bare section should be kept short. Join them "
            "with a keyed, glued splice (see references/connector-fit.md).\n"
            "  - Add a brim and accept it - reasonable in the brim band, "
            "not a fix for a part well past the limit.\n"
            "Report which one you took and why, so the choice is visible."
        )
        sys.exit(1)

    note = ""
    if adhesion > 8 or tipping > 12:
        note = " Use a brim - it is in the marginal band, not the safe one."
    if args.bed and fits:
        note += " Fits the given build volume."
    print(f"\nOK: {args.stl_path} should stand up to printing.{note}")
    sys.exit(0)


if __name__ == "__main__":
    main()
