---
name: unity-game-design
description: Playbook for building or modifying Unity games/scenes through a Unity MCP bridge (endpoints like /compile, /console, /play, /scene, /object) that has no endpoint to directly create GameObjects or assets — content is built by writing [InitializeOnLoadMethod] Editor scripts run by Unity's compile/reload cycle. Covers the staged/resumable pattern that survives AssetDatabase's stale-index quirks (CreateAsset/SaveAsPrefabAsset returning null on immediate read-back), how CreateAsset over an existing path destroys the asset and nulls every live scene reference to it (m_Mesh: {fileID: 0} after a save), repair stages ordered ahead of saves, save gates that key on new state so they can't loop forever, the exact compile-and-check loop (forcing newline, /compile polling, /console since-cursor pitfalls), verifying structure via /scene and /object vs appearance via file-saved screenshots taken early, saving the scene without the Editor-freezing "changed on disk" modal, why setup stages silently stop when Play mode is left on, why a played game is not a saved scene, giving a 2D game 3D visuals (mesh swaps, perspective framing math, chamfers/contrast for engraved detail, height-field meshes for grid geometry, the cube UV-tiling stripe trap), and keeping editor-authored content separate from runtime-only state. Use whenever the user works on a Unity project through an MCP/Uplink-style bridge, wants GameObjects/prefabs/sprites/scenes changed programmatically, wants a 2D game to "look 3D" or lettering/detail engraved into meshes, reports "nothing shows up until I press play", sees asset writes return null, sees scene references go null after re-running an asset stage, sees stages stall despite clean compiles, sees textures squeezed into stripes on some faces of a scaled cube, or hits bridge timeouts after a scene save.
---

# Unity Game Design via MCP

Most Unity MCP bridges expose *inspection and control* — `/status`, `/console`, `/compile`, `/play`, `/screenshot`, `/refresh`, `/scene`, `/object`, `/tests` — but not *creation*: there is usually no `/create-gameobject` or `/set-property` endpoint. The only way to build or modify scene/asset content is to **write real C# Editor code** into the project and let Unity's own compile/reload cycle execute it. You are not remote-controlling the Editor; you are patching the project's source and asset-generation logic, then asking Unity to run it. The MCP calls trigger that cycle and verify what happened — they are not themselves the mechanism of change.

## The core workflow

> **Before every burst of compile-and-check, call `/status` and confirm `isPlaying: false`.**
> Play mode turns the whole setup state machine into a silent no-op. The failure signature:
> *clean compiles + real reloads + zero stage logs = Play mode, every time.*
> Details in the Play-mode trap below.

1. Write or edit an Editor-only script (under an `Editor/` folder) that performs the desired creation/setup, guarded by `[InitializeOnLoadMethod]` so it runs automatically on every domain reload.
2. Trigger a recompile via the MCP `/compile` endpoint (or equivalent) — see "The compile-and-check loop" below for the exact recipe.
3. Verify: structure via `/console`/`/scene`/`/object`, appearance via a screenshot saved to a file — see "Verifying: structure vs appearance" below.
4. Repeat, editing the script further, until the desired state exists.

**Clarify the visual target before building geometry.** When a request is about how something *looks* ("engraved", "glowing", "beveled") and admits more than one reading, resolve it before writing geometry — either by asking, or by building the cheapest possible version and showing a screenshot. Visual intent is exactly the kind of ambiguity that is cheap to resolve up front and expensive afterwards, because by then the result is wired into stages, assets and docs.

## Verifying: structure vs appearance

Split verification by what is being checked:

- **Structure** (references, transforms, component values): `/scene` and `/object` — read `m_Sprite`, `m_LocalScale`, `m_Color`, `m_Size`, `m_LocalPosition` etc. directly. Screenshots are a poor and expensive way to verify these.
- **Appearance** (anything about how it *looks* — shape, lighting, legibility, color, on-screen scale): a screenshot is the only ground truth, and it should be taken **as early as there is anything to see** — before the change is wired into setup stages, prefabs or documentation. A rough preview that is obviously wrong is worth more than a finished implementation that is subtly wrong. Structural endpoints and `Editor.log` can never tell you a result is unreadable.

