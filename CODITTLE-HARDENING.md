# Codittle IDE — robustness & data-safety hardening

Improvement proposals for **Codittle** (the open-source T24 IDE), derived from real
incidents and observations at a T24 **R18 (TAFC)** site (June–July 2026). Each item is
written so it can seed an upstream GitHub issue/PR: *Problem → Evidence → Impact → Proposal*.

The headline finding: **Codittle keeps critical state (SSH connections, project graphs,
deploy history) only in an embedded PGlite database that has corrupted at least twice, and
on corruption it silently starts a fresh empty database with no warning and no recovery
path — even though the on-disk `.codittle/` metadata could fully reconstruct it.**

Severity: **P0** data-loss / silent · **P1** major reliability · **P2** quality-of-life.

| # | Title | Sev |
|---|---|---|
| 1 | DB corruption causes silent loss of connections + project state | **P0** |
| 2 | On-disk `.codittle/` not used as source of truth to rebuild the DB | **P0** |
| 3 | No automatic backups of the embedded database | **P0** |
| 4 | Connections have no export/import (DB-only, encrypted, unportable) | **P1** |
| 5 | Workspace identity tied to root path → silent new empty workspace | **P1** |
| 6 | Explorer/file-tree can't rebuild from local files (needs server re-pull) | **P1** |
| 7 | Project discovery scans helper folders as projects (no ignore) | **P2** |
| 8 | Cloud-synced workspace (OneDrive) interacts badly with path-hash identity | **P2** |
| 9 | Per-file `remoteHost`, no project-level env binding | **P2** |
| 10 | Password-encryption key scope undocumented / not portable | **P2** |
| 11 | No built-in recovery/diagnostics (`codittle doctor`) | **P2** |
| 12 | Desktop shell panic-crashes (0xc0000409) instead of recovering a dead backend | **P1** |
| 13 | Sidecar runs on system Node from PATH, not the bundled runtime | **P1** |
| 14 | "Frozen-but-open" window: renderer hangs mid-activation, no watchdog (backend healthy) | **P1** |
| 15 | Claude-updater patch anchors use `\w+` — `$`-aliases in newer minified bundles cause false drift, updates held back | **P2** |
| 16 | Built-in screenshot (capture overlay) deadlocks the desktop shell — sync Tauri command does grab/OCR + window ops on the main thread | **P1** |

---

## Observed architecture (context for all items)

- **DB:** per-workspace **PGlite** (Postgres-in-WASM) at
  `%LOCALAPPDATA%\Codittle\workspaces\<16-hex-id>\pgdata`.
- **Tables seen:** `t24_banks`, `t24_release_streams`, `t24_stream_envs` (connections —
  `host, port, username, password_enc, ssh_home, remote_roots, exec_setup, stream_id`),
  `projects` (`name, target, project_subdir, release_stream_id, meta`), `project_graphs`
  (`package_tree, nodes, edges, file_logic_*, node_body_*, …`), `project_versions`,
  `sessions`, `users`, `schema_migrations`.
- **On-disk per project** (intact, NOT in the DB): `.codittle/versions.json` (tracked files
  with `remotePath`, `remoteHost`, `currentVersion`, full `history[]` of deploys),
  `.codittle/folder-mappings.json` (`workingBp`/`projectBp`), `.codittle/file-types.json`.
- **Workspace metadata at root:** `Codittle/.codittle/ofs-library.json` (saved OFS messages).

---

## P0

