#!/usr/bin/env python3
"""
t24_run.py -- interactive multi-server command runner for T24 test environments.

Reads a CSV of environments, then runs ONE command on every SSH host in parallel.
On each host it:
    1. cd's into that host's bnk.run path (from the CSV)
    2. sources a trimmed .profile so PATH/env load WITHOUT the interactive jBASE
       login process:   . <(sed '/jpqn.*loginproc/,$d' "$HOME/.profile")
    3. runs your command

CSV columns (header row required; order/spacing flexible, matched by name):
    Groups, Label, Tags, Hostname/IP, Protocol, Port, Username, Password, bnk.run

Auth: password from the CSV (paramiko). Host keys auto-accepted (test fleet).
Requires: Python 3.8+, paramiko  (pip install paramiko)
"""

import csv
import os
import re
import select
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import paramiko
except ImportError:
    sys.exit("paramiko is not installed. Run:  pip install paramiko")

# ------------------------------- tunables ------------------------------------
DEFAULT_CSV       = "Test_Environments"     # base name tried first (.csv also tried)
CMD_LIBRARY       = ".t24_cmd_library.tsv"  # saved labelled commands (label<TAB>command)
CONNECT_TIMEOUT   = 15                       # TCP/auth connect timeout (seconds)
CMD_TIMEOUT       = 60                        # hard cap per host for the command (seconds)
MAX_WORKERS       = 6                         # how many hosts to hit at once
APPEND_BNKRUN_ARG = False                     # True = append bnk.run path as last arg to your command
PROFILE_PATH      = '"$HOME/.profile"'        # remote-evaluated. .profile lives in bnk.run and we cd
                                              # there first, so change to '.profile' if $HOME != bnk.run.
STRIP_ANSI        = True                      # strip terminal escape codes (e.g. T24's clear-screen) from output
# ------------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")


def clean(text: str) -> str:
    return ANSI_RE.sub("", text) if STRIP_ANSI else text


# ============================ CSV / environments ==============================
def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _find_col(headers, *aliases):
    norm = [_norm(h) for h in headers]
    for a in aliases:
        if a in norm:
            return norm.index(a)
    return None


def load_environments(path):
    """Return a list of dicts for SSH hosts only."""
    hosts = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return hosts
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

    for row in rows[1:]:
        proto = _norm(cell(row, "proto"))
        if proto not in ("ssh", ""):
            continue  # skip RDP/HTTP/etc.
        host = cell(row, "host")
        if not host:
            continue
        hosts.append({
            "label": cell(row, "label") or host,
            "host":  host,
            "port":  int(cell(row, "port") or 22),
            "user":  cell(row, "user"),
            "pass":  cell(row, "pass"),
            "bnk":   cell(row, "bnk"),
        })
    return hosts


def find_csv():
    for cand in (DEFAULT_CSV, DEFAULT_CSV + ".csv"):
        if os.path.isfile(cand):
            return cand
    print(f"Could not find '{DEFAULT_CSV}' (or '{DEFAULT_CSV}.csv') in {os.getcwd()}.")
    while True:
        name = input("Enter the CSV filename to use: ").strip().strip('"')
        if name and os.path.isfile(name):
            return name
        print(f"  '{name}' not found — try again.")


