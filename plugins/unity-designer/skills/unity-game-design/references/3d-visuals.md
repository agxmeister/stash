# Giving a 2D game 3D visuals — and the cube UV-tiling trap

"Make it look 3D" should not touch gameplay: keep the 2D physics (Rigidbody2D, 2D colliders, XY plane) and swap only the visuals. Apply these changes through the same staged/repair-stage state machine described in SKILL.md — each swap below is a repair stage with an old-state guard and a paired deferred-save gate.

## The mesh swap recipe

Replace each `SpriteRenderer` with `MeshFilter` + `MeshRenderer` using built-in meshes (`Resources.GetBuiltinResource<Mesh>("Cube.fbx")`, `"New-Sphere.fbx"`) and URP Lit materials (`Shader.Find("Universal Render Pipeline/Lit")`, tint via `_BaseColor`), switch the camera to perspective, and add a backdrop plane behind the playfield so the directional light's shadows land somewhere visible — shadows are the strongest depth cue.

**Keeping the framing identical**: to make the perspective camera show the same on-screen extent as the old orthographic view, derive the FOV from `distance * tan(fov/2) = orthoSize` at the gameplay plane.

## Consequences that follow the swap

- `SpriteRenderer.color` per-instance tinting is gone; use a `MaterialPropertyBlock` with `_BaseColor` on the `MeshRenderer` so every instance still shares one material.
- Sprite overlays (decals, damage states) still work on meshes: a child `SpriteRenderer` positioned just in front of the mesh face renders on the transparent queue and z-tests correctly against it.
- Boxes that overlap or share a coplanar face z-fight, visibly at grazing angles. Butt boxes against each other (frame corners: columns end at the beam's *underside*), never overlap them — the old orthographic camera may have been cropping the overlap out of view; a perspective camera and a free Scene-view orbit will not.
- Procedural textures meant for tinted materials are best drawn in grayscale (structure only) so the material's `_BaseColor` supplies the color, same as tinted sprites.

## Depth that reads on a head-on camera: lean the walls, and depth alone is not enough

**A face parallel to the view direction has no screen area.** With the camera looking straight down −Z, a wall standing perpendicular to a surface is edge-on and contributes nothing at all to the image. Anything meant to read as depth — a groove, a recess, a bevel — must have its walls **leaned** (a chamfer) so they present real area and the directional light can differentiate them. A recess built with vertical walls is simply invisible from the gameplay camera.

The companion rule: a recess whose floor is parallel to the face is lit *identically* to the face, so the recess itself contributes almost nothing. What reads is the shading on the chamfer, and whatever is placed **in** the recess. **Depth alone does not read; contrast does.**

## Grid-shaped geometry: make the grid carry the shape

**Prefer a height field to hand-fitted walls.** Assembling a recess (or any grid-following relief) from floor quads, trapezoid walls and corner patches means deciding per cell which walls to emit — every awkward glyph shape becomes its own special case, with real risk of unsealed corners. Instead, assign a depth to each *grid vertex* (down where all four adjacent cells are inside the shape, up otherwise) and draw one quad per cell through its four corner depths. Neighboring cells share corner depths by construction, so the surface is watertight; miters at outside corners and dimples at inside corners fall out for free; there is no corner code at all — and the whole builder comes out at roughly a third of the code of the wall-fitting version.

The principle: **when geometry follows a grid, make the grid carry the shape and let the mesh be a function of it.** Fitting pieces together at boundaries is where the special cases live.

## The UV-tiling trap

This one produces user-visible artifacts twice over before the root cause is obvious: material tiling (`SetTextureScale`) applies the same repeat count to *all six faces* of a scaled stock cube, so any face whose proportions don't match the tiling axes — end caps, tops — shows the texture squeezed into dense stripes. No tiling value can fit all faces of a non-cubic box at once.

The real fix is a custom box `Mesh` authored at final world size whose per-face UVs equal each face's world dimensions; a 1-world-unit texture then maps at natural scale on every face with the material at 1:1 tiling.

**Follow-through**: the transform drops to unit scale, so 2D colliders no longer inherit their size from it — set `BoxCollider2D.size` explicitly to the same world dimensions the scale used to provide.
