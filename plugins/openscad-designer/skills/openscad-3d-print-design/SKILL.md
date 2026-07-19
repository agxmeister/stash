---
name: openscad-3d-print-design
description: Design and build 3D-printable models in OpenSCAD (.scad) and export them to .stl (or other formats). Covers print-optimized geometry (flat beds with real contact area, self-supporting overhangs, support-free details) and a thorough multi-angle visual QA workflow using the loom MCP tools before export. Use this whenever the user asks to create, design, or model something for 3D printing, mentions a .scad file, an STL export, a print bed, supports/overhangs, or wants a physical object modeled — even if they just describe an object ("make me a...", "model a...", "design a part that...") without explicitly saying "3D print" or naming OpenSCAD.
---

# OpenSCAD 3D-Print Design

A model that's actually good for 3D printing — not just geometrically valid — has to **sit and stick on the print bed** and be **printable without supports wherever reasonably possible**. Both are easy to get wrong in ways that look fine in a single render and only show up from angles you weren't looking at, or when you do the arithmetic on where a feature actually lands.

The loop: **design with print constraints in mind → validate → render from every angle → hunt for the bugs that hide from a single view → fix → repeat → export.** Don't export after the first render that "looks right" — the cheapest render is the one that catches a bug before it's baked into 20 minutes of failed printing.

## Design principles

Each of these is a real failure mode that looked correct until checked properly.

**Parts must genuinely overlap, not just look close.** This is the most common way these models come out broken: `union()` doesn't merge solids that don't intersect. A part positioned a fraction of a millimeter short of its neighbor (an arm against a body, an ear against a head) stays a separate shell — it prints as a loose unattached chunk, or fails outright as plastic extruded into thin air. Each disconnected shell is individually manifold, so validate/export report no errors, and a small gap can be invisible in renders. When positioning any part against another, build in deliberate overlap (a millimeter or two into the neighboring solid) rather than trying to land exactly on the surface — slight overlap unions away invisibly, while landing short is a silent, total failure. The mandatory connectivity check in the workflow is the only reliable catch.

**Walls need real thickness, not just non-zero.** A wall thinner than about two nozzle-widths (~0.8mm on a common 0.4mm nozzle; design toward 1.2–2mm as a comfortable default) gets printed as a single weak line or dropped entirely by the slicer. Thin walls sneak in easily — a `difference()` cavity sized without an explicit wall variable, a too-generous offset, a parameter typo — and a 0.3mm wall on a 100mm model is a few pixels in a render; manifold checks don't measure thickness at all. Give every shell/wall an explicit named thickness variable so it's a single number to reason about, and rely on the mandatory thickness check in the workflow.

