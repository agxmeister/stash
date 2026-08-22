# Older bridges: fallbacks and sharp edges

Everything in SKILL.md assumes a current Uplink build. This file is the playbook for bridges that predate those capabilities — and for sessions with no Uplink MCP tool at all, where the transport is raw HTTP.

How to recognize an older bridge (any one of these):

- `GET /compile` answers `405` (no read-only status twin).
- `POST /compile` with `{"force": true}` doesn't reload when nothing changed.
- `/screenshot` has no `path=`/`crop=` parameters — the image comes back in the response body.
- `/status` doesn't report `sceneDirty`; `/compile` responses don't report `isPlaying`.

(`/input` missing is **not** by itself a sign of age — on current Uplink it also means the project lacks the Input System package. Check the package first; see SKILL.md.)

## Raw HTTP when no MCP tool is connected

Endpoint semantics are identical to what SKILL.md documents; only the transport traps change:

- **A bare `POST` fails with `411`** before Uplink even sees it — the .NET listener rejects any `POST` without a `Content-Length`, and `curl -X POST <url>` alone sends none. Always send a body: `-d '{}'`.
- Save screenshots straight to disk with `-o` — never let image bytes ride through a tool result.

## Forcing a domain reload without `force: true`

`/compile` will not reload on `changed: false`, and the in-Editor alternatives don't work in a driven session: `EditorApplication.delayCall` was observed to never fire at all, and `EditorUtility.RequestScriptReload()` silently got stuck after a couple of chained reloads. The reliable fallback: **make a trivial textual edit** (a no-op comment or newline) to a `.cs` file before calling `/compile` again — Unity then treats the file as changed and does a full fresh domain reload.

**Clean up afterwards**: the forcing newlines accumulate at the end of the file and must be stripped before the change is done.

## The manual compile-and-check loop

Without `force`, the read-only `GET /compile`, and the console page embedded in the result, the loop is manual, with the sharp edges named:

1. `/status` — confirm `isPlaying: false` (these bridges don't report it in `/compile` responses, so this is the only check).
2. Capture `/console`'s `nextSince` cursor *before* the burst (see the cursor pitfalls below).
3. Append a newline to a `.cs` file to force the reload.
4. Poll `/compile` until `state: "done"` — with `POST`, since `GET` answers `405`. **One poll too many after the result is delivered silently starts a build nobody asked for**, and nothing in the response distinguishes "your result" from "a run you just started by asking". Count your polls and stop at `done`.
5. Wait briefly before reading `/console`: `done` can arrive before the reload's log messages have landed, and an empty read looks exactly like "the stage did nothing".
6. Read `/console?since=<cursor>`. The paging is easy to get off by one — if a stage seems not to have run, re-read with `since = nextSince - 1` before believing it. Filter out the bridge's own per-reload startup line (e.g. `[Uplink] Serving …`).
7. **Clean up afterwards**: strip the forcing newlines.

## Screenshots without `path=`

The image comes back in the response — through MCP that means base64 in a tool result, which truncates. Here the curl fallback is genuinely the better tool even when an MCP tool is connected: save the body to disk and crop client-side.

```
curl -s "http://localhost:8787/screenshot?view=camera&width=1920&height=1080&format=png" -o shot.png
```

To inspect small detail, capture at 3840×2160 and crop client-side — `sips -c <h> <w> --cropOffset <y> <x>` on macOS.

## Without `/input`

The honest fallback is what it always was: ask the user to play to the state you need, or verify structurally (`/scene`, `/object`) and say plainly what went visually unverified.

## Without `sceneDirty` in `/status`

The ground truth for "did the scene actually save" is `grep` on the scene file (expected object names as `m_Name:` entries, serialized references as non-zero `fileID`s) or `git status` showing the `.unity` file modified. This works on every bridge — SKILL.md recommends it as the final check even on current builds.
