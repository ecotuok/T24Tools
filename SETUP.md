# Setup

The tools ship with **no hosts, credentials, or site-specific paths** — you provide those
locally, and nothing sensitive is ever stored in plaintext or committed to git.

## 1. Environments & credentials — the shared store
Hosts and credentials live in an **encrypted store**, not a file. Passwords are sealed with
**Windows DPAPI** (per-user, machine-bound); they are never written in plaintext and never sit
in a CSV at rest.

The store is the **union** of two backends, so both sides always see the same hosts:
- **Amethyst's DB** when the Amethyst app is present — it registers its location in the
  `AMETHYST_DB` user env var when it runs, and
- a **local standalone DB** (`~/.t24tools/envs.db`) — whose path the tools register as
  `T24_ENV_DB`, so Amethyst can find and merge it later.

So if the tools know 2 hosts and Amethyst knows 5, both show all 7. The apps are portable
(no install) and discover each other purely through those registered env vars.

Manage the store with `t24_env.py`:
```
python t24_env.py list                 # every host (no passwords printed)
python t24_env.py add                   # add/update one (hidden password prompt)
python t24_env.py passwd <label>        # set/replace a password (hidden)
python t24_env.py remove <label>
```

### Add servers in ONE place: import from Codittle
If you already keep connections in **Codittle**, don't re-type them — import the metadata
(host / port / user / ssh-home) and just key the passwords (Codittle seals its own with a key
we can't read, so passwords are always yours to enter):
```
python t24_env.py import-codittle       # pulls connection details only (no passwords)
python t24_env.py passwd <label>        # key each password once
```

Pull from other clients too (metadata only — you key passwords):
```
python t24_env.py import-tabby          # Tabby / Terminus  (reads config.yaml; needs PyYAML)
python t24_env.py import-termius        # Termius  (best-effort)
```
> **Termius** encrypts hosts client-side (end-to-end), so most installs expose nothing readable
> on disk — the importer tells you and you add those manually. **Tabby** parses cleanly.

### One-time CSV import (optional)
A CSV can **seed** the store once, then be deleted — it is never read at runtime:
```
python t24_env.py import servers.csv
# header: Groups,Label,Tags,Hostname/IP,Protocol,Port,Username,Password[,bnk.run]
rm servers.csv
```

`--env` on every tool selects by **label** (`ENV-01`), **last IP octet** (`30`), or **full IP**.

## 2. Remote path — auto-detected (usually nothing to do)
`bnk.run` differs per environment (each jBASE account has its own). The tools **auto-detect
each host's `bnk.run`** at connect time — `$HOME` if it holds a `VOC` (the T24 file dictionary),
else they search `/t24/*/bnk/bnk.run` — so you don't configure it per box.

Overrides, in priority order, if detection ever needs help:
1. `--bnk` / `--remote-base` on the command line,
2. a stored `bnk.run` value for that env,
3. the `T24_BNK_RUN` env var (last-resort default).

## 3. Optional env vars
| Var | Used by | Purpose |
|---|---|---|
| `AMETHYST_DB` | resolver | path to Amethyst's DB (Amethyst registers this itself) |
| `T24_ENV_DB` | resolver | path to the local standalone store (default `~/.t24tools/envs.db`) |
| `T24_BNK_RUN` | fetch / grep / record / session | last-resort `bnk.run` (only if auto-detect fails) |
| `T24_HOST` | `run_192.py` | the single host that script targets |
| `T24_PROJECTS_ROOT` | `ctx_sync.py` | the Codittle `…/projects` dir (else auto-detected) |

## 4. Codittle tools
`codittle_connections.py`, `codittle_db.py`, and `t24_env.py import-codittle` use the
**Codittle desktop app**'s bundled `node.exe` to read its PGlite DB. No extra pip packages.

## 5. Python deps
`pip install paramiko` (for the SSH tools). `pip install pyyaml` only if you use `import-tabby`.
