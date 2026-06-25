# t24-tools — Temenos T24 dev toolkit (over SSH)

A self-contained toolkit for working with **Temenos T24 (R18 / jBASE / TAFC)** test
environments without hand-logging into each box: **fetch & inspect source**, read
records/VERSIONs, search the source tree, recover record exports, and **run a
command across the whole fleet** at once.

Environments and credentials come from a **shared encrypted store** (see [SETUP.md](SETUP.md)):
passwords are sealed with **Windows DPAPI** — never plaintext, never a CSV at rest. The store is
the union of Amethyst's DB and a local standalone DB, so hosts added on either side are visible
to both, and you can seed it straight from **Codittle**.

```
t24-tools/
├── envstore.py                    ┐ shared env/credential store (DPAPI-sealed; union of
│                                  ┘  Amethyst's DB + local DB; Codittle/CSV import)
├── t24_env.py                     ← manage the store: list / add / passwd / import-codittle
├── .t24_cmd_library.tsv           ← saved commands for t24_run (local; gitignored)
├── fetch_t24_sources.py           ┐
├── grep_t24.py                    │ source fetch & inspection (paramiko)
├── probe_t24.py                   │  — single env (--env); the others import fetch_t24_sources
├── t24_record.py                  │
├── get_t24.py / put_t24.py        │  — binary SFTP download / upload with md5 verify
├── ssh_cmd.py                     │  — one raw shell command on one env
├── t24_session.py                 │  — persistent jBASE session (connect once, run many)
├── recover_jbase_tar.py           ┘ (recovers corrupt jBASE export tars)
├── t24_run.py / _py.cmd           ┐ multi-server command runner
├── run_192.py                     ┘  — run ONE command across MANY envs (or one), in parallel
├── codittle_connections.py        ┐ Codittle connection inspector
├── codittle_connections.mjs       ┘  — reads the live Codittle database (no Codittle open needed)
├── codittle_db.py                 ┐ Codittle DB recovery — status / backup / restore
└── codittle_db.mjs                ┘  — restore lost connections after a pgdata corruption
```

> The store resolves automatically wherever you run the tools from. `--env` selects a host by
> **label**, **last IP octet**, or **full IP**. The remote `bnk.run` is **auto-detected per host**
> — override only if needed with `--bnk` / `--remote-base` or `T24_BNK_RUN`.

---

## Quick reference

| Tool | Scope | One-liner |
|---|---|---|
| **`t24_env.py`** | local | manage the env store (`list` / `add` / `passwd` / `import-codittle`) |
| **`fetch_t24_sources.py`** | 1 env | pull jBASE source by JSHOW paste **or** routine name |
| **`grep_t24.py`** | 1 env | remote `grep` over `*.BP` (`--find` = filename glob) |
| **`probe_t24.py`** | 1 env | SFTP `stat`/`listdir` — does a path exist? |
| **`t24_record.py`** | 1 env | dump a live record / VERSION (`LIST … ALL`) |
| **`get_t24.py` / `put_t24.py`** | 1 env | binary SFTP download / upload, md5-verified |
| **`t24_session.py`** | 1 env | persistent jBASE session — connect once, run many |
| **`recover_jbase_tar.py`** | local | salvage members from a corrupt jBASE export tar |
| **`t24_run.py`** | N envs | run one command across every SSH host, in parallel |
| **`run_192.py`** | 1 env | non-interactive example: run a command on a single host |
| **`codittle_connections.py`** | local | list all Codittle SSH connections + live SSH status |
| **`codittle_db.py`** | local | inspect / back up / **recover** Codittle's DB when it corrupts |

---

## Environments & credentials (the store)

Nothing is configured in a file you commit. Manage hosts with **`t24_env.py`** — see
[SETUP.md](SETUP.md) for the full model. The fast path:

```bash
python t24_env.py import-codittle     # seed host/port/user from Codittle (no passwords)
python t24_env.py passwd ENV-01       # key each password once (hidden, DPAPI-sealed)
python t24_env.py list                # confirm — passwords are shown only as yes/--
```

Passwords are sealed with **Windows DPAPI** (per-user). The store is the union of Amethyst's DB
(discovered via the `AMETHYST_DB` env var it registers) and a local standalone DB
(`~/.t24tools/envs.db`, registered as `T24_ENV_DB`), so both apps see every host.

