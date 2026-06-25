# t24-tools — Temenos T24 dev toolkit (over SSH)

A self-contained toolkit for working with **Temenos T24 (R18 / jBASE / TAFC)** test
environments without hand-logging into each box: **fetch & inspect source**, read
records/VERSIONs, search the source tree, recover record exports, and **run a
command across the whole fleet** at once.

Everything is driven by one **servers CSV** and authenticates with the password from that
CSV. Centralised here so we never rebuild it.

```
t24-tools/
├── Test_Environments.csv          ← servers + credentials (the shared config; gitignored)
├── .t24_cmd_library.tsv           ← saved commands for t24_run (local; gitignored)
├── fetch_t24_sources.py           ┐
├── grep_t24.py                    │ source fetch & inspection (paramiko)
├── probe_t24.py                   │  — single env (--env), the others import fetch_t24_sources
├── t24_record.py                  │
├── recover_jbase_tar.py           ┘ (local; recovers corrupt jBASE export tars)
├── t24_run.py / .sh               ┐ multi-server command runner
├── t24_run.cmd / _py.cmd          │  — run ONE command across MANY envs in parallel
├── run_192.py                     ┘  (non-interactive single-host example)
├── codittle_connections.py        ┐ Codittle connection inspector
├── codittle_connections.mjs       ┘  — reads the live Codittle database (no Codittle open needed)
├── codittle_db.py                 ┐ Codittle DB recovery — status / backup / restore
└── codittle_db.mjs                ┘  — restore lost connections after a pgdata corruption
```

> **Run the tools from this folder** so the default `Test_Environments.csv` resolves.
> When fetching source, point `--dest` at your project. From elsewhere, pass
> `--servers "<...>/t24-tools/Test_Environments.csv"`. Set `T24_BNK_RUN` to your remote
> `bnk.run` path so the tools default to it (or pass `--bnk` / `--remote-base`).

---

## Quick reference

| Tool | Scope | One-liner |
|---|---|---|
| **`fetch_t24_sources.py`** | 1 env | pull jBASE source by JSHOW paste **or** routine name |
| **`grep_t24.py`** | 1 env | remote `grep` over `*.BP` (`--find` = filename glob) |
| **`probe_t24.py`** | 1 env | SFTP `stat`/`listdir` — does a path exist? |
| **`t24_record.py`** | 1 env | dump a live record / VERSION (`LIST … ALL`) |
| **`recover_jbase_tar.py`** | local | salvage members from a corrupt jBASE export tar |
| **`t24_run.py`** (`.sh`/`.cmd`) | N envs | run one command across every SSH host, in parallel |
| **`run_192.py`** | 1 env | non-interactive example: run a command on a single host |
| **`codittle_connections.py`** | local | list all Codittle SSH connections + live SSH status |
| **`codittle_db.py`** | local | inspect / back up / **recover** Codittle's DB when it corrupts (lost connections) |

---

## `codittle_db.py` — recover Codittle's connections after a DB corruption

Codittle keeps connections + projects in a per-workspace **PGlite** DB
(`%LOCALAPPDATA%\Codittle\workspaces\<id>\pgdata`). If it corrupts, Codittle quarantines it
(`pgdata.corrupt-*`) and starts a **fresh empty** one — connections vanish from the UI but the
data still lives in another workspace or a backup. This tool finds and restores it.

```
python codittle_db.py status                       # workspaces, the ACTIVE one, connection counts, corruption
python codittle_db.py connections [--workspace ID]
python codittle_db.py backup [--all]               # snapshot pgdata -> ../_codittle-db-backups/
python codittle_db.py restore [--from ID|PATH] [--apply]
```

**To recover:** quit Codittle fully → `codittle_db.py status` (it names the workspace that still
has connections) → `codittle_db.py restore --from <ws> --apply` → reopen Codittle. Safe by
default: dry-run unless `--apply`, refuses to write while Codittle is open, backs up first.
Passwords copy across encrypted; if one fails to auth, re-enter it.

---

## Prerequisites
- **Python 3.8+** and **paramiko** (`pip install paramiko`) — for every `*.py`.
- `t24_run.sh` (the bash variant) instead needs **plink.exe** (PuTTY) on PATH.
- **Network reach** to the env (corporate LAN / VPN — hosts are internal IPs).