# ============================ command library =================================
def load_library():
    lib = []
    if os.path.isfile(CMD_LIBRARY):
        with open(CMD_LIBRARY, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" in line:
                    label, cmd = line.split("\t", 1)
                    if label:
                        lib.append((label, cmd))
    return lib


def save_to_library(label, command):
    lib = [(l, c) for (l, c) in load_library() if l != label]
    lib.append((label, command))
    with open(CMD_LIBRARY, "w", encoding="utf-8") as f:
        for l, c in lib:
            f.write(f"{l}\t{c}\n")


def choose_command():
    lib = load_library()
    command = ""
    if lib:
        print(f"Saved commands (from {CMD_LIBRARY}):")
        for i, (label, cmd) in enumerate(lib, 1):
            print(f"   {i:>2}) [{label}]  {cmd}")
        print()
        pick = input("Pick a number (or label) to reuse, or press Enter to type a NEW command: ").strip()
        if pick:
            if pick.isdigit():
                idx = int(pick) - 1
                if 0 <= idx < len(lib):
                    command = lib[idx][1]
                else:
                    print(f"No saved command numbered '{pick}' — you'll enter a new one.")
            else:
                for label, cmd in lib:
                    if label == pick:
                        command = cmd
                        break
                if not command:
                    print(f"No saved command labelled '{pick}' — you'll enter a new one.")
        if command:
            print(f"Selected: {command}")

    if not command:
        command = input("Enter the global command to run on all servers:\n> ").strip()
        if not command:
            sys.exit("No command entered. Aborting.")
        newlabel = input("Label for this command (to reuse later; blank = do not save): ").strip()
        if newlabel:
            save_to_library(newlabel, command)
            print(f"Saved as '[{newlabel}]' in {CMD_LIBRARY}.")
    return command


# ============================ remote execution ================================
def build_remote_script(command, bnk, diagnose=False):
    cmdline = f'{command} "{bnk}"' if APPEND_BNKRUN_ARG else command
    src = f". <(sed '/jpqn.*loginproc/,$d' {PROFILE_PATH}) 2>/dev/null\n"
    cd  = f'cd "{bnk}" || {{ echo "ERROR: cannot cd into bnk.run: {bnk}"; exit 3; }}\n'
    if not diagnose:
        return cd + src + f"{cmdline}\n"
    # Diagnose: emit phase markers (whole seconds since shell start) to stderr.
    # SECONDS resets at shell start, so 'sourced' = .profile load time and
    # 'cmd_done' - 'sourced' = the command's own time. Connect time is measured
    # on the Python side.
    return (
        cd
        + 'echo "T24DIAG cd_done $SECONDS" >&2\n'
        + src
        + 'echo "T24DIAG sourced $SECONDS" >&2\n'
        + f"{cmdline}\n"
        + '__rc=$?\n'
        + 'echo "T24DIAG cmd_done $SECONDS" >&2\n'
        + 'exit $__rc\n'
    )


DIAG_RE = re.compile(r"^T24DIAG (cd_done|sourced|cmd_done) (\d+)\s*$", re.MULTILINE)


def run_on_host(env, command, diagnose=False):
    """Connect, run the remote bash script.

    Returns (status, exit_code, output, timing) where timing is a dict with
    'connect' and 'exec' seconds (always) and, in diagnose mode, 'profile' and
    'command' seconds parsed from the remote phase markers.
    """
    timing = {}
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # trust-on-first-use test fleet
    t0 = time.monotonic()
    try:
        client.connect(
            hostname=env["host"], port=env["port"],
            username=env["user"], password=env["pass"],
            timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
            allow_agent=False, look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        return ("FAILED", None, "authentication failed (check username/password in CSV)", timing)
    except (socket.timeout, TimeoutError):
        return ("TIMEOUT", None, f"could not connect within {CONNECT_TIMEOUT}s", timing)
    except paramiko.ssh_exception.NoValidConnectionsError:
        return ("FAILED", None, "connection refused / port closed", timing)
    except Exception as e:  # DNS, network unreachable, etc.
        return ("FAILED", None, f"connect error: {e}", timing)
    timing["connect"] = time.monotonic() - t0

    try:
        script = build_remote_script(command, env["bnk"], diagnose=diagnose)
        chan = client.get_transport().open_session(timeout=CONNECT_TIMEOUT)
        chan.exec_command("bash -s")          # non-login shell -> jBASE loginproc never fires
        chan.sendall(script)
        chan.shutdown_write()

        t1 = time.monotonic()
        buf = bytearray()
        deadline = t1 + CMD_TIMEOUT
        timed_out = False
        while True:
            done = chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready()
            if done:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            select.select([chan], [], [], min(remaining, 1.0))
            if chan.recv_ready():
                buf += chan.recv(65536)
            if chan.recv_stderr_ready():
                buf += chan.recv_stderr(65536)
        # final drain
        while chan.recv_ready():
            buf += chan.recv(65536)
        while chan.recv_stderr_ready():
            buf += chan.recv_stderr(65536)
        timing["exec"] = time.monotonic() - t1

        text = buf.decode("utf-8", "replace")
        # pull out diagnose markers and remove them from the shown output
        marks = {k: int(v) for k, v in DIAG_RE.findall(text)}
        if "sourced" in marks:
            timing["profile"] = marks["sourced"]
        if "sourced" in marks and "cmd_done" in marks:
            timing["command"] = marks["cmd_done"] - marks["sourced"]
        text = DIAG_RE.sub("", text)
        output = clean(text).strip()

        if timed_out:
            try:
                chan.close()
            except Exception:
                pass
            return ("TIMEOUT", None, output or f"no response within {CMD_TIMEOUT}s", timing)

        exit_code = chan.recv_exit_status()
        # T24 verbs return unreliable exit codes -> "OK" means we connected and ran.
        return ("OK", exit_code, output, timing)
    except (socket.timeout, TimeoutError):
        return ("TIMEOUT", None, f"command exceeded {CMD_TIMEOUT}s", timing)
    except Exception as e:
        return ("FAILED", None, f"run error: {e}", timing)
    finally:
        client.close()


# ================================== main ======================================
def indent(text, pad="      "):
    if not text:
        return ""
    return "\n".join(pad + line for line in text.splitlines())


def fmt_timing(timing, diagnose):
    if not timing:
        return ""
    parts = [f"connect={timing['connect']:.1f}s"] if "connect" in timing else []
    if diagnose:
        if "profile" in timing:
            parts.append(f"profile={timing['profile']}s")
        if "command" in timing:
            parts.append(f"command={timing['command']}s")
    if "exec" in timing:
        parts.append(f"total={timing['connect'] + timing['exec']:.1f}s"
                     if "connect" in timing else f"exec={timing['exec']:.1f}s")
    return "  ".join(parts)


def main():
    global CMD_TIMEOUT, MAX_WORKERS
    import argparse
    ap = argparse.ArgumentParser(description="Run one command across T24 test servers in parallel.")
    ap.add_argument("-d", "--diagnose", action="store_true",
                    help="time each phase per host (connect / .profile load / command) to find slow ones")
    ap.add_argument("-t", "--timeout", type=int, metavar="SEC",
                    help=f"per-host command timeout in seconds (default {CMD_TIMEOUT}; "
                         "diagnose mode defaults to 300)")
    ap.add_argument("-w", "--workers", type=int, metavar="N",
                    help=f"max hosts to run at once (default {MAX_WORKERS})")
    args = ap.parse_args()

    diagnose = args.diagnose
    if args.timeout:
        CMD_TIMEOUT = args.timeout
    elif diagnose:
        CMD_TIMEOUT = 300            # give slow hosts room so we can actually measure them
    if args.workers:
        MAX_WORKERS = args.workers

    csv_path = find_csv()
    print(f"Using environment file: {csv_path}")
    envs = load_environments(csv_path)
    if not envs:
        sys.exit("No SSH hosts parsed from the CSV. Check the header/columns.")

    print(f"\nParsed {len(envs)} SSH environment(s):")
    for i, e in enumerate(envs, 1):
        print(f"  {i:>2}) {e['label']:<18} {e['user']}@{e['host']}:{e['port']}   bnk.run={e['bnk'] or '<none>'}")
    print()

    command = choose_command()

    print(f"\nAbout to run on {len(envs)} host(s):\n   {command}")
    if input("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        sys.exit("Cancelled.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"t24_results_{ts}.log"
    icons = {"OK": "[OK]     ", "FAILED": "[FAILED] ", "TIMEOUT": "[TIMEOUT]"}

    mode = "DIAGNOSE (timing each phase)" if diagnose else "run"
    print(f"\nMode: {mode}.  Running on {len(envs)} host(s), up to {MAX_WORKERS} at a time "
          f"(timeout {CMD_TIMEOUT}s)...\n")
    results = {}
    ok = fail = 0
    done = 0
    total = len(envs)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_env = {pool.submit(run_on_host, e, command, diagnose): e for e in envs}
        for future in as_completed(future_to_env):
            e = future_to_env[future]
            status, code, output, timing = future.result()
            results[e["host"]] = (status, code, output, timing, e)
            done += 1
            if status == "OK":
                ok += 1
            else:
                fail += 1
            tag = icons.get(status, status)
            tstr = fmt_timing(timing, diagnose)
            line = f"[{done:>2}/{total}] {tag} {e['label']:<18} ({e['user']}@{e['host']}:{e['port']})"
            if tstr:
                line += f"   {tstr}"
            print(line)
            if output:
                print(indent(output))
            print(flush=True)

    # write full log in CSV order
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"T24 multi-server run - {ts}\n")
        lf.write(f"Command: {command}\n")
        lf.write(f"CSV: {csv_path}   Hosts: {total}   Mode: {mode}\n")
        lf.write("=" * 64 + "\n")
        for e in envs:
            status, code, output, timing, _ = results[e["host"]]
            tstr = fmt_timing(timing, diagnose)
            lf.write(f"\n----- [{status}] {e['label']} ({e['user']}@{e['host']}:{e['port']})  "
                     f"exit={code}  {tstr} -----\n")
            lf.write((output or "") + "\n")
        lf.write("\n" + "=" * 64 + "\n")
        lf.write(f"SUMMARY:  {ok} OK,  {fail} failed,  {total} total.\n")

    print("=" * 64)
    print(f"SUMMARY:  {ok} OK,  {fail} failed,  {total} total.")
    if fail:
        print("Failed hosts:")
        for e in envs:
            status, code, output, timing, _ = results[e["host"]]
            if status != "OK":
                reason = (output or "").splitlines()[0] if output else status
                tstr = fmt_timing(timing, diagnose)
                print(f"   {icons.get(status, status)} {e['label']:<18} {e['host']}  - {reason}"
                      + (f"   ({tstr})" if tstr else ""))
    if diagnose:
        print("\nDiagnose key: connect=SSH login, profile=time sourcing .profile, "
              "command=time the verb itself ran.")
    print(f"Full log: {log_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