> **Windows / Git-Bash:** native `python.exe` + Git-Bash rewrites `/t24/...` CLI args
> into `C:\...`. Pass remote `/t24` paths (e.g. to `probe_t24.py`) only with
> `MSYS_NO_PATHCONV=1`. The fetch/grep/record tools build `/t24` paths *internally*,
> so normal use is unaffected.

---

## A. Source fetch & inspection (single env)

### `fetch_t24_sources.py` — get source
```bash
# 1) paste a raw JSHOW session ('jsh <user> ~ -->JSHOW -c X' prompt lines are ignored)
JSHOW -c FOO | python fetch_t24_sources.py --env 30 --dest /path/to/project

# 2) just name routines — runs `JSHOW -c NAME` on the box, finds the BP, pulls it
python fetch_t24_sources.py --env 30 --dest . -r MY.ROUTINE -r OTHER.ROUTINE
```
Lands `<dest>/<BP>/<ROUTINE>`. Tries `<routine>` then `<routine>.b`. **Skips cleanly**
when a routine is **object-only** (no source deployed — e.g. vendor/utility routines).
`--dry-run` shows the plan; `--remote-base` overrides the root.

### `grep_t24.py` — search the tree
```bash
python grep_t24.py --env 30 --files-only "SOME.FIELD"      # who references a field
python grep_t24.py --env 30 "ANOTHER.FIELD"                # matching lines
python grep_t24.py --env 30 --find "MY.ROUTINE*"           # filename globs (.LOAD/.SELECT/…)
```
Sources the trimmed `.profile` so `grep` is on PATH; `--find` uses bash globbing
(jBASE shadows `find`/`ls`).

### `probe_t24.py` — does it exist? (pure SFTP, no shell)
```bash
MSYS_NO_PATHCONV=1 python probe_t24.py --env 30 \
  --list /t24/<inst>/bnk/bnk.run --grep .BP \
  /t24/<inst>/bnk/bnk.run/SOME.BP/SOME.RECORD
```

### `t24_record.py` — read a record / VERSION
```bash
# a VERSION (use LIST ... ALL — field-named, non-interactive)
python t24_record.py --env 30 --cmd 'LIST F.VERSION WITH @ID EQ "FUNDS.TRANSFER,MY.VERSION" ALL'
# any record by id
python t24_record.py --env 30 --file F.MY.PARAM <record-id>
```
`--cmd` runs a literal jBASE command (best for VERSIONs). Default verb `CT` is a
pager — the tool auto-pages + strips escapes; attribute marks → `\n`/` | `/` ^ `.

### `get_t24.py` / `put_t24.py` — binary SFTP, md5-verified
```bash
python get_t24.py --env 30 "$T24_BNK_RUN/SOME.BP/MY.ROUTINE" MY.ROUTINE.local
python put_t24.py --env 30 local/myfile.jar "$T24_BNK_RUN/SOME.BP/myfile.jar"
```
SFTP is binary-safe (no CRLF/encoding mangling), and both md5-verify the remote copy.

### `recover_jbase_tar.py` — salvage a corrupt export tar (local)
T24 records exported via `COPY … TO <dir>` then tarred often corrupt after member 1.
This scans every `ustar` header and rebuilds all members:
```bash
python recover_jbase_tar.py RECORDS.tar --list
python recover_jbase_tar.py RECORDS.tar --extract recovered
python recover_jbase_tar.py RECORDS.tar --grep MY.RECORD   # +FM attr index
```

## B. Multi-server command runner

### `t24_run.py` — run one command on every env (parallel)
Interactive: resolves envs from the store, lets you pick/type a T24 verb or shell
command, runs it on **all** SSH hosts at once (each: `cd bnk.run` → source a trimmed
`.profile` so jBASE loads without the interactive login → run), prints per-host
results and writes a `t24_results_<ts>.log`.
```bash
python t24_run.py                 # interactive
python t24_run.py -d              # diagnose mode (times connect / profile / command)
python t24_run.py -t 120 -w 8     # per-host timeout 120s, 8 hosts at a time
```
- `t24_run_py.cmd` launches `t24_run.py` from a Windows cmd prompt.
- `run_192.py` — non-interactive example: `python run_192.py "JSHOW -c MY.ROUTINE"`
  runs one command on a single host (reuses `t24_run`'s logic). Set the target via the
  `T24_HOST` env var or edit the script.

### `t24_session.py` — persistent jBASE session
Connect once, keep jBASE loaded, fire many commands cheaply:
```bash
python t24_session.py serve --env ENV-01 --port 8765        # start the daemon
python t24_session.py send  --port 8765 'LIST F.SOME.FILE WITH @ID LIKE "ABC..." ALL'
python t24_session.py stop  --port 8765
python t24_session.py envs                                   # list Amethyst DB env ids
```

---

## C. Codittle connection inspector

### `codittle_connections.py` — list stream connections and check SSH liveness

Reads **directly from Codittle's embedded database** (no need to open the Codittle
app) and reports every SSH connection configured for the workspace, ordered by
most-recently-used first. Also probes each host on port 22 to confirm SSH is up.

