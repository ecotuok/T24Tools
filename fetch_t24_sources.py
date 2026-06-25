#!/usr/bin/env python3
"""
fetch_t24_sources.py -- pull T24 jBASE source files over SFTP.

Two ways to tell it WHAT to fetch (combine freely):

  1. JSHOW output  (--jshow FILE, or piped on stdin)
       Paste a raw JSHOW session straight from jsh -- prompt lines like
           jsh <user> ~ -->JSHOW -c MY.ROUTINE
       are ignored; only the
           jBC <ROUTINE> source file <BP>
       lines matter. Duplicates / "(DUP!!)" blocks collapse to one entry.

  2. Routine name  (--routine NAME, repeatable; and/or --routines-file FILE)
       The script SSH-execs `JSHOW -c NAME` on the server itself, reads back the
       source-file (BP), then pulls it. i.e. you just give the name.

WHERE it fetches from:  <remote-base>/<BP>/<ROUTINE>   (then <ROUTINE>.b)
WHERE it lands:         <dest>/<BP>/<ROUTINE>

If neither <ROUTINE> nor <ROUTINE>.b exists, the routine is SKIPPED (the source
was never deployed or was removed -- only the compiled .so object remains).

Server list + creds come from a CSV in t24_run.py's format; --env picks one row
by label (e.g. ENV-01), last IP octet (e.g. 30) or full IP. Read-only on the server.
Auth: password from the CSV (paramiko); host keys auto-accepted (test fleet).

Examples:
  # paste a JSHOW session
  JSHOW -c FOO | python fetch_t24_sources.py --env 30 --servers Test_Environments.csv

  # or just name routines and let it discover the paths
  python fetch_t24_sources.py --env 30 --servers Test_Environments.csv \
         -r MY.ROUTINE -r OTHER.ROUTINE
"""
import argparse
import csv
import os
import re
import select
import socket
import sys
import time

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is not installed. Run:  pip install paramiko")

DEFAULT_REMOTE_BASE = os.environ.get("T24_BNK_RUN", "/t24/bnk/bnk.run")
CONNECT_TIMEOUT = 20
EXEC_TIMEOUT = 60
# jBASE source can be stored as the bare name or with a .b suffix; try both.
SRC_SUFFIXES = ("", ".b")
# valid jBASE routine name -> safe to interpolate into a remote command
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# JSHOW line:  jBC <ROUTINE> source file <BP>
JSHOW_RE = re.compile(r"\bjBC\s+(\S+)\s+source file\s+(\S+)", re.IGNORECASE)
PROFILE_PATH = '"$HOME/.profile"'  # sourced (trimmed) so PATH/jBASE load without interactive login


def parse_jshow(text):
    """Ordered, de-duplicated list of (routine, source_file) pairs."""
    seen, pairs = set(), []
    for m in JSHOW_RE.finditer(text):
        key = (m.group(1), m.group(2))
        if key not in seen:
            seen.add(key)
            pairs.append(key)
    return pairs


# ---- CSV / environment selection (compatible with t24_run.load_environments) ----
def _norm(s):
    return (s or "").strip().lower()


def _find_col(headers, *aliases):
    norm = [_norm(h) for h in headers]
    for a in aliases:
        if a in norm:
            return norm.index(a)
    return None