Don't round-trip screenshots as base64 through tool results — it truncates. Save straight to a file and read the file:

```
curl -s "http://localhost:8787/screenshot?view=camera&width=1920&height=1080&format=png" -o shot.png
```

`view=camera` works in Edit mode and at any requested size; `view=game` needs Play mode and captures whatever size the Game view window happens to be — possibly a useless sliver. To inspect small detail, capture at 3840×2160 and crop (`sips -c <h> <w> --cropOffset <y> <x>` on macOS). **Any screenshot taken in Play mode leaves the Editor playing unless you explicitly stop it** — and a playing Editor silently disables every setup stage (see the Play-mode trap).

For pixel-adjacent facts that aren't about appearance (did an import fail, did a render happen), the project's actual `Logs/Editor.log` (in the project root, not `~/Library/Logs/Unity/Editor.log`, which can be stale) is still the right tool via `grep`/`tail`.

## The single biggest trap: AssetDatabase is not synchronous here

Unity's docs read as though `AssetDatabase.CreateAsset`, `PrefabUtility.SaveAsPrefabAsset`, `AssetDatabase.CreateFolder`, and `AssetDatabase.ImportAsset(..., ForceSynchronousImport)` take effect immediately. In a headless or unfocused Editor session driven by an outside process, this doesn't reliably hold *within a single method call*: `CreateAsset` followed immediately by `LoadAssetAtPath` for the same path can return `null`, even with force-synchronous flags. The write does land on disk — it's the in-memory `AssetDatabase` index that hasn't caught up yet.

The fix is architectural, not a workaround flag: **treat asset creation as a resumable state machine, one step per domain reload**, instead of one long method that creates several things in sequence and reads them back.

```csharp
[InitializeOnLoadMethod]
static void Setup()
{
    if (EditorApplication.isPlayingOrWillChangePlaymode) return; // don't run mid Play-mode transition

    var sprite = AssetDatabase.LoadAssetAtPath<Sprite>(SpritePath);
    if (sprite == null)
    {
        CreateSpriteAsset();     // writes the asset...
        return;                  // ...but don't try to use it yet — bail and let the NEXT reload see it
    }

    var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(PrefabPath);
    if (prefab == null)
    {
        CreatePrefab(PrefabPath, sprite); // sprite is now reliably loadable, because this is a fresh reload
        return;
    }

    // ...continue the chain: next stage only runs once everything above already exists.
}
```

Each guard (`if (x == null) { create x; return; }`) is a stage. The method is safe to call every reload — cheap no-ops once everything exists — and it naturally self-resumes wherever it left off. Don't try to be clever and create multiple new things in one pass; the second creation in the same call is exactly where the stale-index problem bites.

**Overwriting an existing asset destroys every live scene reference to it.** Same family of problem, opposite direction: `AssetDatabase.CreateAsset(mesh, path)` over a path that already exists does not update the object the scene is pointing at — it **destroys** it and writes a new one. The GUID in the `.meta` survives, so nothing looks wrong on disk, but every live `MeshFilter.sharedMesh` (or material/sprite slot) in the open scene that referenced it becomes `null` — and if anything saves the scene afterwards, the null is what gets written:

```yaml
-  m_Mesh: {fileID: 4300000, guid: a51c09b1…, type: 2}
+  m_Mesh: {fileID: 0}
```

This stays invisible for as long as some later stage happens to rebuild and re-wire the affected objects — and bites the first time an asset-building stage re-runs *without* that rebuild. Three defenses:

- **Prefer not to rewrite assets that are already correct.** If a stage builds several kinds of asset, guard each kind separately so retuning one does not rewrite the others:
  ```csharp
  bool lettersStale = !File.Exists(...) || MeshWidthDiffers(...);
  bool arrowsStale  = !File.Exists(...) || MeshWidthDiffers(...);
  if (lettersStale || arrowsStale) { if (lettersStale) {…} if (arrowsStale) {…} return; }
  ```
