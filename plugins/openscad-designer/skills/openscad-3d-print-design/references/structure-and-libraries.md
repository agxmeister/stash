# Structuring source across files & using BOSL2

## Multi-file layout

Once a model has several distinct physical parts, or reusable mechanical elements (fasteners, gears, enclosures) that show up more than once, split the source rather than letting one file balloon into an unreadable wall of modules:

```
project/
├── main.scad
├── lib/
│   ├── fasteners.scad     // screws, nuts, standoffs
│   ├── enclosures.scad    // box shells, lids, snap-fits
│   └── gears.scad         // involute gears, etc.
└── parts/
    ├── bracket.scad
    └── housing.scad
```

- `main.scad` is the top-level assembly: it pulls in `lib/` and `parts/`, positions everything, and is the file you actually validate/render/export.
- `lib/` holds reusable, project-specific building blocks — modules meant to be called from multiple places.
- `parts/` holds the distinct physical pieces of the design, each generally something that could stand on its own.

This split earns its keep once there's real reuse or enough pieces that one file gets hard to navigate. For two or three simple parts with no shared logic, a flat file is genuinely less overhead — don't split just because a model has more than one part.

**`use` vs `include`:** `use <file.scad>` pulls in modules/functions without re-running the file's top-level statements — the right default for library and part files, since it avoids variable clashes and accidentally rendering another file's top-level geometry into yours. Reach for `include <file.scad>` only when you deliberately need that file's global variables or top-level side effects too.

**Iterating on one part:** it's often faster to validate/render that part's own file standalone (with a temporary top-level call to just that module) before re-checking it in the full assembly — a fast, isolated feedback loop on the piece you're changing beats re-rendering the whole assembly every time. If the part depends on BOSL2, each standalone file needs its own `include <BOSL2/std.scad>` (see below) — it doesn't inherit through a `use` chain.

## BOSL2

Screw threads, nut traps, snap-fit joints, involute gears, rounded/chamfered boxes — these are fiddly to get both geometrically correct and print-friendly from scratch, and [BOSL2](https://github.com/BelfrySCAD/BOSL2) already encodes the tolerances and support-free-printing lessons that took its authors many iterations. It's the standard choice for hardware-adjacent geometry (`screw()`, `nut()`, `gear()`, rounded-box primitives, attachable positioning) — but treat it as an environment dependency to verify, not something to assume is installed:

- **Check it resolves before building real geometry on it.** A missing library doesn't hard-fail: `include <BOSL2/std.scad>` prints a "can't find include file" *warning* and quietly continues, so BOSL2-dependent geometry renders as a bare/incomplete shape instead of erroring, and `validate-model` won't reliably catch it. Spend one cheap render up front on a throwaway file — `include <BOSL2/std.scad>; cube(1); #cuboid(2);` — to confirm the library resolves. If it's missing, clone BOSL2 into OpenSCAD's default library directory (e.g. `~/Documents/OpenSCAD/libraries/BOSL2` on macOS) — once per environment, not once per model.
- **`std.scad` doesn't pull in every submodule.** Some functionality lives in separate files — e.g. `screw()`/`screw_hole()` are in `BOSL2/screws.scad`. If a BOSL2 call comes back as an unknown module, check whether it needs its own explicit `include` for a more specific file.
- **Every top-level file needs its own `include <BOSL2/std.scad>`**, even files only reached via `use` from another file — BOSL2's attachable/tag system relies on `$`-prefixed variables set up directly in the file OpenSCAD is rendering, and that setup doesn't propagate through a `use` chain the way plain modules do.
- **Not every parameter combination is valid.** Some option combinations legitimately raise assertion errors deep inside the library (e.g. certain `screw_hole()` tolerance/thread combinations). If a call throws from inside library code rather than your own, check the library's own examples for the working pattern rather than guessing at nearby parameter tweaks.