def load_environments(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if not rows:
        return []
    headers = rows[0]
    ci = {
        "label": _find_col(headers, "label"),
        "host":  _find_col(headers, "hostname/ip", "hostname / ip", "hostname", "host", "ip"),
        "proto": _find_col(headers, "protocol", "proto"),
        "port":  _find_col(headers, "port"),
        "user":  _find_col(headers, "username", "user"),
        "pass":  _find_col(headers, "password", "pass", "pwd"),
        "bnk":   _find_col(headers, "bnk.run", "bnkrun", "bnk run", "path"),
    }
    if ci["host"] is None:
        sys.exit("CSV has no recognizable Hostname/IP column.")

    def cell(row, key):
        idx = ci[key]
        return row[idx].strip() if (idx is not None and idx < len(row)) else ""

    envs = []
    for row in rows[1:]:
        if _norm(cell(row, "proto")) not in ("ssh", ""):
            continue
        host = cell(row, "host")
        if not host:
            continue
        envs.append({
            "label": cell(row, "label") or host,
            "host":  host,
            "port":  int(cell(row, "port") or 22),
            "user":  cell(row, "user"),
            "pass":  cell(row, "pass"),
            "bnk":   cell(row, "bnk"),
        })
    return envs


def select_env(envs, selector):
    sel = selector.strip().lower()
    last_octet = lambda h: h.rsplit(".", 1)[-1]
    matches = []
    for e in envs:
        if sel in (e["label"].lower(), e["host"].lower()):
            return [e]
        if sel.isdigit() and last_octet(e["host"]) == sel:
            matches.append(e)
        elif not sel.isdigit() and sel in e["label"].lower():
            matches.append(e)
    uniq = []
    for e in matches:
        if e not in uniq:
            uniq.append(e)
    return uniq


# ------------------------------- SSH / SFTP ----------------------------------
def connect(env):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # trust-on-first-use test fleet
    client.connect(
        hostname=env["host"], port=env["port"],
        username=env["user"], password=env["pass"],
        timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
        allow_agent=False, look_for_keys=False,
    )
    return client


def run_script(client, script, timeout=EXEC_TIMEOUT):
    """Run a bash script on the host (non-login shell so jBASE loginproc never
    fires), return combined stdout+stderr text."""
    chan = client.get_transport().open_session(timeout=CONNECT_TIMEOUT)
    chan.exec_command("bash -s")
    chan.sendall(script)
    chan.shutdown_write()
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        if time.monotonic() > deadline:
            break
        select.select([chan], [], [], 1.0)
        if chan.recv_ready():
            buf += chan.recv(65536)
        if chan.recv_stderr_ready():
            buf += chan.recv_stderr(65536)
    while chan.recv_ready():
        buf += chan.recv(65536)
    while chan.recv_stderr_ready():
        buf += chan.recv_stderr(65536)
    return buf.decode("utf-8", "replace")


def remote_jshow(client, bnk, names):
    """Run `JSHOW -c NAME` for each name on the server; return parsed (routine, bp)
    pairs. Loads the T24 env first (cd bnk.run; source trimmed .profile)."""
    safe = [n for n in names if NAME_RE.match(n)]
    bad = [n for n in names if not NAME_RE.match(n)]
    for n in bad:
        print(f"[skip ] ignoring unsafe routine name: {n!r}")
    if not safe:
        return []
    jshow_lines = "\n".join(f"JSHOW -c {n}" for n in safe)
    script = (
        f'cd "{bnk}" || {{ echo "ERROR: cannot cd into {bnk}"; exit 3; }}\n'
        f". <(sed '/jpqn.*loginproc/,$d' {PROFILE_PATH}) 2>/dev/null\n"
        f"{jshow_lines}\n"
    )
    out = run_script(client, script)
    return parse_jshow(out)


def fetch_one(sftp, base, bp, routine, dest):
    """Try <base>/<bp>/<routine> then <routine>.b. Returns (status, detail)."""
    local_dir = os.path.join(dest, bp)
    for suffix in SRC_SUFFIXES:
        remote = f"{base}/{bp}/{routine}{suffix}"
        local = os.path.join(local_dir, routine + suffix)
        try:
            os.makedirs(local_dir, exist_ok=True)
            sftp.get(remote, local)
            return ("OK", f"{bp}/{routine}{suffix}  ({os.path.getsize(local)} bytes)")
        except IOError:
            continue
    return ("SKIP", f"{bp}/{routine}  (no source or .b on this env -- object-only)")


def main():
    ap = argparse.ArgumentParser(
        description="Fetch T24 jBASE sources by JSHOW paste or by routine name, over SFTP.")
    ap.add_argument("--env", required=True,
                    help="environment selector: label (UAT-KE-30), last IP octet (30), or full IP")
    ap.add_argument("--jshow", help="file with JSHOW output (use '-' or omit to read stdin if no -r given)")
    ap.add_argument("-r", "--routine", action="append", default=[], metavar="NAME",
                    help="routine name to discover via remote JSHOW and fetch (repeatable)")
    ap.add_argument("--routines-file", help="file with one routine name per line")
    ap.add_argument("--servers", default="Test_Environments.csv",
                    help="server CSV (default ./Test_Environments.csv)")
    ap.add_argument("--dest", default=".", help="local destination root (default: current dir)")
    ap.add_argument("--remote-base", default=None,
                    help=f"remote source root (default: env bnk.run, else {DEFAULT_REMOTE_BASE})")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan; for name mode this still SSH-execs JSHOW (read-only) but downloads nothing")
    args = ap.parse_args()

    names = list(args.routine)
    if args.routines_file:
        with open(args.routines_file, encoding="utf-8") as f:
            names += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    # JSHOW text: from --jshow / stdin. Only auto-read stdin when no names were given
    # (so `-r FOO` alone doesn't block waiting on stdin).
    jtext = ""
    if args.jshow and args.jshow != "-":
        with open(args.jshow, encoding="utf-8", errors="replace") as f:
            jtext = f.read()
    elif args.jshow == "-" or (not names and not sys.stdin.isatty()):
        jtext = sys.stdin.read()

    pairs_from_text = parse_jshow(jtext) if jtext else []
    if not pairs_from_text and not names:
        sys.exit("Nothing to fetch: give a JSHOW paste (--jshow/stdin) or routine names (-r).")

    # environment
    if not os.path.isfile(args.servers):
        sys.exit(f"Server CSV not found: {args.servers}")
    envs = load_environments(args.servers)
    matches = select_env(envs, args.env)
    if not matches:
        sys.exit(f"No environment matched --env {args.env!r}. Available: "
                 + ", ".join(f"{e['label']}({e['host']})" for e in envs))
    if len(matches) > 1:
        sys.exit(f"--env {args.env!r} is ambiguous: "
                 + ", ".join(f"{e['label']}({e['host']})" for e in matches))
    env = matches[0]
    base = args.remote_base or env["bnk"] or DEFAULT_REMOTE_BASE

    print(f"# target : {env['label']}  {env['user']}@{env['host']}:{env['port']}")
    print(f"# remote : {base}/<BP>/<ROUTINE>[.b]")
    print(f"# dest   : {os.path.abspath(args.dest)}{os.sep}<BP>{os.sep}<ROUTINE>")

    # connect if we need the server (name discovery, or any real download)
    need_server = bool(names) or not args.dry_run
    client = None
    if need_server:
        try:
            client = connect(env)
        except paramiko.AuthenticationException:
            sys.exit("Authentication failed (check username/password in CSV).")
        except (socket.timeout, TimeoutError):
            sys.exit(f"Connect timed out after {CONNECT_TIMEOUT}s (on the bank network / VPN?).")
        except Exception as e:
            sys.exit(f"Connect failed for {env['host']}: {e}")

    # discover name-mode routines via remote JSHOW
    pairs = list(pairs_from_text)
    if names:
        print(f"# discovering {len(names)} routine(s) via remote JSHOW on {env['host']} ...")
        discovered = remote_jshow(client, base, names)
        found_names = {r for r, _ in discovered}
        for n in names:
            if n not in found_names:
                print(f"[skip ] JSHOW found no source-file mapping for {n} (object-only or unknown)")
        for key in discovered:
            if key not in pairs:
                pairs.append(key)

    if not pairs:
        if client:
            client.close()
        sys.exit("No (routine, source-file) pairs resolved -- nothing to fetch.")

    by_bp = {}
    for routine, bp in pairs:
        by_bp.setdefault(bp, []).append(routine)
    print(f"# {len(pairs)} routine(s) across {len(by_bp)} source folder(s):")
    for bp in sorted(by_bp):
        print(f"#   {bp}: {', '.join(sorted(by_bp[bp]))}")
    print()

    if args.dry_run:
        print("DRY RUN -- nothing downloaded.")
        if client:
            client.close()
        return

    # download
    sftp = client.open_sftp()
    ok = skip = 0
    for routine, bp in pairs:
        status, detail = fetch_one(sftp, base, bp, routine, args.dest)
        tag = "[OK]  " if status == "OK" else "[SKIP]"
        print(f"{tag} {detail}")
        ok += status == "OK"
        skip += status == "SKIP"
    sftp.close()
    client.close()
    print(f"\nSUMMARY: {ok} downloaded, {skip} skipped, {len(pairs)} total.")


if __name__ == "__main__":
    main()