**Prerequisites:**
- Codittle desktop app installed (portable version in `Downloads\Codittle_*\` works).
- No additional pip packages needed — only the standard library.
- No need for Codittle to be open or running.

```bash
python codittle_connections.py                 # full table with live SSH status
python codittle_connections.py --no-check      # skip the SSH probe — instant
python codittle_connections.py --live          # only SSH-reachable servers
python codittle_connections.py --json          # raw JSON — pipe to jq
```

> Tip: `t24_env.py import-codittle` reuses this reader to seed the env store with the
> connection metadata, so you add servers in Codittle once and just key the passwords.

**How it works:**
1. Locates Codittle's `node.exe` from the running process or `Downloads\Codittle_*\`.
2. Runs `codittle_connections.mjs` (must live alongside this script) which copies
   Codittle's embedded PGlite database to a temp dir and queries `t24_stream_envs`.
3. For each host, opens a TCP socket to port 22 (2-second timeout) to check SSH.

> **Note:** The `.mjs` helper must be run with Codittle's own `node.exe`
> (not a system Node) because the PGlite WASM module is bundled with Codittle.
> The Python script handles this automatically.

---

## D. `codittle_db.py` — recover Codittle's connections after a DB corruption

Codittle keeps connections + projects in a per-workspace **PGlite** DB
(`%LOCALAPPDATA%\Codittle\workspaces\<id>\pgdata`). If it corrupts, Codittle quarantines it
(`pgdata.corrupt-*`) and starts a **fresh empty** one — connections vanish from the UI but the
data still lives in another workspace or a backup. This tool finds and restores it.

```
python codittle_db.py status                       # workspaces, the ACTIVE one, connection counts
python codittle_db.py connections [--workspace ID]
python codittle_db.py backup [--all]               # snapshot pgdata -> ../_codittle-db-backups/
python codittle_db.py restore [--from ID|PATH] [--apply]
```

**To recover:** quit Codittle fully → `codittle_db.py status` (it names the workspace that still
has connections) → `codittle_db.py restore --from <ws> --apply` → reopen Codittle. Safe by
default: dry-run unless `--apply`, refuses to write while Codittle is open, backs up first.

---

## Prerequisites
- **Python 3.8+** and **paramiko** (`pip install paramiko`) — for the SSH tools.
- **Network reach** to the env (corporate LAN / VPN — hosts are internal).
- Manage environments with **`t24_env.py`** — see [SETUP.md](SETUP.md).

## Common workflows
- **Pull a service + batch companions:** `grep_t24.py --find "<base>*"` → list
  `.LOAD/.SELECT/…`, then `fetch_t24_sources.py` (`-r` or JSHOW paste).
- **Who writes/reads a field:** `grep_t24.py --files-only "<FIELD>"` → fetch + read.
- **Inspect a VERSION's hooks:** `t24_record.py --cmd 'LIST F.VERSION WITH @ID EQ "<APP>,<VER>" ALL'`
  → `INPUT.ROUTINE / VALIDATION.RTN / AUTH.ROUTINE / BEFORE.AUTH.RTN / CHECK.REC.RTN`.
- **Same check across all envs:** `t24_run.py` with a `JSHOW -c <name>`.
- **Pick an env for `--env`:** `codittle_connections.py --live` → shows only UP servers; use
  the last IP octet shown as the `--env` value for any other tool.

## Gotchas (learned the hard way)
- **`find`/`ls` are shadowed** by jBASE on the login PATH → use `grep_t24.py --find`.
- **`CT` is an interactive pager** → prefer `LIST … ALL` for VERSIONs.
- **Object-only source**: many Temenos/utility routines ship as compiled `.so` with **no
  source** — read their contract from the callers.
- **Attribute marks** (FM/VM/SM = 0xFE/0xFD/0xFC) are invalid UTF-8 — convert them on
  the server *before* decoding (the tools do) or they're lost.
- **Credentials** are DPAPI-sealed in the store — never in plaintext, never committed. A host
  with no password yet shows `--` in `t24_env.py list`; key it with `t24_env.py passwd`.
