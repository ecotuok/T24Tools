#!/usr/bin/env python3
"""Manage the shared t24-tools / Amethyst environment store — no plaintext, no CSV at rest.

  python t24_env.py list                 # union of all stores (no passwords shown)
  python t24_env.py add [--label .. --host .. --user .. --port 22 --bnk ..]   # add/update (hidden pw)
  python t24_env.py passwd <label>       # key/replace a password (hidden)
  python t24_env.py remove <label>       # delete from the local store
  python t24_env.py import-codittle      # pull connection details from Codittle (no passwords)
  python t24_env.py import-tabby         # pull SSH profiles from Tabby's config.yaml
  python t24_env.py import-termius       # best-effort pull from Termius (often E2E-encrypted)
  python t24_env.py import servers.csv   # ONE-TIME seed from a CSV, then delete it

Hosts resolve from the UNION of Amethyst's DB and the local encrypted DB, so both sides see
every host. Passwords are sealed with Windows DPAPI. Codittle/Amethyst details are merged in,
so you add a server in ONE place and just key the password here.
"""
import argparse
import getpass
import sys

import envstore as es


def cmd_list(_a):
    s = es.backend_summary()
    if s["amethyst_db"]:
        print(f"Amethyst store : {s['amethyst_db']}  ({s['amethyst_envs']} envs)")
    print(f"Local store    : {s['local_db']}  ({s['local_envs']} envs)")
    print(f"Total (union)  : {s['total']} env(s)\n")
    rows = es.all_envs(reveal=True)          # reveal so we can flag which have no password yet
    if not rows:
        print("No environments yet.  Add one:   python t24_env.py add")
        print("or pull from Codittle:           python t24_env.py import-codittle")
        return
    print(f"{'LABEL':18} {'HOST':16} {'PORT':5} {'USER':10} {'SRC':9} {'PW':3} BNK.RUN")
    for e in rows:
        pw = "yes" if e.get("pass") else "--"
        print(f"{e['label']:18} {e['host']:16} {str(e['port']):5} {e['user']:10} "
              f"{e['source']:9} {pw:3} {e['bnk'] or '(auto)'}")
    if any(not e.get("pass") for e in rows):
        print("\n'--' under PW = no password yet.  Key it:  python t24_env.py passwd <label>")


def cmd_add(a):
    label = a.label or input("label: ").strip()
    host = a.host or input("host / IP: ").strip()
    if not label or not host:
        sys.exit("label and host are required")
    user = a.user if a.user is not None else input("username: ").strip()
    pw = getpass.getpass("password (hidden — sealed with DPAPI; blank to skip): ")
    es.add_local(label, host, a.port, user, pw or None, a.bnk or "")
    print(f"saved '{label}' to {es.standalone_db()}")


def cmd_passwd(a):
    pw = getpass.getpass(f"password for '{a.label}' (hidden, sealed): ")
    if not pw:
        sys.exit("no password entered")
    n = es.set_password(a.label, pw)
    print(f"password set for '{a.label}'" if n else
          f"unknown env '{a.label}' — add it first (t24_env.py add) or import-codittle")


def cmd_remove(a):
    n = es.delete_local(a.label)
    print(f"removed '{a.label}' from the local store" if n
          else f"no local env named '{a.label}' (Amethyst-DB envs are managed in Amethyst)")


def cmd_import_codittle(_a):
    try:
        n = es.import_codittle()
    except Exception as e:                    # noqa: BLE001 - surface any Codittle read failure
        sys.exit(f"could not read Codittle: {e}")
    if not n:
        sys.exit("no Codittle connections found (is the Codittle app installed, with connections?)")
    print(f"imported {n} connection(s) from Codittle (metadata only — no passwords).")
    print("Key each password:  python t24_env.py passwd <label>")


def cmd_import_tabby(_a):
    try:
        n = es.import_tabby()
    except Exception as e:                    # noqa: BLE001 - surface any read/parse failure
        sys.exit(f"Tabby import failed: {e}")
    if not n:
        sys.exit("no Tabby SSH profiles found (looked for %APPDATA%/tabby/config.yaml).")
    print(f"imported {n} profile(s) from Tabby (metadata only — no passwords).")
    print("Key each password:  python t24_env.py passwd <label>")


def cmd_import_termius(_a):
    n = es.import_termius()
    if not n:
        sys.exit("no readable Termius hosts found.  Termius is end-to-end encrypted, so host\n"
                 "details on disk are usually ciphertext — add them via Tabby/Codittle or manually.")
    print(f"imported {n} host(s) from Termius (metadata only — no passwords).")
    print("Key each password:  python t24_env.py passwd <label>")


def cmd_import(a):
    imported, skipped = es.import_csv(a.csv)
    print(f"imported {imported} env(s) into {es.standalone_db()} (skipped {skipped}); passwords sealed.")
    print(f"You can now DELETE {a.csv} — it is never read at runtime.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    ad = sub.add_parser("add")
    ad.add_argument("--label"); ad.add_argument("--host"); ad.add_argument("--user")
    ad.add_argument("--port", type=int, default=22); ad.add_argument("--bnk")
    pw = sub.add_parser("passwd"); pw.add_argument("label")
    rm = sub.add_parser("remove"); rm.add_argument("label")
    sub.add_parser("import-codittle")
    sub.add_parser("import-tabby")
    sub.add_parser("import-termius")
    im = sub.add_parser("import"); im.add_argument("csv")
    a = ap.parse_args()
    {"list": cmd_list, "add": cmd_add, "passwd": cmd_passwd, "remove": cmd_remove,
     "import-codittle": cmd_import_codittle, "import-tabby": cmd_import_tabby,
     "import-termius": cmd_import_termius, "import": cmd_import}[a.cmd](a)


if __name__ == "__main__":
    main()
