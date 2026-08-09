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

## The UV-tiling trap

This one produces user-visible artifacts twice over before the root cause is obvious: material tiling (`SetTextureScale`) applies the same repeat count to *all six faces* of a scaled stock cube, so any face whose proportions don't match the tiling axes — end caps, tops — shows the texture squeezed into dense stripes. No tiling value can fit all faces of a non-cubic box at once.

The real fix is a custom box `Mesh` authored at final world size whose per-face UVs equal each face's world dimensions; a 1-world-unit texture then maps at natural scale on every face with the material at 1:1 tiling.

**Follow-through**: the transform drops to unit scale, so 2D colliders no longer inherit their size from it — set `BoxCollider2D.size` explicitly to the same world dimensions the scale used to provide.
