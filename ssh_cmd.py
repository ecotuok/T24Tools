#!/usr/bin/env python3
"""Run ONE raw shell command on a single env over SSH (no profile, no jBASE).

Unlike t24_run.py (interactive, multi-host, sources the trimmed profile) this
is a plain exec_command for OS-level diagnostics (ss, netstat, strace, time).

  python ssh_cmd.py --env 174 'ss -tn state syn-sent'
"""
import argparse
import sys

import fetch_t24_sources as ft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", default=None, help=argparse.SUPPRESS)  # deprecated: shared store
    ap.add_argument("--timeout", type=float, default=120,
                    help="per-command timeout, seconds")
    ap.add_argument("command")
    args = ap.parse_args()

    envs = ft.load_environments(args.servers)
    m = ft.select_env(envs, args.env)
    if len(m) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in m))
    if not m[0].get("pass"):
        sys.exit(f"no password for '{m[0]['label']}' — run: python t24_env.py passwd \"{m[0]['label']}\"")
    client = ft.connect(m[0])
    try:
        _, stdout, stderr = client.exec_command(args.command, timeout=args.timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        sys.exit(rc)
    finally:
        client.close()


if __name__ == "__main__":
    main()
