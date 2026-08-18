---
name: unity-game-design
description: Playbook for building or modifying Unity games/scenes through a Unity MCP bridge (endpoints like /compile, /console, /play, /scene, /object) that has no endpoint to directly create GameObjects or assets — content is built by writing [InitializeOnLoadMethod] Editor scripts run by Unity's compile/reload cycle. Covers the staged/resumable pattern that survives AssetDatabase's stale-index quirks (CreateAsset/SaveAsPrefabAsset returning null on immediate read-back), how CreateAsset over an existing path destroys the asset and nulls every live scene reference to it (m_Mesh: {fileID: 0} after a save), repair stages ordered ahead of saves, save gates that key on new state so they can't loop forever, the exact compile-and-check loop (POST /compile with force:true to re-run setup code without touching files, waiting on the read-only GET /compile a.k.a. compile_status so one poll too many can't start a build nobody asked for, POST again to collect the one-shot result, reading stage logs from the console page embedded in the done result, isPlaying and sceneDirty reported in responses — plus the older-bridge fallback of forcing newlines and /console since-cursor pitfalls), calling the bridge through its MCP tool (e.g. the guided list-endpoints/get-endpoint-details/call-endpoint flow) rather than raw curl, verifying structure via /scene and /object (narrowed with fields=/components=) vs appearance via screenshots written straight to disk with path= and cropped server-side with crop=, taken early, photographing what no scene camera shows via view=viewpoint (frame= auto-fits an object's renderer bounds with axis= picking the side, from=/at=/dir= poses freely, the response echoes the pose actually used, inactive subtrees answer a 400 naming inactivity rather than silently falling back to the main camera), reaching states that exist only behind actual play by scripting keyboard/mouse steps into the running game via POST /input and polling its read-only GET /input twin (play-mode-only, runInBackground switched on so an unfocused Editor still ticks, ~10fps background throttling that makes hold times matter, playModeEnded, verifying against a transform the game moved rather than input state, the endpoint vanishing from the spec when the Input System package is absent), saving the scene without the Editor-freezing "changed on disk" modal, why setup stages silently stop when Play mode is left on, why a played game is not a saved scene, giving a 2D game 3D visuals (mesh swaps, perspective framing math, chamfers/contrast for engraved detail, height-field meshes for grid geometry, the cube UV-tiling stripe trap), and keeping editor-authored content separate from runtime-only state. Use whenever the user works on a Unity project through an MCP/Uplink-style bridge, wants GameObjects/prefabs/sprites/scenes changed programmatically, wants a 2D game to "look 3D" or lettering/detail engraved into meshes, reports "nothing shows up until I press play", sees asset writes return null, sees scene references go null after re-running an asset stage, sees stages stall despite clean compiles, sees textures squeezed into stripes on some faces of a scaled cube, sees a bare POST rejected with 411 or a poll loop stall on 405 from GET /compile, watches a poll accidentally kick off an extra compile, hits bridge timeouts after a scene save, cannot visually verify something sitting outside the main camera's frame or inside an inactive subtree, needs to reach or screenshot a menu/screen/state that only exists after playing to it, sends input that queues but the game never advances a frame, or finds /input missing from the bridge's API spec.
---

# Unity Game Design via MCP

Most Unity MCP bridges expose *inspection and control* — `/status`, `/console`, `/compile`, `/play`, `/screenshot`, `/refresh`, `/scene`, `/object`, `/tests`, and on current Uplink builds `/input` — but not *creation*: there is usually no `/create-gameobject` or `/set-property` endpoint. The only way to build or modify scene/asset content is to **write real C# Editor code** into the project and let Unity's own compile/reload cycle execute it. You are not remote-controlling the Editor; you are patching the project's source and asset-generation logic, then asking Unity to run it. The MCP calls trigger that cycle and verify what happened — they are not themselves the mechanism of change.

## Call the bridge through MCP, not curl

When the session has an Uplink MCP tool — typically a guided tool like `unity-guided-api` (flow: `list-endpoints` → `get-endpoint-details`, which returns an acknowledgment token → `call-endpoint` with that token), or one MCP tool per endpoint (`compile`, `read_console`, `screenshot`, …) — **use it for every bridge call instead of shelling out to `curl` against `http://localhost:8787`**. The reasons are practical, not stylistic:

- The tool descriptions are generated from the *running* server's OpenAPI spec, so they describe the Uplink version actually installed — parameters, status codes, and behavioral notes this document may lag behind. When in doubt, `get-endpoint-details` is the ground truth.
- Raw-HTTP transport traps simply don't exist on the MCP path: the `411` a bare `curl -X POST` earns (the .NET listener rejects a `POST` without `Content-Length` before Uplink sees it) never happens through the adapter.
- The guided tool addresses endpoints by method + path, so everything in this document written as `POST /compile` or `GET /screenshot?view=camera&…` translates directly: the path is the endpoint, the query string becomes `queryParameters`, the JSON body goes in `body`.

Fall back to `curl` only when no Uplink MCP tool is connected in the session; the endpoint semantics described here apply unchanged either way.

## The core workflow

> **Before every burst of compile-and-check, confirm `isPlaying: false`.**
> Current Uplink builds report `isPlaying` in every `/compile` and `/refresh` response (and a reload
> that ran during play carries a `note` saying setup was suppressed); on bridges that don't, call
> `/status` first. Play mode turns the whole setup state machine into a silent no-op. The failure
> signature: *clean compiles + real reloads + zero stage logs = Play mode, every time.*
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

Don't round-trip screenshots as base64 through tool results — it truncates. Land the PNG on disk and read the file. Current Uplink builds do this in one `GET /screenshot` call: `path=` writes the file server-side (parent directories are created for you) and answers `{path, view, width, height}` with no image bytes crossing the transport, and `crop=x,y,w,h` (top-left origin, like every image tool) keeps just the region under inspection:

```
GET /screenshot?view=camera&width=3840&height=2160&path=/tmp/shot.png&crop=1600,900,640,360
```

(via the MCP tool these are ordinary `queryParameters`; the curl-fallback equivalent is the same URL against `http://localhost:8787`). On bridges without `path`, the image comes back in the response — through MCP that means base64 in the tool result, exactly the truncation this paragraph warns about, so there the curl fallback is genuinely the better tool: save the body to disk and crop client-side:

```
curl -s "http://localhost:8787/screenshot?view=camera&width=1920&height=1080&format=png" -o shot.png
```

`view=camera` works in Edit mode and at any requested size; `view=game` needs Play mode and captures whatever size the Game view window happens to be — possibly a useless sliver. To inspect small detail, capture at 3840×2160 and crop — via `crop=`, or `sips -c <h> <w> --cropOffset <y> <x>` on macOS when the bridge can't. **Any screenshot taken in Play mode leaves the Editor playing unless you explicitly stop it** — and a playing Editor silently disables every setup stage (see the Play-mode trap).

**When no camera in the scene shows the thing you built, photograph it from a viewpoint of your own — don't ship it visually unverified.** Current Uplink builds accept `view=viewpoint`: Uplink creates a hidden temporary camera for the shot (nothing enters the hierarchy, nothing is dirtied), so "the object sits outside the main camera's frustum" stops being a reason a visual change goes out checked only through `/object` and a `.unity` diff. Two ways to aim it:

- `frame=/Path/To/Object` (an object path exactly as `/scene` reports it) — Uplink measures the subtree's renderer bounds and stands back far enough to fit them; `axis=front|back|left|right|top|bottom` picks the side to look from (front, −Z, is the default). This is the one-call answer to "show me this object".
- `from=x,y,z` with `at=x,y,z` or `dir=x,y,z` — an explicit pose, for composed shots `frame` can't express. `fov` (vertical degrees, default 60) or `ortho=<half-height>` for an orthographic render; `near`/`far` are worked out for you when left alone (from the fit when framing, deliberately generous with `from`) — set them only when a huge or tiny scene clips wrong.

`width`, `height`, `crop`, `path` and `format` compose unchanged. What to know before the first call:

- **The response echoes the pose actually used** (`from`, `at`, `fov`/`ortho`). With `frame` you didn't choose those numbers — so when a shot frames badly, nudge it by editing the echoed numbers instead of guessing again.
- **It never falls back.** A `frame` path that names nothing, or a subtree with no enabled renderers, is a `400` — never a quiet photo of the main camera. The common cause of "no enabled renderers" is a subtree authored inactive (a menu that only activates in Play mode), and the error message says so. Don't "fix" that with `SetActive` from an editor script just to photograph it — that dirties the scene with no way to un-dirty it, and a later save persists a change nobody asked for. Enter Play mode, where the subtree is active, and take the viewpoint shot there — or drive the game into showing it itself (see "Driving the game's own inputs" below), which is the truer picture anyway.
- **The fit uses `Renderer.bounds`**: colliders, empty transforms and disabled renderers don't influence the framing — what is fitted is what is drawn.
- **Rendering settings are copied from `Camera.main`** (clear flags, background color, culling mask), so the shot resembles what the game would draw; with no main camera the fallback is solid black with every layer visible.
- Gizmos, overlays and handles are absent, same as `view=camera`; `view=game` in Play mode is the answer when those matter.

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

**Forcing the next reload**: on current Uplink builds, call `/compile` with `{"force": true}` — it reloads the domain even when nothing changed, which is exactly what re-runs `[InitializeOnLoadMethod]` and lets the next stage see the previous stage's output. The result reports `forced` alongside `changed`, so a forced reload and a real rebuild read differently. On bridges without `force`, don't reach for `EditorApplication.delayCall` or `EditorUtility.RequestScriptReload()` from your own code — observed sessions had `delayCall` never fire at all and `RequestScriptReload()` silently get stuck after a couple of chained reloads. The reliable fallback there: make a trivial textual edit to the script (a no-op comment) before calling `/compile` again — Unity then treats the file as changed and does a full fresh domain reload.

**When a stage silently refuses to run, check Play mode before anything else** (this is the trap the checklist at the top of the workflow exists for). The `isPlayingOrWillChangePlaymode` guard at the top of `Setup()` turns the whole state machine into a no-op with *zero log output* while the game is running — and users leave Play mode on all the time. The failure signature: *clean compiles + real reloads (the bridge logs its startup line each time) + zero stage logs = Play mode, every time.* Do not start re-reading your own guards until you've ruled it out — on current Uplink builds every `/compile` and `/refresh` response reports `isPlaying`, and a reload that ran during play carries a `note` saying setup was suppressed; on older bridges, one `/status` call answers it. Users re-enter Play *between requests* to play-test what you just built — and **you** enter it yourself whenever you take a `view=game` screenshot or drive the game with `/input` and forget to stop — so check `isPlaying` on every stage-driving burst, not just at session start. **Stopping Play is not enough by itself**: the domain reload *during* the play-exit transition still sees `isPlayingOrWillChangePlaymode == true`, so you need one more clean reload (`/compile` with `{"force": true}`, or trivial edit + `/compile` on older bridges) after the Editor is back in Edit mode. Separately, `/compile` can occasionally answer a single opaque error mid-reload — that's transient; retry the same call.

**Folder existence**: don't trust `AssetDatabase.IsValidFolder`/`CreateFolder` across separate calls in a driven session either — it can go out of sync with the real filesystem and silently create `"Sprites 1"`, `"Sprites 2"`, etc. on repeated attempts. Check and create folders with plain `System.IO.Directory.Exists`/`CreateDirectory` against `Application.dataPath`-relative paths, then call `AssetDatabase.Refresh()` once to let Unity pick them up:

```csharp
static string ToAbsolutePath(string assetsRelativePath) =>
    Application.dataPath + assetsRelativePath.Substring("Assets".Length);

static bool FolderExists(string p) => Directory.Exists(ToAbsolutePath(p));
static void CreateFolder(string p) { Directory.CreateDirectory(ToAbsolutePath(p)); AssetDatabase.Refresh(); }
```

**Sub-assets are unreliable too**: `AssetDatabase.AddObjectToAsset(sprite, texture)` (nesting a Sprite inside its Texture2D asset) may not actually persist the sub-asset in this kind of session — confirmed by reading the raw `.asset` YAML and finding only the texture block. If a generated sprite silently has no visible sub-object, save texture and sprite as two independent asset files instead of nesting them.

## The compile-and-check loop

Driving stages means repeating one loop: force a reload, wait for it, read what the stage logged. On current Uplink builds the shape is **`POST` to start, `GET` to wait, `POST` to collect**:

1. `POST /compile` (the `compile` tool) — with `{"force": true}` when no script changed (it reloads the domain anyway, which is what re-runs setup code); after a real edit, `{}` is enough. It answers `202`: the run has started. (Curl-fallback trap only: **a bare `POST` fails with `411`** before Uplink even sees it — the .NET listener rejects any `POST` without a `Content-Length`, and `curl -X POST <url>` alone sends none, so send a body, `-d '{}'`. Through MCP this can't happen.)
2. Wait by polling `GET /compile` (the `compile_status` tool), not `POST`. The read never starts a run and never consumes a result, so any number of them is safe by construction — whereas with `POST`-only polling, one poll too many after the result is delivered silently starts a build nobody asked for, and nothing in the response distinguishes "your result" from "a run you just started by asking". `GET` answers `202` while `state: "compiling"`, and `200` with `state: "done"` (a finished result is waiting for a `POST` to take delivery) or `state: "idle"` (nothing running, nothing pending — `idle` still carries the last run's errors and duration). Everything a `GET` returns is marked `stale: true`; that's the reminder that the one-shot hand-over belongs to `POST`. Bridges older than this split answer `405` to `GET /compile` — poll with `POST` there and treat the response after `done` accordingly.
3. When `GET` reports `done`, call `POST /compile` once more to take delivery — the result is handed over exactly once, and the *next* `POST` after that starts a fresh compile. `done` means the *domain reload* finished too, not just the build: the run stays `202` until a couple of quiet ticks after the reload, so an empty result can no longer masquerade as "the stage did nothing".
4. Read the stage's output from the `console` page embedded in the `done` result — everything the run logged, already stripped of the bridge's own `[Uplink]` chatter. No cursor capture, no `since` arithmetic. The page caps at 100 entries; if it says `truncated`, follow its `nextSince` into `/console` for the rest. (The same page rides along on a `GET` that observes `done` — usable in a pinch, but collecting via `POST` keeps the cycle in its expected rhythm: leaving a delivered-by-`GET` result unclaimed means your *next* burst's opening `POST` hands over that stale result instead of starting the run you wanted.)
5. Check `isPlaying` in the same response — `errors: 0` with an empty `console` plus `isPlaying: true` is the Play-mode trap, and the response's `note` will say so.

**On older bridges** without `force` and the embedded console page, the loop is manual, with the sharp edges named:

1. `/status` — confirm `isPlaying: false` (the checklist item at the top of this document).
2. Capture `/console`'s `nextSince` cursor *before* the burst.
3. Append a newline to a `.cs` file to force the reload — `/compile` will not reload on `changed: false`.
4. Poll `/compile` until `state: "done"`.
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
- `/object` on a specific path gives you exact serialized field values — this is your ground truth for "is this actually the size/color/position I intended." Current Uplink builds accept `components=` and `fields=` to narrow the response to just the values under inspection instead of the whole component dump. For these structural facts it beats a screenshot; for how the result *looks*, it says nothing (see "Verifying: structure vs appearance").
- `/refresh` has two distinct modes worth knowing: `{"scenes": false}` just reimports assets; `{"scenes": true, "discardUnsavedChanges": true}` reloads the scene from disk and throws away any in-memory-only edits. The latter is useful for resetting to a known-clean scene between iterations of a setup script, but remember it will also discard anything real the user hasn't saved — don't use it casually once you're past the iteration phase.
- Play-mode entry/exit (`/play` with `target: "play"`/`"stop"`) is itself a good verification step for runtime-only logic: enter Play, check `/console` for zero new errors, check `/scene`/`/object` for the runtime objects you expect, then stop. On current Uplink builds this extends past the opening state: drive the game with `/input` (next section) to reach the states behind actual play — a menu selection, a scored point, an emptied list — and verify those too.

## Driving the game's own inputs: /input

Some states exist only after somebody has played to them — a menu two screens in, a rule that only shows once a list is empty, anything gated behind a physics interaction. Current Uplink builds close this with `POST /input` (the `play_input` tool): a script of keyboard/mouse steps played into the running game, so you reach the state, then photograph it (`view=viewpoint` or `view=game`) or read it (`/scene`, `/object`). On bridges without it, the honest fallback is what it always was: ask the user to play to the state, or verify structurally and say plainly what went visually unverified.

```json
{"steps": [
  {"key": "space", "hold": 0.05},
  {"key": "leftArrow", "hold": 1.2},
  {"wait": 0.5},
  {"move": [960, 540]},
  {"click": "left"}
]}
```

- **Play mode only.** Outside it there is no player loop to receive events; the call is a `400` telling you to call `set_play_mode` first. (This is Uplink's general convention: a well-formed request in the wrong Editor state answers `400`, and the *message* names the state needed and the tool that gets there — read the message rather than branching on the code.) And driving input means being in Play mode, with everything the Play-mode trap says: setup stages are suppressed the whole time, and after stopping you need one more clean forced reload before stage-driving resumes.
- **It's a cycle endpoint with the same rhythm as `/compile`**: `POST` starts the script and answers `202`; poll with the read-only `GET /input` (the `input_status` tool — `running` with `stepsDelivered` of `steps`, `done`, or `idle`, everything marked `stale: true`); on `done`, one more `POST` takes delivery of the outcome exactly once. Steps sent while a script is playing are **not queued** — the response's `note` says so; wait the current script out.
- **Check `playModeEnded` in the result.** A game can quit mid-script (game over, a quit menu item), and a script that ran into a stopped Editor must not read as success. Play mode ending is also a domain reload, so the play-exit reload rules apply afterwards.
- **Control names are the Input System's own paths** (`<Keyboard>/space`, `<Mouse>/leftButton`), with short forms (`space`, `leftArrow`, `a`, `left`/`right`/`middle`) accepted as sugar. An unknown name fails the call rather than being delivered to nothing. Pointer `move` coordinates are pixels from the **top-left** (the same convention as `crop`), and every response reports `gameView` — the pixel size of the surface those coordinates land on — so aim using that, not the size you asked a screenshot for.
- **Starting a script turns on `Application.runInBackground`** — the runtime property, not the Player setting: nothing is written to the project, and it resets when Play mode ends. It exists because a Unity player does not tick at all while the Editor isn't the foreground application, which is the *normal* condition when you're driving it from another window — without it the events queue perfectly and the game never runs a frame to read them. It stays on after the script so that a following screenshot shows a live frame rather than a frozen one. But background frames are Editor-throttled (≈10fps observed): the schedule honors wall-clock time, not frames, so the game gets few frames to react in. That's why `hold` defaults to 0.05s — long enough to put press and release on different frames even at background rates. Don't shorten it, and don't design scripts that need frame-precise timing.
- **Verify against something the game itself moved.** After a script, read a transform the gameplay code writes (`/object` on the paddle, the ball) — not input state re-read from the editor side, which sees the *editor's* device buffer, not the player's, and reads as "input is blocked" when input is fine. And confirm you're reading the object that's actually active in the current mode — a menu's paddle is not the gameplay paddle.
- **If `/input` is missing from the spec entirely**, the project lacks the Input System package (`com.unity.inputsystem`): the endpoint deliberately unregisters itself rather than exist and answer `500`. Check the package, not the code. The legacy `Input` manager is not a supported backend and can't be, without the project cooperating.

Scripts are bounded (200 steps, 30s per hold/wait, 300s total), so drive a long journey as several scripts, reading state between them — which is better practice anyway, since each read tells you whether the previous leg actually landed.

## Saving the scene: the modal-dialog trap and the tick-deferred save

Editor-script-driven scene changes exist only in the Editor's in-memory scene until something saves the `.unity` file. Both obvious ways to handle that have failure modes observed in practice:

**Calling `EditorSceneManager.SaveScene` directly from `[InitializeOnLoadMethod]` can freeze the Editor.** The save completes and lands on disk, but Unity's file watcher may treat its own write to the open scene as an *external* change and raise a modal "scene has been changed on disk — reload?" dialog. The modal blocks the Editor main thread, so every MCP endpoint starts returning 504 timeouts — the bridge is dead until a human clicks a button (either button is safe: disk and memory are identical). You cannot dismiss it remotely; macOS UI scripting via `osascript` fails without assistive-access permission. If this happens, verify your work by grepping the scene YAML on disk (see below) and tell the user to dismiss the dialog — the work is done, only live verification is blocked.

**Leaving the scene merely dirty (`MarkSceneDirty`) and asking the user to press Cmd+S doesn't reliably happen either** — users press Play instead, see everything working, and reasonably conclude it's saved. It isn't: **Play mode runs the in-memory scene and never writes the `.unity` file**, so "I ran the app and it was all there" says nothing about persistence. Current Uplink builds surface this directly — `/status` reports `sceneDirty` (and `dirtyScenes` when several scenes are open), the cheap first check whenever work must be saved and not merely look right. The ground truth remains `grep` on the scene file (expected object names as `m_Name:` entries, serialized references as non-zero `fileID`s) or `git status` showing the `.unity` file modified.

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
