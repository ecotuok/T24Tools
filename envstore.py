#!/usr/bin/env python3
"""Shared environment + credential store for t24-tools and Amethyst — NO CSV at rest,
NO plaintext, and CROSS-CUTTING: both sides see the union of all hosts.

Two physical stores can exist; envs from both are merged (deduped by label):
  * **Amethyst's** DB — found via the `AMETHYST_DB` env var that Amethyst registers (setx)
    when it runs, or the sibling ../amethyst/data/amethyst.db.
  * the **local standalone** DB (~/.t24tools/envs.db), whose path t24-tools registers as
    `T24_ENV_DB` so Amethyst can find and merge it.

So if t24-tools has 2 hosts and Amethyst has 5, both show 7. The apps are portable (no
install) and discover each other purely through these registered env vars.

Passwords are sealed with **Windows DPAPI** (same scheme as amethyst/app/secrets.py), so a
blob written by one side is readable by the other. A CSV may be imported ONCE
(`t24_env.py import file.csv`) and then deleted; it is never read at runtime.
"""
import csv as _csv
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# ── credential sealing (Windows DPAPI; format-compatible with amethyst) ──────
_PREFIX = b"dpapi1:"

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob_bytes(b):
        d = ctypes.string_at(b.pbData, b.cbData)
        _kernel32.LocalFree(b.pbData)
        return d

    def _dpapi(data, protect):
        inp = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                           ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        fn = _crypt32.CryptProtectData if protect else _crypt32.CryptUnprotectData
        if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            raise OSError("DPAPI call failed")
        return _blob_bytes(out)

    def seal(plaintext: str) -> bytes:
        return _PREFIX + _dpapi((plaintext or "").encode("utf-8"), True)

    def unseal(blob) -> str:
        if not blob:
            return ""
        blob = bytes(blob)
        if blob.startswith(_PREFIX):
            return _dpapi(blob[len(_PREFIX):], False).decode("utf-8")
        return blob.decode("utf-8", "replace")
else:
    def seal(plaintext: str) -> bytes:
        raise OSError("Secure credential storage needs Windows (DPAPI); refusing to store plaintext.")

    def unseal(blob) -> str:
        if not blob:
            return ""
        blob = bytes(blob)
        if blob.startswith(_PREFIX):
            raise OSError("DPAPI-sealed credential cannot be opened on this platform.")
        return blob.decode("utf-8", "replace")


# ── env-var discovery (portable apps register their DB; the other finds it) ──
def _read_persisted(name):
    """Current value of a user env var — process env first, else the persisted (setx) value."""
    v = os.environ.get(name)
    if v:
        return v
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None
    return None


def _persist(name, value):
    """Persist a user env var (setx) so the *other* app discovers us on future runs."""
    if _read_persisted(name) == value:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["setx", name, value], capture_output=True, timeout=5)
        os.environ[name] = value
    except Exception:
        pass


def amethyst_db():
    """Amethyst's DB: where it registered itself (AMETHYST_DB), else the sibling dir."""
    reg = _read_persisted("AMETHYST_DB")
    if reg and Path(reg).is_file():
        return Path(reg)
    sib = Path(__file__).resolve().parent.parent / "amethyst" / "data" / "amethyst.db"
    return sib if sib.is_file() else None


def standalone_db(register=False):
    p = Path(os.environ.get("T24_ENV_DB") or _read_persisted("T24_ENV_DB")
             or (Path.home() / ".t24tools" / "envs.db"))
    if register:
        _persist("T24_ENV_DB", str(p))
    return p


# environments schema — superset matching amethyst's table so either side can create it
_ENV_DDL = """CREATE TABLE IF NOT EXISTS environments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT UNIQUE NOT NULL, host TEXT NOT NULL, port INTEGER DEFAULT 22,
  username TEXT DEFAULT '', password_enc BLOB, groups TEXT DEFAULT '', tags TEXT DEFAULT '',
  bnk_run TEXT DEFAULT '', jbase_agent TEXT DEFAULT '', ear_name TEXT DEFAULT 'tocfee.ear',
  enabled INTEGER DEFAULT 1)"""


def _open(path, create=False):
    path = Path(path)
    if not create and not path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=4000")
    if create:
        c.executescript(_ENV_DDL)
    return c


def _read_envs(path, source, reveal):
    c = _open(path, create=False)
    if c is None:
        return []
    try:
        rows = c.execute("SELECT label,host,port,username,password_enc,bnk_run,enabled "
                         "FROM environments ORDER BY label").fetchall()
    except sqlite3.OperationalError:
        c.close()
        return []
    c.close()
    out = []
    for r in rows:
        if not r["enabled"]:
            continue
        e = {"label": r["label"], "host": r["host"], "port": r["port"] or 22,
             "user": r["username"] or "", "bnk": r["bnk_run"] or "", "source": source}
        if reveal:
            e["pass"] = unseal(r["password_enc"])
        out.append(e)
    return out


# ── public API ───────────────────────────────────────────────────────────────
def all_envs(reveal=False):
    """Union of every store (Amethyst DB + local standalone DB), deduped by label, so both
    sides see all hosts. Passwords unsealed only when reveal=True (connect time)."""
    envs, seen = [], set()
    for path, source in ((amethyst_db(), "amethyst"), (standalone_db(), "local")):
        if not path:
            continue
        for e in _read_envs(path, source, reveal):
            key = e["label"].lower()
            if key in seen:
                continue
            seen.add(key)
            envs.append(e)
    return envs