**A flat base needs real contact area, not a tangent point.** A sphere resting at exactly `z = radius` touches the bed at a single mathematical point — zero adhesion. Sink the center so a genuine cap gets sliced off: at `z = radius - cut_depth`, the flat disc has radius `sqrt(radius² − (radius − cut_depth)²)`. Pick a `cut_depth` that gives a comfortably large disc (tens of percent of the part's own radius, not a sliver). Ask this of every rounded shape: if I flatten this, does the flat face actually have area?

**Keep unsupported overhangs within ~45° of vertical.** FDM bridges slopes up to about 45° from vertical; beyond that, material sags or fails. Judge a protruding feature by its angle to the **build axis**, not to the surface it attaches to — a 60°-from-vertical arm looks fine in a still render but is a near-horizontal cantilever in reality. This is genuinely hard to judge by eye across a whole model, so `scripts/check_overhangs.py` measures it (mandatory, see workflow).

**Short tapered protrusions can cheat.** A cone-shaped tip under roughly 10–15mm (a nose) bridges fine even close to horizontal; a long arm or beam at the same angle sags. Use judgment on scale, not just angle.

**Prefer dimples over bumps for small surface details.** Eyes, buttons, logos: a shallow subtracted indentation needs zero support and prints cleanly; a small raised bump on a curved surface is itself a tiny overhang. This is close to a free win — default to it.

**Large flat faces are the best print orientation you can ask for.** A hat brim, base plate, or flange that's naturally flat should be truly flat (a plane, not an approximation) and oriented to sit on the bed or print as a horizontal top surface. This usually makes the model look cleaner too.

**Moderate overlap between unioned rounded parts.** Enough that there's no razor-thin seam at the joint, but not so much that distinct parts blur into one fat mass — heavy overlap usually isn't needed for printability anyway. For "distinct but connected," bias toward less overlap than your first instinct.

## One printed piece vs. separate, assembled parts

Decide this **before writing geometry** — it shapes the design, and defaulting to "one fused object" is itself a print-optimization miss for many designs, not a safe neutral choice:

- **Must be separate**: anything that moves relative to the rest after assembly — a spinning wheel, a hinge, a lid that opens — physically cannot be fused to its neighbor.
- **Better separate**: a part whose best bed orientation conflicts with the rest, or that needs heavy support fused but prints flat and clean alone. A toy car's wheels print flat on a face with zero support; fused into the wheel wells they print on a curved edge packed with support material.
- **Split purely for printability**: nothing functional requires it, but the geometry fights printing — an overhang that reorienting/chamfering can't fix without breaking another face's flatness, features needing contradictory orientations, or a part bigger than the bed. Before settling for "export with support anyway," consider cutting along a sensible seam and rejoining after printing (a peg-and-socket pair, registration pins alongside a glued flat interface). Each piece then picks its own best orientation, which often makes every piece print support-free even though the whole object couldn't. Report it as the printability decision it is: "the arm prints separately and glues on — as one piece it needed support the full length of the underside."

Don't force it either: a single-orientation object with no awkward parts (a nameplate, a simple hook) has no reason to be more than one piece. Whenever the deliverable changes, say so plainly — "the wheels print separately and press-fit onto the axles" tells the user how many files to expect and how to assemble them.

**Multi-part print ≠ multi-file source.** Source split across several `.scad` files can still describe one physical printed object. Intentionally separate parts get their own STL exports, each passing the connectivity check as exactly one piece. Two disconnected shells inside one export are never "detachable parts" — that's the floating-geometry bug the connectivity check exists to catch.

### Connector fit: positive clearance, not interference

On FDM, "press fit / stays together without glue" does **not** mean a peg bigger than its socket. The printer adds roughness and slight oversizing at the layer level, so a true interference fit routinely refuses to insert at all, or needs force that cracks the part. Both usable regimes are *clearances* — peg smaller than socket — differing only in size:

- **Tight/friction** (assembles by hand and stays put — split-part seams, snap-on lids, glued-or-not add-ons): gap of **0.1–0.2mm per side** as a safe default; against vertical walls it can go tighter — down toward 0 — for real grip rather than a slip fit (see *Clearance is directional* below).
- **Loose/moving** (wheel on axle, hinge pin, lift-off lid — anything that rotates, slides, or removes freely): gap of **0.2mm or more per side** — 0.2–0.4mm is a reasonable starting point, sized up for a rougher printer/filament or larger feature.
- A **true interference fit** (zero/negative clearance) is a rare, deliberate choice: a forced permanent bond where press force and cracking risk are accepted. Don't reach for it just because "press fit" sounds like it.
- A small retaining lip or flare at an axle tip (slightly larger than the hub bore) keeps a wheel captive without glue while still spinning freely.

**Clearance is directional — how much you need depends on which way the mating surface faces.** FDM doesn't reproduce every direction equally well, so a single symmetric gap is often wrong for a joint that has to both grip *and* seat. Two cases behave very differently:

- **Walls — the vertical sides a part slides past** (a socket bore, the flanks of a slot): this clearance is measured horizontally, and vertical walls print close to true size. For a *tight* joint the wall gap can go nearly to **0mm per side** — ~0.1mm already assembles cleanly but grips only lightly, so if you want it firm, go smaller. This is where the printer's own bias shows most: a machine running a hair oversized turns a 0mm nominal into real grip, while one running undersized may want a slightly negative *nominal* gap. Tune it on a test print.
- **Ceilings — a downward-facing overhead surface the part seats up against** (the roof of a bottom-loading pocket, the top of a blind hole): this clearance is measured vertically, and the roof prints as a bridge that **sags downward** into the cavity, shrinking the gap after printing. Leave **more** room here — around **0.2mm** — *even when the joint is meant to be tight everywhere else*, or the drooped ceiling stops the part before it can fully seat. This is a seating allowance, not a grip surface; extra room costs nothing but a hair of play in a direction that doesn't hold the joint together.

**Contact area sets the tight-fit number.** Grip is friction, so the wall gap you can get away with depends on how much of the part the walls actually wrap. A peg **fully enveloped** by its bore holds firmly at **~0.1mm**; a part that meets its cavity along only a couple of flanks — a dumbbell pin gripped just on its bulb's curved sides — needs **~0** to feel equally tight. The less the walls wrap, the smaller the gap.

Give the gap an explicit named variable applied to both peg and socket — one number to reason about and tune, not something implicit in two independently-chosen diameters. A single `connector_gap = 0.15;` serves a simple joint; a joint that grips on its walls but seats against a ceiling wants two — `wall_gap = 0;`, `ceiling_gap = 0.2;` — so grip and seating tune independently.

**Then verify the geometry actually has that clearance.** A variable applied to one side of the joint but not the other, a diameter/radius mix-up, off-axis positioning, or an unrelated boss/fillet protruding into the joint area all survive the assembly render — previews `union()` everything, which silently absorbs any overlap, so a collision looks exactly as clean as a perfect fit. Build a small throwaway file that exports `intersection() { partA(); partB(); }` with both parts at their true assembled position (not their print orientations):

- **Clearance joints (both regimes)**: the export should fail with "Current top level object is empty" — that failure **is** the pass: zero overlap, the peg genuinely fits. Any successful export means something is colliding; track it down. This confirms the gap's *sign* only — trust the named `connector_gap` variable for the magnitude (a 5mm-oversized socket also exports empty, and would be sloppy despite passing).
- **True interference joints**: a successful export is expected — run `python3 <skill-dir>/scripts/check_interference.py path/to/intersection.stl` to confirm the overlap is a usable few tenths of a millimeter rather than a jam. It measures a characteristic thickness for each connected piece separately (with a location), so one real jam can't hide behind an average across several joints.

## Structure and libraries

Once a model has several distinct parts or reusable mechanical elements, split the source into `main.scad` + `lib/` + `parts/` — and for anything hardware-adjacent (screw threads, nut traps, gears, snap-fits, rounded boxes), use the BOSL2 library rather than hand-building; it encodes tolerance and printability lessons that took its authors many iterations. Both have traps: a missing BOSL2 install fails *silently* (warning, not error — geometry renders bare/incomplete), `use` vs `include` semantics differ, and every top-level file needs its own BOSL2 include. **Read `references/structure-and-libraries.md` before splitting source files or writing any BOSL2-dependent geometry.** For two or three simple parts with no shared logic, one flat file is less overhead — don't split just because a model has more than one part.

## OpenSCAD gotchas worth knowing up front

These won't throw an error — the model validates and renders, just doesn't do what you meant.

- **A feature anchored at a solid's center must out-reach the radius before it protrudes at all.** A cylinder translated to a sphere's center needs length greater than the sphere's radius before any of it appears outside. Always compute: does this feature's reach exceed the distance from its anchor to the surface it must poke through?
- **`rotate([θ,0,0])` direction is easy to get backwards.** On a cylinder pointing along +Z, it sends the tip to `y' = −sin(θ)·h, z' = cos(θ)·h`. A flipped sign points a nose or arm directly away from where it should be — invisible from the "obvious" viewing angle. When an added feature doesn't show up in a render, check its direction before assuming it's missing.
- **A subtracted detail can land inside the wrong solid where parts overlap.** A "head" dimple anchored near the surface can still be inside the "body" sphere in the neck region — the visible surface there belongs to the other part, and the dimple gets buried. Check the anchor's distance to every nearby part's center, not just the one you aimed at.
- **The loom preview camera format is `eye_x,y,z,center_x,y,z`, and `viewAll: true` (the default) auto-fits the whole object** regardless of requested distance — and `viewAll: false` won't reliably force a close-up either. To see a small feature clearly, bump `width`/`height` on a full-object shot instead of fighting the camera.

## Workflow

1. **Decide one piece vs. separate parts first** (section above), then write the `.scad` with the design principles built in — a flat base and shallow overhangs are much cheaper designed in than retrofitted. Use `$fn` 32–64 for smooth curves without slow renders. Split source files and reach for BOSL2 per the structure section when the model warrants it.
2. **`mcp__loom__validate-model`** — fast syntax/geometry sanity check before spending time on renders.
3. **`mcp__loom__render-preview` from enough angles to see the whole thing**: front, back, left, right, top, bottom, and at least one 3/4 perspective. Compute real camera coordinates (e.g. the actual z-height of a face) rather than eyeballing — it's the difference between a render that shows the feature you're checking and one that doesn't.
4. **Hunt the failure modes above**: does the base genuinely read flat from below? Did every added feature show up, on the correct side? Any protrusion closer to horizontal than 45°? Any parts ballooned into each other that shouldn't be?
5. **Fix, re-validate, re-render** — re-check the angles that would reveal a regression, not just the one that showed the bug. If a fix means accepting support that reorienting/chamfering can't avoid, revisit the split-for-printability option before settling.
6. **`mcp__loom__export-model`** once clean. One fused design → one STL. Deliberately separate parts → **each piece as its own STL** (a temporary top-level file per part), each in its own intended print orientation.
7. **Run the check scripts on every exported STL, every time, no exceptions.** These are the checks a render literally cannot substitute for — every one of these failure modes is invisible to OpenSCAD's manifold check and easy to miss by eye:
   - `python3 <skill-dir>/scripts/check_connectivity.py model.stl` — reports how many disconnected pieces exist and roughly where. Every exported file must report **exactly one piece**.
   - `python3 <skill-dir>/scripts/check_wall_thickness.py model.stl` — thinnest wall found vs. threshold (default 1.2mm; `--nozzle 0.4` to derive it from the target printer instead).
   - `python3 <skill-dir>/scripts/check_overhangs.py model.stl` — every downward-facing surface leaning more than ~45° from vertical (`--threshold` to tune, `--up` if the part doesn't print Z-up), grouped into regions with location, area, and span. This one is a **review signal, not hard pass/fail**: the STL must be in its intended print orientation to mean anything, and some parts genuinely can't avoid all support. Treat each flagged region as a prompt to reorient, chamfer, or split first; accept support only as a deliberate, reported choice. Short flat spans are flagged as probable bridges — don't chase those.
   - For every mating joint between separately printed parts: the `intersection()` export + `check_interference.py` procedure from the connector-fit section.

   A flagged problem says which part and roughly where — fix it in the `.scad` source (add overlap, thicken the wall variable, reorient/chamfer/split, adjust peg/socket), re-export, re-run until clean. For multi-part designs, run connectivity, thickness, and overhangs on **each** part's own STL — each prints in its own orientation, so each needs its own overhang pass.
8. **Report back concisely**: each export is manifold, single-piece, and passes the thickness check (not just "Status: NoError" — see why above); mating joints pass the interference check. State the support picture plainly per part: support-free in the given orientation, or name where support is needed and why it was unavoidable. If there are multiple printed parts, describe how they assemble ("the wheels press-fit onto the axles — 0.3mm clearance so they still spin"). Call out anything else that matters for slicing ("the nose bridges fine at this size, but scaled up it'll need support").

Don't treat "it rendered without errors" as "it's done" — a perfectly manifold model can still have an invisible feature, a backwards rotation, a single-point base, geometry silently missing from a failed `include`, a part that never attached, a wall too thin to print, or an overhang that will droop. Renders catch the first few if you look from the right angles; the connectivity, thickness, and overhang scripts catch the rest, which renders systematically miss. Budget the turns for all of it.
