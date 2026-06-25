#!/usr/bin/env python3
"""Run one command on a single environment, reusing t24_run's logic.
Set the target host via the T24_HOST env var (or edit TARGET below)."""
import os
import sys
import t24_run as t

TARGET = os.environ.get("T24_HOST", "")

def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "echo NO_COMMAND"
    envs = t.load_environments("Test_Environments.csv")
    match = [e for e in envs if e["host"] == TARGET]
    if not match:
        sys.exit(f"{TARGET} not found in CSV")
    env = match[0]
    print(f"# host={env['user']}@{env['host']}:{env['port']} bnk.run={env['bnk'] or '<none>'}")
    print(f"# command: {command}\n")
    status, code, output, timing = t.run_on_host(env, command, diagnose=False)
    print(f"# status={status} exit={code} {t.fmt_timing(timing, False)}\n")
    print(output)

if __name__ == "__main__":
    main()