### Servers CSV (`Test_Environments.csv`)
Header row required; columns matched by name:
```
Groups,Label,Tags,Hostname/IP,Protocol,Port,Username,Password[,bnk.run]
Group/Example,ENV-01,"tags",<host-ip>,ssh,22,<user>,<password>
```
`--env` selects by **label** (`ENV-01`), **last IP octet**, or **full IP**.
Remote bnk.run defaults to `$T24_BNK_RUN` (else a generic `/t24/bnk/bnk.run`); override with
`--bnk` / `--remote-base`, or add a `bnk.run` column. `t24_run` reads the CSV from the
**current directory**.

> **This CSV holds credentials — keep it out of version control** (it's gitignored here).

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
Interactive: reads the CSV (current dir), lets you pick/type a T24 verb or shell
command, runs it on **all** SSH hosts at once (each: `cd bnk.run` → source a trimmed
`.profile` so jBASE loads without the interactive login → run), prints per-host
results and writes a `t24_results_<ts>.log`.
```bash
cd <this folder>      # so Test_Environments.csv + .t24_cmd_library.tsv resolve
python t24_run.py                 # interactive
python t24_run.py -d              # diagnose mode (times connect / profile / command)
python t24_run.py -t 120 -w 8     # per-host timeout 120s, 8 hosts at a time
```
- `t24_run.sh` — same idea in bash (needs **plink**); `t24_run.cmd` / `t24_run_py.cmd`
  launch the `.sh` / `.py` from a Windows cmd prompt.
- `run_192.py` — non-interactive example: `python run_192.py "JSHOW -c MY.ROUTINE"`
  runs one command on a single host (reuses `t24_run`'s logic). Set the target via the
  `T24_HOST` env var or edit the script.

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

**Sample output:**
```
Probing SSH on N host(s)... done.

Codittle stream connections  Bank: <bank>   Stream: R18/TAFC
SSH reachable: N/N

Name    Connection            SSH Home                  SSH  Last Used
------  --------------------  ------------------------  ---  ---------
ENV-01  <user>@<host-ip>:22   /t24/<inst>/bnk/bnk.run   UP   yesterday
ENV-02  <user>@<host-ip>:22   /t24/<inst>/bnk/bnk.run   UP   never
...

Projects in this stream (N):
  project-a
  project-b
  ...
```

**"Last Used"** reflects the last time a connection was opened in Codittle
(the `last_viewed_at` field). `never` means it was added but not yet used from the
Codittle UI — it may still be perfectly SSH-reachable.

**How it works:**
1. Locates Codittle's `node.exe` from the running process or `Downloads\Codittle_*\`.
2. Runs `codittle_connections.mjs` (must live alongside this script) which copies
   Codittle's embedded PGlite database to a temp dir and queries `t24_stream_envs`.
3. For each host, opens a TCP socket to port 22 (2-second timeout) to check SSH.

> **Note:** The `.mjs` helper must be run with Codittle's own `node.exe`
> (not a system Node) because the PGlite WASM module is bundled with Codittle.
> The Python script handles this automatically.

---

## Common workflows
- **Pull a service + batch companions:** `grep_t24.py --find "<base>*"` → list
  `.LOAD/.SELECT/…`, then `fetch_t24_sources.py` (`-r` or JSHOW paste).
- **Who writes/reads a field:** `grep_t24.py --files-only "<FIELD>"` → fetch + read.
- **Inspect a VERSION's hooks:** `t24_record.py --cmd 'LIST F.VERSION WITH @ID EQ "<APP>,<VER>" ALL'`
  → `INPUT.ROUTINE / VALIDATION.RTN / AUTH.ROUTINE / BEFORE.AUTH.RTN / CHECK.REC.RTN`.
- **Same check across all envs:** `t24_run.py` with a `JSHOW -c <name>`.
- **Recover dropped record history:** `recover_jbase_tar.py … --extract`.
- **Pick an env for `--env`:** `codittle_connections.py --live` → shows only UP servers; use
  the last IP octet shown as the `--env` value for any other tool.

## Gotchas (learned the hard way)
- **`find`/`ls` are shadowed** by jBASE on the login PATH → use `grep_t24.py --find`.
- **`CT` is an interactive pager** → prefer `LIST … ALL` for VERSIONs.
- **Object-only source**: many Temenos/utility routines ship as compiled `.so` with **no
  source** — read their contract from the callers.
- **Attribute marks** (FM/VM/SM = 0xFE/0xFD/0xFC) are invalid UTF-8 — convert them on
  the server *before* decoding (the tools do) or they're lost.
- **Credentials** live in `Test_Environments.csv` — treat it as a secret; never commit it.