- **Pair every asset-rewriting stage with a repair stage** that re-points scene references which have gone null, driven by a table of object path → asset path. It costs nothing while references hold — and it must run *before* any stage that saves (see "Evolving an already-built scene" below).
- **Know the diagnostic**, because the symptom is invisible in the Hierarchy: `/object` shows `m_Mesh: null`, and `git diff` on the `.unity` file shows `m_Mesh: {fileID: 0}` appearing.

**Write guards in fall-through style, because features get added later.** A final stage written as `if (GameObject.Find("GameManager") != null) return;` reads fine today, but the moment a later session appends a stage after it, that early return blocks everything downstream and has to be restructured into `if (Find(...) == null) { Build(); return; }`. Use the fall-through form from the start for every stage, including the last one.

**Guards for inactive objects**: `GameObject.Find` only finds *active* objects. If a stage authors something deactivated (a hidden UI panel, a disabled template), its guard must be `Object.FindAnyObjectByType<MyComponent>(FindObjectsInactive.Include)` or the stage will happily rebuild a duplicate every reload. (Use `FindAnyObjectByType`, not `FindFirstObjectByType` — the latter is obsolete in Unity 6 and emits CS0618.)

**Forcing the next reload**: `EditorApplication.delayCall` and `EditorUtility.RequestScriptReload()` are worth trying first, but neither is guaranteed — observed sessions had `delayCall` never fire at all and `RequestScriptReload()` silently get stuck after a couple of chained reloads. The reliable fallback when a stage stops progressing: make a trivial textual edit to the script (a no-op comment) before calling `/compile` again — Unity then treats the file as changed and does a full fresh domain reload, which is what actually re-runs `[InitializeOnLoadMethod]` and lets the next stage see the previous stage's output.

**When a stage silently refuses to run, check Play mode before anything else** (this is the trap the checklist at the top of the workflow exists for). The `isPlayingOrWillChangePlaymode` guard at the top of `Setup()` turns the whole state machine into a no-op with *zero log output* while the game is running — and users leave Play mode on all the time. The failure signature: *clean compiles + real reloads (the bridge logs its startup line each time) + zero stage logs = Play mode, every time.* Do not start re-reading your own guards until one `/status` call has ruled it out. Users re-enter Play *between requests* to play-test what you just built — and **you** enter it yourself whenever you take a `view=game` screenshot and forget to stop — so check `/status` before every stage-driving burst, not just at session start. **Stopping Play is not enough by itself**: the domain reload *during* the play-exit transition still sees `isPlayingOrWillChangePlaymode == true`, so you need one more clean reload (trivial edit + `/compile`) after the Editor is back in Edit mode. Separately, `/compile` can occasionally answer a single opaque error mid-reload — that's transient; retry the same call.

**Folder existence**: don't trust `AssetDatabase.IsValidFolder`/`CreateFolder` across separate calls in a driven session either — it can go out of sync with the real filesystem and silently create `"Sprites 1"`, `"Sprites 2"`, etc. on repeated attempts. Check and create folders with plain `System.IO.Directory.Exists`/`CreateDirectory` against `Application.dataPath`-relative paths, then call `AssetDatabase.Refresh()` once to let Unity pick them up:

```csharp
static string ToAbsolutePath(string assetsRelativePath) =>
    Application.dataPath + assetsRelativePath.Substring("Assets".Length);

static bool FolderExists(string p) => Directory.Exists(ToAbsolutePath(p));
static void CreateFolder(string p) { Directory.CreateDirectory(ToAbsolutePath(p)); AssetDatabase.Refresh(); }
```

**Sub-assets are unreliable too**: `AssetDatabase.AddObjectToAsset(sprite, texture)` (nesting a Sprite inside its Texture2D asset) may not actually persist the sub-asset in this kind of session — confirmed by reading the raw `.asset` YAML and finding only the texture block. If a generated sprite silently has no visible sub-object, save texture and sprite as two independent asset files instead of nesting them.

## The compile-and-check loop

Driving stages means repeating one loop: force a reload, wait for it, read what the stage logged. Don't reassemble it from scratch each session — this is the recipe, with the sharp edges named:

1. `/status` — confirm `isPlaying: false` (the checklist item at the top of this document).
2. Capture `/console`'s `nextSince` cursor *before* the burst.
3. Append a newline to a `.cs` file to force the reload — `/compile` will not reload on `changed: false`.
4. Poll `/compile` until `state: "done"`. A `202` means still running, and the result is handed over exactly once — the *next* call after `done` starts a fresh compile.
5. Wait briefly before reading `/console`: `done` can arrive before the reload's log messages have landed, and an empty read looks exactly like "the stage did nothing".
6. Read `/console?since=<cursor>`. The paging is easy to get off by one — if a stage seems not to have run, re-read with `since = nextSince - 1` before believing it. Filter out the bridge's own per-reload startup line (e.g. `[Uplink] Serving …`).
7. **Clean up afterwards**: the forcing newlines accumulate at the end of the file and must be stripped before the change is done.

## Programmatic sprites: get the pixel scale right

`Sprite.Create(texture, rect, pivot, pixelsPerUnit)` — the `pixelsPerUnit` argument must match the actual pixel dimensions of the texture if you want the sprite's native size to equal a clean number of world units (typically 1 unit per texture, for a grid-based game). A common invisible-sprite bug: generating a tiny procedural texture (e.g. 4×4 px) and leaving `pixelsPerUnit` at the default of 100 — the sprite ends up 0.04×0.04 world units, technically rendered but imperceptible next to everything else. If a sprite "isn't showing up" but `/object` confirms `m_Sprite` is assigned, check `m_Size` and the pixels-per-unit math before assuming anything else is wrong.

## Wiring private serialized fields from Editor code

To set a private `[SerializeField]` reference (like a prefab slot) on a component from an Editor script, don't try to make the field public just to reach it — go through `SerializedObject`:

```csharp
var so = new SerializedObject(myComponent);
so.FindProperty("segmentPrefab").objectReferenceValue = segmentPrefab;
so.ApplyModifiedPropertiesWithoutUndo();
```

Building a prefab asset itself follows the same "build a temp instance, save it, discard the instance" shape:

```csharp
var go = new GameObject(name);
go.AddComponent<SpriteRenderer>().sprite = sprite;
PrefabUtility.SaveAsPrefabAsset(go, path);
Object.DestroyImmediate(go);
```

## Evolving an already-built scene: repair stages

Feature work after the initial build (visual overhauls, fixing artifacts the user spotted) goes through the same state machine: **append new fall-through stages that retrofit the existing content**. The original build functions won't re-run — their guards see the built scene and no-op — so also update them to produce the new end state directly, purely for the from-scratch rebuild path.

- A repair stage's guard detects the *old in-memory state*: a transform scale still at the old value, `Camera.main.orthographic` still true, a prefab root still carrying a `SpriteRenderer`, a renderer's `sharedMaterial.name` still being the old material. Cheap to check every reload, permanently quiet once repaired.
- Its paired deferred-save stage gates on the *old state still being in the scene file*: `File.ReadAllText(scenePath).Contains(...)` with the old serialized value (`"orthographic: 1"`, the old `m_LocalScale: {x: ..}` string) or the absence of a new asset's guid (`AssetDatabase.AssetPathToGUID(path)`). Same self-limiting property as the original save gates.
- Editing an existing repair stage in place beats appending another when the new fix *supersedes* it — loosen its guard threshold and update its save gate to the new target; append instead when the older stage's output remains a valid intermediate.

**A repair stage must run before any stage that saves.** A stage that repairs in-memory state belongs immediately after the stage that damages it and ahead of every stage that saves. Appending it at the end of the chain — the natural place for new stages — is the wrong place for this one: with the ordering *rewrite assets (references go null) → later stage saves → repair*, the save in the middle commits the damage to disk, and the repair is left undoing something already saved instead of preventing it. Where correct placement conflicts with sequential stage numbering, keep the number and move the code, with a comment saying why it stands out of order — the number records when it was added, the position records what it protects.

**How to write a save gate that cannot loop forever.** Three rules:

