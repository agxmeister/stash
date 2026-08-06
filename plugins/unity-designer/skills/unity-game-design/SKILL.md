---
name: unity-game-design
description: Playbook for building or modifying Unity games/scenes through a Unity MCP bridge (endpoints like /compile, /console, /play, /scene, /object) that has no endpoint to directly create GameObjects or assets — content is built by writing [InitializeOnLoadMethod] Editor scripts run by Unity's compile/reload cycle. Covers the staged/resumable pattern that survives AssetDatabase's stale-index quirks (CreateAsset/SaveAsPrefabAsset returning null on immediate read-back), saving the scene without the Editor-freezing "changed on disk" modal, why setup stages silently stop when Play mode is left on, why a played game is not a saved scene, and keeping editor-authored content separate from runtime-only state. Use whenever the user works on a Unity project through an MCP/Uplink-style bridge, wants GameObjects/prefabs/sprites/scenes changed programmatically, reports "nothing shows up until I press play", sees asset writes return null, sees stages stall despite clean compiles, or hits bridge timeouts after a scene save.
---

# Unity Game Design via MCP

Most Unity MCP bridges expose *inspection and control* — `/status`, `/console`, `/compile`, `/play`, `/screenshot`, `/refresh`, `/scene`, `/object`, `/tests` — but not *creation*: there is usually no `/create-gameobject` or `/set-property` endpoint. The only way to build or modify scene/asset content is to **write real C# Editor code** into the project and let Unity's own compile/reload cycle execute it. You are not remote-controlling the Editor; you are patching the project's source and asset-generation logic, then asking Unity to run it. The MCP calls trigger that cycle and verify what happened — they are not themselves the mechanism of change.

## The core workflow

1. Write or edit an Editor-only script (under an `Editor/` folder) that performs the desired creation/setup, guarded by `[InitializeOnLoadMethod]` so it runs automatically on every domain reload.
2. Trigger a recompile via the MCP `/compile` endpoint (or equivalent).
3. Verify structurally via `/console` (errors since last checkpoint), `/scene` (hierarchy/children), and `/object` (specific field values) — see "Verifying without screenshots" below.
4. Repeat, editing the script further, until the desired state exists.

Don't reach for base64 screenshot round-tripping to check visual results — decoding/re-encoding large images by hand through Write/Bash is unreliable and truncates. Trust the structural endpoints instead: read `m_Sprite`, `m_LocalScale`, `m_Color`, `m_Size`, `m_LocalPosition` etc. directly. If you genuinely need pixel-level confirmation, use the project's actual `Logs/Editor.log` (in the project root, not `~/Library/Logs/Unity/Editor.log`, which can be stale) via `grep`/`tail` rather than screenshots.

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

**Write guards in fall-through style, because features get added later.** A final stage written as `if (GameObject.Find("GameManager") != null) return;` reads fine today, but the moment a later session appends a stage after it, that early return blocks everything downstream and has to be restructured into `if (Find(...) == null) { Build(); return; }`. Use the fall-through form from the start for every stage, including the last one.

**Guards for inactive objects**: `GameObject.Find` only finds *active* objects. If a stage authors something deactivated (a hidden UI panel, a disabled template), its guard must be `Object.FindAnyObjectByType<MyComponent>(FindObjectsInactive.Include)` or the stage will happily rebuild a duplicate every reload. (Use `FindAnyObjectByType`, not `FindFirstObjectByType` — the latter is obsolete in Unity 6 and emits CS0618.)

**Forcing the next reload**: `EditorApplication.delayCall` and `EditorUtility.RequestScriptReload()` are worth trying first, but neither is guaranteed — observed sessions had `delayCall` never fire at all and `RequestScriptReload()` silently get stuck after a couple of chained reloads. The reliable fallback when a stage stops progressing: make a trivial textual edit to the script (a no-op comment) before calling `/compile` again — Unity then treats the file as changed and does a full fresh domain reload, which is what actually re-runs `[InitializeOnLoadMethod]` and lets the next stage see the previous stage's output.

**When a stage silently refuses to run, check Play mode before anything else.** The `isPlayingOrWillChangePlaymode` guard at the top of `Setup()` turns the whole state machine into a no-op with *zero log output* while the game is running — and users leave Play mode on all the time. The symptom: `/compile` reports `done`/`changed: true`, reloads demonstrably happen, yet no stage logs appear. One `/status` call showing `isPlaying: true` explains it instantly — make that the first diagnostic, and check it at the start of every session too. Stopping Play is not enough by itself: the domain reload *during* the play-exit transition still sees `isPlayingOrWillChangePlaymode == true`, so you need one more clean reload (trivial edit + `/compile`) after the Editor is back in Edit mode.

**Folder existence**: don't trust `AssetDatabase.IsValidFolder`/`CreateFolder` across separate calls in a driven session either — it can go out of sync with the real filesystem and silently create `"Sprites 1"`, `"Sprites 2"`, etc. on repeated attempts. Check and create folders with plain `System.IO.Directory.Exists`/`CreateDirectory` against `Application.dataPath`-relative paths, then call `AssetDatabase.Refresh()` once to let Unity pick them up:

```csharp
static string ToAbsolutePath(string assetsRelativePath) =>
    Application.dataPath + assetsRelativePath.Substring("Assets".Length);

static bool FolderExists(string p) => Directory.Exists(ToAbsolutePath(p));
static void CreateFolder(string p) { Directory.CreateDirectory(ToAbsolutePath(p)); AssetDatabase.Refresh(); }
```

