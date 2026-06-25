#!/usr/bin/env python3
"""
codittle_db.py -- inspect / back up / RECOVER Codittle's embedded PGlite database.

Codittle stores SSH connections (and projects) in a per-workspace PGlite database at
  %LOCALAPPDATA%\\Codittle\\workspaces\\<id>\\pgdata
If that DB corrupts, Codittle quarantines it as `pgdata.corrupt-<ts>` and starts a
FRESH EMPTY one -> your connections vanish from the UI even though the data still
exists in another workspace (or a backup). This tool finds it and puts it back.

  python codittle_db.py status                          # workspaces, the active one,
                                                         #   connection counts, corruption flags
  python codittle_db.py connections [--workspace ID]     # list connections in a workspace
  python codittle_db.py backup [--all]                   # snapshot pgdata -> DevTools/_codittle-db-backups/
  python codittle_db.py restore [--from ID|PATH] [--apply]
        # copy connections INTO the active workspace, re-pointed to its stream.
        # Auto-source = the other workspace with the most connections.

SAFE BY DEFAULT:
  * restore is a DRY RUN unless --apply
  * --apply REFUSES to run while Codittle is open (writing a live DB is what corrupts it)
  * --apply always backs up the active DB first
Requires Codittle installed (uses its bundled node.exe for PGlite); read-only on sources.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import codittle_connections as cc   # reuse the Codittle node.exe finder

WS = Path(os.environ.get("LOCALAPPDATA", "")) / "Codittle" / "workspaces"
MJS = Path(__file__).resolve().parent / "codittle_db.mjs"
BACKUPS = Path(__file__).resolve().parents[1] / "_codittle-db-backups"   # DevTools/_codittle-db-backups


def _node():
    n = cc._find_codittle_node()
    if not n:
        sys.exit("ERROR: Codittle node.exe not found (install/run Codittle once).")
    return str(n)


def _mjs(*args):
    out = subprocess.run([_node(), str(MJS), *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"db query failed: {out.stderr.strip() or out.stdout.strip()}")
    return json.loads(out.stdout or "null")


def _codittle_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=5).stdout.lower()
        return "codittle-desktop.exe" in out
    except Exception:
        return False


def _status():
    return _mjs("status")


def _active(st):
    return next((w for w in st if w.get("active")), st[0] if st else None)


def _best_source(st):
    cands = [w for w in st if not w.get("active") and (w["connections"] or 0) > 0]
    return max(cands, key=lambda w: w["connections"], default=None)


def _resolve_pgdata(ref):
    p = Path(ref)
    if p.name == "pgdata" and p.is_dir():
        return str(p)
    if (p / "pgdata").is_dir():
        return str(p / "pgdata")
    if (WS / ref / "pgdata").is_dir():
        return str(WS / ref / "pgdata")
    if p.is_dir():
        subs = list(p.glob("*-pgdata")) or list(p.glob("*/pgdata"))
        if subs:
            return str(subs[0])
    sys.exit(f"could not resolve a pgdata directory from '{ref}'")


# ── commands ────────────────────────────────────────────────────────────────
def cmd_status(_):
    st = _status()
    print(f"workspaces: {WS}\n")
    for w in st:
        tag = "ACTIVE" if w.get("active") else "  --  "
        conn = w["connections"] if w["connections"] >= 0 else "UNREADABLE(corrupt)"
        corr = f"  corrupt-snapshots={len(w['corrupt_snapshots'])}" if w["corrupt_snapshots"] else ""
        print(f"[{tag}] {w['workspace']}  connections={conn}  projects={w['projects']}{corr}")
    a = _active(st)
    if a and (a["connections"] or 0) == 0:
        src = _best_source(st)
        if src:
            print(f"\n!  active workspace has 0 connections — {src['workspace']} has {src['connections']}.")
            print("   recover (close Codittle first):")
            print(f"     python codittle_db.py restore --from {src['workspace']} --apply")


def cmd_connections(args):
    st = _status()
    w = next((x for x in st if x["workspace"] == args.workspace), None) if args.workspace else _active(st)
    if not w:
        sys.exit("workspace not found")
    rows = _mjs("connections", w["pgdata"])
    for r in rows:
        print(f"  {r['name']:12} {r['username']}@{r['host']}:{r['port']}  {r.get('ssh_home') or ''}")
    print(f"\n{len(rows)} connections in {w['workspace']}")


def cmd_backup(args):
    st = _status()
    dest = BACKUPS / f"{datetime.now():%Y%m%d-%H%M%S}-manual"
    for w in (st if args.all else [_active(st)]):
        d = dest / f"{w['workspace']}-pgdata"
        shutil.copytree(w["pgdata"], d)
        print(f"backed up {w['workspace']} -> {d}")


def cmd_restore(args):
    st = _status()
    dst = _active(st)
    if not dst:
        sys.exit("no active workspace found")
    src = _resolve_pgdata(args.from_) if args.from_ else (_best_source(st) or {}).get("pgdata")
    if not src:
        sys.exit("no other workspace has connections — pass --from <id|backup-path>")
    print(f"source      : {src}")
    print(f"target(active): {dst['pgdata']}")

    if not args.apply:
        res = _mjs("restore", src, dst["pgdata"], "dry")
        print(f"\nDRY RUN — would restore {res['would_restore']}: {', '.join(res['names'])}")
        print("Close Codittle, then re-run with --apply to write.")
        return

    if _codittle_running() and not args.force:
        sys.exit("Codittle is RUNNING — close it first (writing a live DB corrupts it). "
                 "Quit Codittle fully, then re-run. (--force to override, not advised.)")

    bk = BACKUPS / f"{datetime.now():%Y%m%d-%H%M%S}-preapply" / f"{dst['workspace']}-pgdata"
    shutil.copytree(dst["pgdata"], bk)
    print(f"backup      : {bk}")
    res = _mjs("restore", src, dst["pgdata"], "apply")
    print(f"\nrestored {res['added']} connection(s): {', '.join(res['names'])}")
    print(f"active workspace now has {res['total']}. Reopen Codittle to see them.")
    print("(passwords are copied encrypted — if any fails to auth, re-enter it; they're in Test_Environments.csv)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("connections"); c.add_argument("--workspace")
    b = sub.add_parser("backup"); b.add_argument("--all", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("--from", dest="from_", metavar="ID|PATH", help="source workspace id or backup path")
    r.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    r.add_argument("--force", action="store_true", help="write even if Codittle seems to be running")
    args = ap.parse_args()
    {"status": cmd_status, "connections": cmd_connections,
     "backup": cmd_backup, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    main()