def backend_summary():
    am = amethyst_db()
    return {"amethyst_db": str(am) if am else None,
            "amethyst_envs": len(_read_envs(am, "amethyst", False)) if am else 0,
            "local_db": str(standalone_db()),
            "local_envs": len(_read_envs(standalone_db(), "local", False)),
            "total": len(all_envs())}


def add_local(label, host, port=22, username="", password=None, bnk_run=""):
    """Add/update a host in the LOCAL standalone store. t24-tools shows it immediately (union);
    Amethyst merges it in on next start. If password is None, any existing stored password is
    PRESERVED (so re-importing metadata never wipes a password you keyed)."""
    c = _open(standalone_db(register=True), create=True)
    if password is not None:
        c.execute("INSERT INTO environments(label,host,port,username,password_enc,bnk_run) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(label) DO UPDATE SET host=excluded.host, "
                  "port=excluded.port, username=excluded.username, "
                  "password_enc=excluded.password_enc, bnk_run=excluded.bnk_run",
                  (label, host, int(port or 22), username, seal(password), bnk_run or ""))
    else:
        c.execute("INSERT INTO environments(label,host,port,username,password_enc,bnk_run) "
                  "VALUES(?,?,?,?,NULL,?) ON CONFLICT(label) DO UPDATE SET host=excluded.host, "
                  "port=excluded.port, username=excluded.username, bnk_run=excluded.bnk_run",
                  (label, host, int(port or 22), username, bnk_run or ""))
    c.commit()
    c.close()


def codittle_envs():
    """Connection metadata from Codittle's DB (label/host/port/user/ssh_home) — NEVER the
    password (Codittle seals it with its own key). Used to seed our store; user keys passwords."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run([sys.executable, str(here / "codittle_connections.py"),
                              "--json", "--no-check"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return []
        import json
        data = json.loads(out.stdout or "{}")
    except Exception:
        return []
    res = []
    for c in data.get("connections", []):
        if not c.get("host"):
            continue
        res.append({"label": c.get("name") or c.get("host"), "host": c.get("host"),
                    "port": c.get("port") or 22, "user": c.get("username") or "",
                    "bnk": c.get("ssh_home") or "", "source": "codittle"})
    return res


def import_codittle():
    """Pull Codittle's connections into the local store (metadata only; passwords preserved).
    Returns the count imported. Run `t24_env.py passwd <label>` to key each password."""
    n = 0
    for e in codittle_envs():
        add_local(e["label"], e["host"], e["port"], e["user"], None, e["bnk"])
        n += 1
    return n


def set_password(label, password):
    """Key/replace an env's password in the local store (creating the row from union metadata
    if it only existed in Amethyst/Codittle). Returns 1 on success, 0 if label unknown."""
    c = _open(standalone_db(register=True), create=True)
    n = c.execute("UPDATE environments SET password_enc=? WHERE label=?",
                  (seal(password), label)).rowcount
    if n == 0:
        match = [e for e in all_envs(reveal=False) if e["label"].lower() == label.lower()]
        if not match:
            c.close()
            return 0
        m = match[0]
        c.execute("INSERT INTO environments(label,host,port,username,password_enc,bnk_run) "
                  "VALUES(?,?,?,?,?,?)",
                  (m["label"], m["host"], m["port"], m["user"], seal(password), m["bnk"]))
        n = 1
    c.commit()
    c.close()
    return n


def delete_local(label):
    c = _open(standalone_db(), create=False)
    if c is None:
        return 0
    n = c.execute("DELETE FROM environments WHERE label=?", (label,)).rowcount
    c.commit()
    c.close()
    return n


def _col(headers, *names):
    low = [h.strip().lower() for h in headers]
    for n in names:
        if n in low:
            return low.index(n)
    return None


def import_csv(path):
    """One-time import of a servers CSV into the local store; passwords sealed.
    Returns (imported, skipped). The CSV is never read at runtime — delete it after."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in _csv.reader(f) if any(c.strip() for c in r)]
    if not rows:
        return (0, 0)
    h = rows[0]
    ci = {"label": _col(h, "label"),
          "host": _col(h, "hostname/ip", "hostname / ip", "hostname", "host", "ip"),
          "port": _col(h, "port"), "user": _col(h, "username", "user"),
          "pwd": _col(h, "password", "pass", "pwd"),
          "bnk": _col(h, "bnk.run", "bnkrun", "bnk run", "path"),
          "proto": _col(h, "protocol", "proto")}
    if ci["host"] is None:
        raise ValueError("CSV has no recognizable Hostname/IP column")

    def cell(row, key):
        i = ci[key]
        return row[i].strip() if (i is not None and i < len(row)) else ""

    imported = skipped = 0
    for row in rows[1:]:
        if cell(row, "proto").lower() not in ("ssh", ""):
            skipped += 1
            continue
        host = cell(row, "host")
        if not host:
            skipped += 1
            continue
        add_local(cell(row, "label") or host, host, cell(row, "port") or 22,
                  cell(row, "user"), cell(row, "pwd") or None, cell(row, "bnk"))
        imported += 1
    return (imported, skipped)