**Sub-assets are unreliable too**: `AssetDatabase.AddObjectToAsset(sprite, texture)` (nesting a Sprite inside its Texture2D asset) may not actually persist the sub-asset in this kind of session — confirmed by reading the raw `.asset` YAML and finding only the texture block. If a generated sprite silently has no visible sub-object, save texture and sprite as two independent asset files instead of nesting them.

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

## Authoring UGUI from Editor code: use legacy Text with the built-in font

For HUDs and panels built by an editor setup script, prefer legacy `UnityEngine.UI.Text` over TextMeshPro: TMP wants its Essentials package imported via an interactive dialog you can't click through a bridge, while legacy Text just needs the built-in font — `Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf")` in Unity 6 (the old `"Arial.ttf"` name no longer resolves). A display-only overlay (score readouts, banners) needs no `EventSystem` at all — only add one (with `InputSystemUIInputModule` if the project uses the new Input System) when you actually need clickable/typable UI widgets; for arcade-style name entry, capturing `Keyboard.current.onTextInput` in a MonoBehaviour is simpler and skips the EventSystem entirely. The usual shape: one `ScreenSpaceOverlay` canvas with a `CanvasScaler` (`ScaleWithScreenSize`), children created as `new GameObject(name, typeof(RectTransform))` with anchors/pivot set to the same corner and offsets from it, wired to a display-only component via `SerializedObject` like any other private field.

## Keep editor-authored content separate from runtime gameplay state

A recurring point of confusion (for users too — "I don't see the player/enemies in the editor") is the difference between:

- **Editor-authored, persistent scene/asset content**: camera setup, walls/bounds, prefabs, a controller GameObject with its prefab references wired up. Build this via `[InitializeOnLoadMethod]` Editor code so it exists as real Hierarchy objects and asset files the user can inspect and tweak without pressing Play, and mark the scene dirty (`EditorSceneManager.MarkSceneDirty(SceneManager.GetActiveScene())`) once it's built so the user knows to save it.
- **Runtime-only gameplay state**: anything that only makes sense once the game is running — a snake's moving body segments, spawned enemies, current score. Create this in `Awake()`/`Start()` on a `MonoBehaviour`, *not* in the Editor script. `Awake`/`Start` never run in Edit mode, so this content correctly does not appear until Play — that's expected behavior, not a bug, and worth explaining proactively if the user asks why the Hierarchy looks empty of "the actual game" in Edit mode.

Trying to force the second category into the first (e.g. pre-spawning the snake's segments in the Editor script so they're "visible") just duplicates the runtime spawn logic and fights the natural, correct split.

## Useful MCP verification patterns

- `/console` with an error filter, checked *before and after* a `/compile` or `/play`, tells you whether the change you just made introduced anything new — old historical errors from earlier iterations will still be in the log, so compare against a timestamp/checkpoint rather than treating "any error present" as a signal.
- `/scene` gives you the hierarchy — use it to confirm expected children exist (e.g. after entering Play mode, confirm N `Segment(Clone)` + 1 `Food(Clone)` under the controller object).
- `/object` on a specific path gives you exact serialized field values — this is your ground truth for "is this actually the size/color/position I intended," far more trustworthy than a screenshot in this workflow.
- `/refresh` has two distinct modes worth knowing: `{"scenes": false}` just reimports assets; `{"scenes": true, "discardUnsavedChanges": true}` reloads the scene from disk and throws away any in-memory-only edits. The latter is useful for resetting to a known-clean scene between iterations of a setup script, but remember it will also discard anything real the user hasn't saved — don't use it casually once you're past the iteration phase.
- Play-mode entry/exit (`/play` with `target: "play"`/`"stop"`) is itself a good verification step for runtime-only logic: enter Play, check `/console` for zero new errors, check `/scene`/`/object` for the runtime objects you expect, then stop.

## Saving the scene: the modal-dialog trap and the tick-deferred save

Editor-script-driven scene changes exist only in the Editor's in-memory scene until something saves the `.unity` file. Both obvious ways to handle that have failure modes observed in practice:

**Calling `EditorSceneManager.SaveScene` directly from `[InitializeOnLoadMethod]` can freeze the Editor.** The save completes and lands on disk, but Unity's file watcher may treat its own write to the open scene as an *external* change and raise a modal "scene has been changed on disk — reload?" dialog. The modal blocks the Editor main thread, so every MCP endpoint starts returning 504 timeouts — the bridge is dead until a human clicks a button (either button is safe: disk and memory are identical). You cannot dismiss it remotely; macOS UI scripting via `osascript` fails without assistive-access permission. If this happens, verify your work by grepping the scene YAML on disk (see below) and tell the user to dismiss the dialog — the work is done, only live verification is blocked.

**Leaving the scene merely dirty (`MarkSceneDirty`) and asking the user to press Cmd+S doesn't reliably happen either** — users press Play instead, see everything working, and reasonably conclude it's saved. It isn't: **Play mode runs the in-memory scene and never writes the `.unity` file**, so "I ran the app and it was all there" says nothing about persistence. The ground truth is `grep` on the scene file (expected object names as `m_Name:` entries, serialized references as non-zero `fileID`s) or `git status` showing the `.unity` file modified.

**The pattern that works — defer the save to an editor tick.** Save from a self-removing `EditorApplication.update` handler registered by the setup method, so the write happens *outside* the domain-reload callback; in the observed session this saved cleanly with no modal and the bridge stayed responsive throughout. Gate it on the scene file's actual disk content so it is self-limiting — it completes the one pending stage and can never silently auto-save unrelated user edits later. (`EditorApplication.delayCall` is not a substitute here: it simply never fired.)

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
