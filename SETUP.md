# Setup

The tools ship with **no hosts, credentials, or site-specific paths** — you provide those
locally. Nothing here is committed (all of it is gitignored).

## 1. Servers / credentials — `Test_Environments.csv`
Create `Test_Environments.csv` in this folder (it's gitignored). Header row required;
columns are matched by name:

```
Groups,Label,Tags,Hostname/IP,Protocol,Port,Username,Password[,bnk.run]
Group/Example,ENV-01,"tags",<host-ip>,ssh,22,<user>,<password>,/t24/<inst>/bnk/bnk.run
```

- One row per environment. `--env` selects by **label**, **last IP octet**, or **full IP**.
- Add a per-row `bnk.run` column, or rely on the `T24_BNK_RUN` default below.
- **Keep this file private** — it holds passwords.

## 2. Remote path — `T24_BNK_RUN`
Set the env var to your T24 `bnk.run` directory so the tools default to it (else they fall
back to a generic `/t24/bnk/bnk.run`, which you'd override with `--bnk` each time):

```bash
# bash / Git-Bash
export T24_BNK_RUN=/t24/<inst>/bnk/bnk.run
```
```powershell
# PowerShell (persist for your user)
setx T24_BNK_RUN "/t24/<inst>/bnk/bnk.run"
```

## 3. Optional env vars
| Var | Used by | Purpose |
|---|---|---|
| `T24_BNK_RUN` | fetch / grep / probe / record / session | default remote `bnk.run` path |
| `T24_HOST` | `run_192.py` | the single host that script targets |
| `T24_PROJECTS_ROOT` | `ctx_sync.py` | the Codittle `…/projects` dir (else auto-detected) |

## 4. Codittle tools
`codittle_connections.py` / `codittle_db.py` need the **Codittle desktop app** installed
(they use its bundled `node.exe` for PGlite). No extra pip packages.

## 5. Python deps
`pip install paramiko` (for the SSH tools). The `bash` runner `t24_run.sh` needs
**plink.exe** (PuTTY) on PATH instead.