### 1. DB corruption causes silent loss of connections + project state
**Problem.** When the PGlite database corrupts, Codittle renames the data dir to
`pgdata.corrupt-<unix-ts>` and **starts a brand-new empty database**, with no notification.
**Evidence.** One workspace contained **two** quarantine dirs —
`pgdata.corrupt-1781617592` (2026-06-16) and `pgdata.corrupt-1782300525` (2026-06-24). After
the 24-Jun event the live DB had **0 connections** (down from 11) while projects had been
re-scanned from folders. Both corrupt dirs are unreadable (PGlite aborts: `Aborted()`).
**Impact.** All SSH connections (incl. credentials), project graphs/file trees, and deploy
history disappear from the UI with no warning; the user assumes total data loss.
**Proposal.**
- Detect corruption explicitly and **warn the user** ("workspace DB was corrupted and
  reset; your previous data is preserved in `pgdata.corrupt-…`").
- Offer **one-click restore** from the newest automatic backup (item 3) or, failing that,
  an attempt to repair the quarantined dir (WAL replay / `pg_resetwal` equivalent).
- Investigate the **root cause** of recurring corruption: unclean shutdown (`before-quit`
  not checkpointing PGlite — see item 12, which guarantees unclean shutdowns),
  antivirus/search-indexer touching `pgdata`, two app instances, or a cloud-sync client.
  Add a startup integrity check + checkpoint on quit.

### 2. On-disk `.codittle/` not used as source of truth to rebuild the DB
**Problem.** Each project already stores everything needed to reconstruct its DB state in
`.codittle/` on disk (tracked files, remote paths, remote host, deploy history, BP mapping),
yet after a DB loss none of it is used — the project graph/file tree comes back empty.
**Evidence.** `project_graphs.package_tree` was `[]` for every project in the fresh DB; the
intact `.codittle/versions.json`/`folder-mappings.json` were ignored on open.
**Impact.** The DB is a single point of failure even though the durable truth is on disk.
**Proposal.** Make **disk authoritative and the DB a derived cache**: on opening a project,
reconcile/rebuild `project_graphs` + the file tree from `.codittle/` + the filesystem. With
this, a corrupt DB becomes a non-event (re-derive on next open).

### 3. No automatic backups of the embedded database
**Problem.** There is no rolling backup of `pgdata`; the only copies that existed were ones
we made manually during recovery.
**Proposal.** On clean shutdown (and/or daily), snapshot `pgdata` to
`workspaces/<id>/backups/<ts>/` keeping the last N. Surface "Restore from backup" in the UI.
A corruption then costs minutes, not the dataset.

---

## P1

### 4. Connections have no export/import
**Problem.** SSH connections live only in `t24_stream_envs` (with `password_enc`); there is
no user-facing export. Recovering them required a custom PGlite reader.
**Impact.** Single point of failure; can't migrate connections between workspaces/machines
or keep them under (secured) version control.
**Proposal.** **Export/Import connections** as JSON (passwords via OS keychain reference or
an optional passphrase-encrypted blob), plus a headless CLI (`codittle connections export`).

### 5. Workspace identity tied to root path → silent new empty workspace
**Problem.** Workspaces appear keyed by a hash of the opened root path. Opening the "same"
project from a slightly different path (cloud re-root, drive letter, casing) spins up a
**new, empty** workspace instead of reusing the existing one.
**Evidence.** Two workspaces for the same project set: an older one with Title-Cased
project/bank names and the active one with lower-cased names — same projects, different
path spelling.
**Impact.** Connections + projects silently "disappear" (they're in the other workspace).
**Proposal.** A **workspace picker / recent-workspaces** list; identify a workspace by a
stable marker file at the root (e.g. `.codittle/workspace.json` with a UUID) rather than a
path hash; on mismatch, **prompt to reuse/migrate** instead of creating an empty one.

### 6. Explorer/file-tree can't rebuild from local files (needs server re-pull)
**Problem.** The explorer renders the DB-tracked file set, not a live disk view. After a DB
reset, local files exist on disk but the explorer is empty, and re-pointing the folder does
not re-import them — the practical recovery is a re-pull from the server over SSH.
**Impact.** Local-only/unsynced edits are at risk during the re-pull; recovery needs the
server to be reachable.
**Proposal.** Add **"import existing routines from folder"** (adopt local files into the
project using `.codittle/versions.json`) so the explorer can be rebuilt **offline** from
disk; couple with item 2.

### 12. Desktop shell panic-crashes (0xc0000409) instead of recovering a dead backend
**Problem.** When the Theia backend / plugin-host connection dies, the UI shows **offline**
with the status bar frozen on the last activation message (observed: "Activating: Git Base",
the built-in git extension), and shortly afterwards the Tauri desktop shell itself
**fast-fails** — exception `0xc0000409` (Rust panic/abort) with the faulting module being
`codittle-desktop.exe` itself. The window vanishes with no message; the user cannot tell a
crash from having closed the app.
**Evidence.** 2026-07-09, portable 0.5.3: both backend node processes were alive but had
**zero sockets** (no listeners, no connections — `netstat -ano`); UI offline + frozen. Two
minutes later, Windows Application event 1000: faulting app **and** module
`codittle-desktop.exe 0.5.3.0`, exception `0xc0000409`, fault offset `0x37c315`, PID matching
the observed instance. An identical `0xc0000409` event 1000 exists for **0.5.0** — the bug
spans versions. Theia log dirs (`%LOCALAPPDATA%\Codittle\theia\logs\<ts>\host`) are always
empty, so there is no app-side trace at all.
**Impact.** Recurring, unexplained whole-app disappearance. Worse: a panic-abort is exactly
the **unclean shutdown** suspected as a root cause of the PGlite corruption in item 1 — this
item likely *feeds* item 1.
**Proposal.**
- Treat backend loss as a recoverable state: show "backend disconnected — restarting…" and
  respawn the backend, instead of letting the IPC error path `unwrap()`/panic.
- Install a panic hook that writes the panic message + backtrace to a crash log under
  `%LOCALAPPDATA%\Codittle\run\` before aborting.
- Make the backend write real logs (the empty Theia log sessions make post-mortems
  impossible; diagnosis required the Windows event log:
  `wevtutil qe Application '/q:*[System[(EventID=1000)]]' /rd:true /f:text`).

### 13. Sidecar runs on system Node from PATH, not the bundled runtime
**Problem.** The portable distribution ships its own `node.exe`, but the desktop shell
spawns the API sidecar with whatever `node` resolves from PATH.
**Evidence.** 2026-07-09, portable 0.5.3: command line
`"C:\Program Files\nodejs\node.exe" …\sidecar\server.cjs --port 55049` while the bundled
runtime (`node.exe` v24.15.0, next to `codittle-desktop.exe`) sat unused; system Node was
v24.16.0.
**Impact.** On a machine without Node — or with an older major version whose ABI doesn't
match the sidecar's native modules (`node-pty`, `ssh2`, PGlite) — the sidecar fails to start
or crashes, and per item 12 the failure is silent. Works today only by coincidence of the
installed version.
**Proposal.** Always spawn the sidecar (and the Theia backend) with the **bundled**
`node.exe` resolved relative to the executable; fall back to PATH only if the bundle is
missing, and log which runtime was chosen.

### 14. "Frozen-but-open" window: renderer hangs mid-activation, no watchdog
**Problem.** The window can wedge completely — unresponsive, status bar frozen on
"Activating Git Base (built-in)" — while the backend is **perfectly healthy**. Unlike item 12
(backend dies, shell panics), nothing here crashes: the frontend renderer's single JS thread
stops pumping mid-activation and never recovers. The app's only self-heal
(reconnect-on-disconnect) never fires, because nothing disconnected. The status text is a red
herring — it's just the last frame the frozen renderer painted, not a real git operation.
**Evidence.** Portable 0.6.0: window frozen on "Activating Git Base". Live process inspection
showed the backend node (child of `codittle-desktop.exe`) answering **HTTP 200 in 6.6 ms**;
the frontend↔backend WebSocket still **`ESTABLISHED`** (renderer `msedgewebview2.exe` ↔ node,
via `netstat -ano`); `git 2.55.0` on PATH; and the opened workspace had **no `.git`** and
**no `index.lock`** — git had nothing to activate against, and no `git.exe` child existed.
Recovery required an external `taskkill /T` of the shell tree + relaunch; a fresh backend came
up on a new port and the window loaded normally.
**Impact.** Total loss of the session with **no in-app affordance to recover** — the user must
know to kill a process tree from the OS. And because the backend is a **child of the shell**
(confirmed via `ParentProcessId`), even an external "restart just the frontend" is impossible:
any recovery also kills the backend and its live SSH sessions.
**Root cause — why it can't self-heal:**
- Reconnect logic triggers only on socket **close** / heartbeat-timeout; a live-but-idle
  socket + a never-resolving activation promise triggers **neither**.
- The reconnect/watchdog code runs on the **same single JS thread that is frozen**, so it
  cannot rescue itself.
- Extension activation has **no timeout** — a stuck `activate()` pends forever, silently
  gating first paint.
**Proposal.**
- **Out-of-thread liveness watchdog + app-level heartbeat.** Have the Tauri host (a separate,
  non-frozen process) ping the renderer and expect a pong within a deadline; N missed pongs on
  a still-open socket = hang → host force-reloads the WebView. This distinguishes a *hang* from
  a *disconnect* and turns a permanent freeze into a bounded auto-reload.
- **Timeout on extension activation**, and make git activation **lazy/non-blocking** so a
  built-in can never gate first paint.
- **Host-drawn recovery overlay** ("Reload window / Restart backend") when a hang is detected,
  so recovery never depends on the frozen renderer or an OS-level process kill. Subscribe to
  WebView2 unresponsiveness / `ProcessFailed` signals to detect it at the host.
- Consider **supervising the backend independently of the shell** so the frontend can be
  recycled (true "replug") without dropping backend SSH sessions.

Cross-reference: complements **item 12** — that item is *backend-death → shell panic*; this is
the inverse (*backend healthy → frontend hangs*). Common theme: the app has no watchdog that
recovers a wedged component.

### 16. Built-in screenshot (capture overlay) deadlocks the desktop shell
**Problem.** Taking a screenshot with Codittle's own capture tool freezes the **entire desktop
shell** (window title `CodittleX`, Windows flags it *Not Responding*). Nothing crashes and no
capture is written; the app simply stops repainting and never comes back. Third variant of the
"wedged component, no watchdog" family (items 12, 14): here the *host* (Tauri main thread)
is the one that hangs, so even the host-side watchdog proposed in item 14 would not fire.
**Evidence.** 2026-09-10, installed build of 2026-09-08 (`%LOCALAPPDATA%\Codittle`):
- `codittle-desktop.exe` PID 26924 **Not Responding**; CPU counters flat across two samples a
  minute apart (kernel 415.94s → 416.25s, user unchanged) — **idle-blocked, not spinning**.
- Every child healthy: API sidecar `:53083/health` → **200 in 8 ms**, Theia backend `:65302`
  → 200, `msedgewebview2.exe` alive, workspace `ssh-audit.jsonl` still being appended.
- `%LOCALAPPDATA%\CodittleData\captures\` **empty** — the hang is mid-capture, before save.
- No crash dump, no Application-Hang event, no host log to consult.
- The shell binary's Tauri command table for this feature (strings from the exe):
  `open_capture_overlay` · `close_capture_overlay` · `capture_region` · `ocr_region` ·
  `get_capture` · `discard_capture` · `copy_capture` · `ocr_capture` · `ocr_capture_layout` ·
  `save_capture_to` · `save_capture_to_inbox` · `save_capture_as` · `set_capture_shortcut`.
**Impact.** Total loss of the session from a routine action; recovery is an OS-level
`taskkill /PID <shell> /F` + relaunch, which (per item 14) also tears down the backend and
its live SSH sessions. Users learn to avoid the feature and reach for Win+Shift+S instead.
**Root cause (inferred — source not on this machine).** The classic Tauri-on-Windows
deadlock: window operations (create/close/show/set-bounds) are serviced by the main thread's
event loop, and a **synchronous** `#[tauri::command]` runs *on* that thread. The capture
flow opens a full-screen overlay window, then a sync `capture_region`/`ocr_region` does the
screen grab and OCR inline **and** closes the overlay from inside the command — the main
thread ends up waiting on a window op that only it can process. Symptoms match exactly:
idle-blocked host, healthy children, nothing written.
**Proposal.**
- **Make every capture command `async`.** Tauri runs async commands on its own pool, so
  window ops issued from them are *dispatched* to the main loop instead of awaited from it.
  Same for `open_capture_overlay`, whose `WebviewWindowBuilder::build()` can deadlock from a
  sync command on Windows. Better still: pre-create the overlay at startup and show/hide it.
- **Move the grab and the OCR into `tauri::async_runtime::spawn_blocking`** — they are
  CPU-heavy and must never run on the UI thread.
- **Order: overlay emits rect → overlay hidden → grab → OCR → return result → close
  overlay** (or let the front end call `close_capture_overlay` after the result lands).
  Hiding before grabbing also stops the tool screenshotting its own overlay.
- **Bound it.** Wrap grab+OCR in `tokio::time::timeout`; on expiry close the overlay and
  `show_toast` an error instead of wedging. Consider running OCR out-of-process (the Node
  sidecar already exists) so an OCR library fault cannot take the host down.
- **Regression test.** Trigger `capture_region` with OCR on a large monitor while the main
  window is visible and assert the front end can still `invoke('sidecar_port')` mid-capture.

Sketch of the fixed command:
```rust
#[tauri::command]
async fn capture_region(app: AppHandle, rect: Rect) -> Result<Capture, String> {
    let cap = tauri::async_runtime::spawn_blocking(move || grab_and_ocr(rect))
        .await.map_err(|e| e.to_string())??;
    if let Some(w) = app.get_webview_window("capture-overlay") { let _ = w.close(); }
    Ok(cap)
}
```

Cross-reference: **item 12** (backend dies → shell panics), **item 14** (renderer hangs →
host healthy), **this** (host hangs → everything else healthy). The one watchdog that covers
all three is an *external* supervisor, not code inside any single process.

---

## P2

### 7. Project discovery scans helper folders as projects
**Evidence.** Codittle listed a `_template` skeleton folder as a project (15 vs 14 real).
**Proposal.** Honour a `.codittleignore` and/or skip `_`/`.`-prefixed folders; allow
hide/exclude from the UI.

### 8. Cloud-synced workspace interacts badly with path-hash identity
**Problem.** The project tree lives under a cloud-synced folder (OneDrive) while `pgdata`
is under LocalAppData (not synced). Cloud clients can change the effective path (→ item 5),
churn files, or present dehydrated (Files-On-Demand) placeholders.
**Proposal.** Stable content-based workspace identity (item 5); detect dehydrated files;
document a recommendation to keep the workspace on a local (non-synced) path, or make
Codittle resilient to sync.

### 9. Per-file `remoteHost`, no project-level env binding
**Problem.** `versions.json` records `remoteHost` per file; there's no single "this project
targets env X" setting, and projects can end up mixing hosts.
**Proposal.** A project-level env/target in `.codittle/folder-mappings.json` (env id + host
+ bnk.run), surfaced and editable in the UI.

### 10. Password-encryption key scope undocumented / not portable
**Problem.** Unclear whether `password_enc` is encrypted with a per-workspace or
install/user-bound key; during recovery, copied blobs may not decrypt in another workspace.
**Proposal.** Use OS-keychain-backed encryption (e.g. `safeStorage`-style, user-bound) so
connections survive workspace changes; document the scope; export (item 4) should
re-encrypt portably.

### 11. No built-in recovery/diagnostics
**Problem.** Diagnosing the above required reverse-engineering the PGlite layout.
**Proposal.** Ship `codittle doctor` / `--export` / `--repair`: list workspaces + DB health,
back up/restore `pgdata`, export connections, rebuild graphs from disk.

### 15. Claude-updater patch anchors use `\w+` — `$`-aliases cause false drift, updates held back
**Problem.** The Claude Code auto-updater (`theia/scripts/claude-updater.mjs`) is correctly
fail-closed: it re-applies the IDE's compatibility patches
(`theia/scripts/patch-claude-image-open.mjs`) to every freshly downloaded VSIX and holds the
update back if any anchor drifts. But the anchor regexes capture minified identifier aliases
with `(\w+)`, and `\w` does not match `$`. The moment upstream's minifier emits a
`$`-containing alias (e.g. `T$`) at an anchor site, a **structurally unchanged** bundle
reports `drift(0)` and the update is held until someone hand-edits the script.
**Evidence.** 2026-09-02: Settings showed *"Claude 2.1.258 held back: patch anchors drifted
(img=ok, imgClick=drift(0), ctrlA=ok)"*. Grep of the downloaded 2.1.258 `extension.js`
showed the click-handler shape byte-for-byte identical to the anchor —
`T$.commands.executeCommand("revealInExplorer",X);return}}catch{}T$.window.showTextDocument(X).then`
— failing only because the alias is `T$`. Widening the identifier class made the old regex
match 0 times and the new one exactly 1 (the count the patcher requires); a dry-run of
`applyToExtensionDir` over the unpacked bundle then returned `ok:true`.
**Impact.** Updates of the embedded assistant stall on a false positive; the user stays on an
aging build until a developer manually re-patches. Each such hold also burns the ~100 MB
download once per day. The trip-wire is meant to catch *real* refactors — false drifts erode
trust in it.
**Proposal.**
- Widen every identifier capture in the anchor regexes from `(\w+)` to `([\w$]+)`
  (backreferences and replacement callbacks are unaffected). **Applied downstream
  2026-09-02** to the installed script (both `IMG_ANCHOR` and `CLICK_ANCHOR`), verified
  against the real 2.1.258 VSIX; needs the same two-line change in the Codittle repo so the
  next release doesn't regress it.
- Longer term: on `held-drift`, log a context window around the nearest literal (e.g.
  `"revealInExplorer"`) into the state file, so the next anchor fix doesn't require
  re-downloading and hand-grepping the bundle.

---

## Appendix — what we built downstream to cope (until upstream fixes land)

- `codittle_db.py` (+ `.mjs`): `status` / `backup` / `connections` / `restore` for the
  PGlite DB. Safe by default (dry-run, refuses to write while Codittle is open, backs up
  first). Used on 2026-06-24 to restore 11 connections from an orphaned workspace into the
  active one.
- `codittle_connections.py` (+ `.mjs`): read-only connection inspector.

> These are workarounds. Items 1–3 (warn + auto-backup + disk-as-source-of-truth) would make
> them unnecessary.
