#!/usr/bin/env python3
"""Dump a T24/jBASE record over SSH, attribute-marks rendered readable, reusing
fetch_t24_sources for the connection + jBASE env loading.

  python t24_record.py --env 30 --servers ".../Test_Environments.csv" \
         --file F.VERSION "FUNDS.TRANSFER,MY.VERSION"

`CT` is a screen pager, so we feed it newlines to page through and strip the
terminal escapes. FM(254)->newline, VM(253)->' | ', SM(252)->' ^ '.
Try --file FBNK.VERSION if F.VERSION returns nothing.
"""
import argparse
import re
import sys

import fetch_t24_sources as ft

PROFILE = '"$HOME/.profile"'
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]|\x1b.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", required=True)
    ap.add_argument("--bnk", default=None, help="remote bnk.run (default: auto-detect per host)")
    ap.add_argument("--file", default="F.VERSION")
    ap.add_argument("--verb", default="CT", help="jBASE display verb (default CT)")
    ap.add_argument("--pages", type=int, default=30, help="newlines fed to page the pager")
    ap.add_argument("--raw", action="store_true", help="do not convert attribute marks")
    ap.add_argument("--cmd", help="run this literal jBASE command instead of <verb> <file> <id>")
    ap.add_argument("id", nargs="?", help="record id (may contain commas)")
    args = ap.parse_args()

    envs = ft.load_environments(args.servers)
    m = ft.select_env(envs, args.env)
    if len(m) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in m))
    client = ft.connect(m[0])
    bnk = ft.resolve_bnk(client, args.bnk, m[0]["bnk"])

    if args.cmd:
        command = args.cmd
    elif args.id:
        rid = args.id.replace("'", "'\\''")
        command = f"{args.verb} {args.file} '{rid}'"
    else:
        sys.exit("provide a record id or --cmd")
    feed = "for i in $(eval echo {1.." + str(args.pages) + "}); do echo; done | "
    conv = "" if args.raw else r" | tr '\376\375\374' '\n|^'"
    script = (
        f'cd "{bnk}" || {{ echo "cannot cd {bnk}"; exit 3; }}\n'
        f". <(sed '/jpqn.*loginproc/,$d' {PROFILE}) 2>/dev/null\n"
        f"{feed}{command}{conv}\n"
    )
    try:
        out = ft.run_script(client, script, timeout=90)
        out = ANSI.sub("", out)
        # collapse the blank lines the pager feed leaves behind
        out = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", out)
        print(out.rstrip())
    finally:
        client.close()


if __name__ == "__main__":
    main()
