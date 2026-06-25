#!/usr/bin/env python3
"""Remote grep across the T24 jBASE source tree (the *.BP dirs), reusing
fetch_t24_sources for the connection. Loads the jBASE environment first so the
PATH (grep, etc.) is available in the non-login shell.

  python grep_t24.py --env 30 --servers ".../Test_Environments.csv" \
         --dirs "*.BP" SOME.FIELD ANOTHER.FIELD THIRD.FIELD
"""
import argparse
import sys

import fetch_t24_sources as ft

PROFILE = '"$HOME/.profile"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", required=True)
    ap.add_argument("--bnk", default=ft.DEFAULT_REMOTE_BASE)
    ap.add_argument("--dirs", default="*.BP", help="glob of dirs to search (default *.BP)")
    ap.add_argument("--max", type=int, default=40, help="max match lines shown per pattern")
    ap.add_argument("--files-only", action="store_true", help="list matching files, not lines")
    ap.add_argument("--find", action="store_true",
                    help="treat patterns as FILENAME globs and locate source files (find), not grep contents")
    ap.add_argument("patterns", nargs="+", help="fixed-string patterns (grep) or filename globs (--find)")
    args = ap.parse_args()

    envs = ft.load_environments(args.servers)
    m = ft.select_env(envs, args.env)
    if len(m) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in m))
    client = ft.connect(m[0])
    try:
        if args.find:
            # pure bash globbing (no external find/ls — jBASE shadows PATH)
            globs = " ".join(f"{args.dirs}/{p}" for p in args.patterns)
            script = (
                f'cd "{args.bnk}" || {{ echo "cannot cd {args.bnk}"; exit 3; }}\n'
                "shopt -s nullglob\n"
                f"for f in {globs}; do echo \"$f\"; done | sort\n"
            )
            out = ft.run_script(client, script, timeout=150).strip()
            print("=================== files matching " + " ".join(args.patterns) + " ===================")
            print(out if out else "  (no matches)")
            return
        for pat in args.patterns:
            flag = "-rIlF" if args.files_only else "-rInHF"
            script = (
                f'cd "{args.bnk}" || {{ echo "cannot cd {args.bnk}"; exit 3; }}\n'
                f". <(sed '/jpqn.*loginproc/,$d' {PROFILE}) 2>/dev/null\n"
                f"grep {flag} -- '{pat}' {args.dirs} 2>/dev/null | head -n {args.max}\n"
            )
            out = ft.run_script(client, script, timeout=150).strip()
            print(f"=================== '{pat}' ===================")
            print(out if out else "  (no matches)")
            print()
    finally:
        client.close()


if __name__ == "__main__":
    main()