1. **Gate on the presence of the *new* state, not the absence of the old**, wherever the new state has a searchable signature — `AssetDatabase.AssetPathToGUID(path)` appearing in the scene file, a new object name. A gate on residual damage (e.g. `m_Mesh: {fileID: 0}` in the `.unity` file) is true forever if the scene legitimately contains that pattern elsewhere — anchor objects with renderers that draw nothing are common — and then it queues a save on every reload, permanently.
2. **Before writing a gate, ask whether the tested pattern is legitimately present elsewhere in the scene.** A `grep` of the `.unity` file answers it in seconds.
3. **Not every stage needs a paired disk-gated save.** If the stage's own guard is a reliable in-memory fact (a null reference is a fact about the scene, not about the disk), register the tick-deferred `SaveSceneOnce` (see "Saving the scene" below) from that stage directly and skip the paired stage — there is then no disk-side gate to get wrong.

To modify an existing prefab (the create-time pattern doesn't apply): `PrefabUtility.LoadPrefabContents(path)` → mutate the returned root (add/remove components and children, wire fields via `SerializedObject`) → `PrefabUtility.SaveAsPrefabAsset(root, path)` → `PrefabUtility.UnloadPrefabContents(root)`. To modify an existing material or other loose asset inside a stage: mutate it, then `EditorUtility.SetDirty(asset)` and `AssetDatabase.SaveAssets()` — safe in the same stage as scene edits, since nothing needs to read it back.

## Authoring UGUI from Editor code: use legacy Text with the built-in font

For HUDs and panels built by an editor setup script, prefer legacy `UnityEngine.UI.Text` over TextMeshPro: TMP wants its Essentials package imported via an interactive dialog you can't click through a bridge, while legacy Text just needs the built-in font — `Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf")` in Unity 6 (the old `"Arial.ttf"` name no longer resolves). A display-only overlay (score readouts, banners) needs no `EventSystem` at all — only add one (with `InputSystemUIInputModule` if the project uses the new Input System) when you actually need clickable/typable UI widgets; for arcade-style name entry, capturing `Keyboard.current.onTextInput` in a MonoBehaviour is simpler and skips the EventSystem entirely. The usual shape: one `ScreenSpaceOverlay` canvas with a `CanvasScaler` (`ScaleWithScreenSize`), children created as `new GameObject(name, typeof(RectTransform))` with anchors/pivot set to the same corner and offsets from it, wired to a display-only component via `SerializedObject` like any other private field.

## Keep editor-authored content separate from runtime gameplay state

A recurring point of confusion (for users too — "I don't see the player/enemies in the editor") is the difference between:

- **Editor-authored, persistent scene/asset content**: camera setup, walls/bounds, prefabs, a controller GameObject with its prefab references wired up. Build this via `[InitializeOnLoadMethod]` Editor code so it exists as real Hierarchy objects and asset files the user can inspect and tweak without pressing Play, and mark the scene dirty (`EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene())`) once it's built so the user knows to save it.
- **Runtime-only gameplay state**: anything that only makes sense once the game is running — a snake's moving body segments, spawned enemies, current score. Create this in `Awake()`/`Start()` on a `MonoBehaviour`, *not* in the Editor script. `Awake`/`Start` never run in Edit mode, so this content correctly does not appear until Play — that's expected behavior, not a bug, and worth explaining proactively if the user asks why the Hierarchy looks empty of "the actual game" in Edit mode.

Trying to force the second category into the first (e.g. pre-spawning the snake's segments in the Editor script so they're "visible") just duplicates the runtime spawn logic and fights the natural, correct split.

## Giving a 2D game 3D visuals

When the user wants a 2D game to "look 3D", asks for meshes/lighting/perspective instead of sprites, wants surface relief (engraving, grooves, bevels) on a head-on camera, or reports textures squeezed into dense stripes on some faces of a scaled cube, **read `references/3d-visuals.md` before writing any code**. It covers the mesh-swap recipe (built-in meshes, URP Lit materials, perspective framing math that preserves the orthographic view's extent), the consequences of leaving sprites behind (per-instance tinting via `MaterialPropertyBlock`, z-fighting between coplanar boxes, grayscale procedural textures), why depth cut into a face is invisible without leaned chamfer walls and contrast, why grid-shaped geometry should be a height field rather than hand-fitted walls, and the cube UV-tiling trap whose real fix is a custom mesh with world-sized per-face UVs — not any tiling value.

## Useful MCP verification patterns

- `/console` with an error filter, checked *before and after* a `/compile` or `/play`, tells you whether the change you just made introduced anything new — old historical errors from earlier iterations will still be in the log, so compare against a timestamp/checkpoint rather than treating "any error present" as a signal.
- `/scene` gives you the hierarchy — use it to confirm expected children exist (e.g. after entering Play mode, confirm N `Segment(Clone)` + 1 `Food(Clone)` under the controller object).
- `/object` on a specific path gives you exact serialized field values — this is your ground truth for "is this actually the size/color/position I intended." For these structural facts it beats a screenshot; for how the result *looks*, it says nothing (see "Verifying: structure vs appearance").
- `/refresh` has two distinct modes worth knowing: `{"scenes": false}` just reimports assets; `{"scenes": true, "discardUnsavedChanges": true}` reloads the scene from disk and throws away any in-memory-only edits. The latter is useful for resetting to a known-clean scene between iterations of a setup script, but remember it will also discard anything real the user hasn't saved — don't use it casually once you're past the iteration phase.
- Play-mode entry/exit (`/play` with `target: "play"`/`"stop"`) is itself a good verification step for runtime-only logic: enter Play, check `/console` for zero new errors, check `/scene`/`/object` for the runtime objects you expect, then stop.

## Saving the scene: the modal-dialog trap and the tick-deferred save

Editor-script-driven scene changes exist only in the Editor's in-memory scene until something saves the `.unity` file. Both obvious ways to handle that have failure modes observed in practice:

**Calling `EditorSceneManager.SaveScene` directly from `[InitializeOnLoadMethod]` can freeze the Editor.** The save completes and lands on disk, but Unity's file watcher may treat its own write to the open scene as an *external* change and raise a modal "scene has been changed on disk — reload?" dialog. The modal blocks the Editor main thread, so every MCP endpoint starts returning 504 timeouts — the bridge is dead until a human clicks a button (either button is safe: disk and memory are identical). You cannot dismiss it remotely; macOS UI scripting via `osascript` fails without assistive-access permission. If this happens, verify your work by grepping the scene YAML on disk (see below) and tell the user to dismiss the dialog — the work is done, only live verification is blocked.

**Leaving the scene merely dirty (`MarkSceneDirty`) and asking the user to press Cmd+S doesn't reliably happen either** — users press Play instead, see everything working, and reasonably conclude it's saved. It isn't: **Play mode runs the in-memory scene and never writes the `.unity` file**, so "I ran the app and it was all there" says nothing about persistence. The ground truth is `grep` on the scene file (expected object names as `m_Name:` entries, serialized references as non-zero `fileID`s) or `git status` showing the `.unity` file modified.

**The pattern that works — defer the save to an editor tick.** Save from a self-removing `EditorApplication.update` handler registered by the setup method, so the write happens *outside* the domain-reload callback; in the observed session this saved cleanly with no modal and the bridge stayed responsive throughout. Gate it on the scene file's actual disk content so it is self-limiting — it completes the one pending stage and can never silently auto-save unrelated user edits later; write the gate by the rules in "How to write a save gate that cannot loop forever" above. (`EditorApplication.delayCall` is not a substitute here: it simply never fired.)

```csharp
// Final stage: persist the panel once it exists in memory but not yet on disk.
var scene = SceneManager.GetActiveScene();
if (!File.ReadAllText(ToAbsolutePath(scene.path)).Contains("RecordsPanel"))
{
    EditorApplication.update += SaveSceneOnce;
}
// ...
static void SaveSceneOnce()
{
    EditorApplication.update -= SaveSceneOnce;
    EditorSceneManager.SaveScene(SceneManager.GetActiveScene());
}
```

If you've been iterating with `/refresh {"scenes": true, "discardUnsavedChanges": true}` along the way, remember it throws away exactly this kind of in-memory-only content — do a save (or remind the user) before anything that reloads the scene from disk.
