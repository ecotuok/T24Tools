#!/usr/bin/env python3
"""Probe a T24 host purely over SFTP (no remote shell / PATH needed).

  - stat each positional remote PATH (says exists/dir/file + size)
  - --list DIR dumps a directory's entries, optionally filtered by --grep

Reuses fetch_t24_sources for env resolution + the connection.

  python probe_t24.py --env 30 --list "$T24_BNK_RUN" --grep .BP \
         "$T24_BNK_RUN/SOME.BP" "$T24_BNK_RUN/OTHER.BP"
"""
import argparse
import stat
import sys

import fetch_t24_sources as ft


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", default=None, help=argparse.SUPPRESS)  # deprecated: shared store
    ap.add_argument("--list", action="append", default=[], metavar="DIR",
                    help="list a remote directory (repeatable)")
    ap.add_argument("--grep", default="", help="only show --list entries containing this substring")
    ap.add_argument("paths", nargs="*", help="remote paths to stat")
    args = ap.parse_args()

    envs = ft.load_environments(args.servers)
    m = ft.select_env(envs, args.env)
    if len(m) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in m))
    if not m[0].get("pass"):
        sys.exit(f"no password for '{m[0]['label']}' — run: python t24_env.py passwd \"{m[0]['label']}\"")
    client = ft.connect(m[0])
    sftp = client.open_sftp()
    try:
        for p in args.paths:
            try:
                st = sftp.stat(p)
                kind = "dir " if stat.S_ISDIR(st.st_mode) else "file"
                print(f"[exists {kind}] {p}  ({st.st_size} bytes)")
            except IOError:
                print(f"[missing    ] {p}")
        for d in args.list:
            print(f"\n== listing {d}" + (f"  (filter: '{args.grep}')" if args.grep else "") + " ==")
            try:
                entries = sorted(sftp.listdir(d))
            except IOError as e:
                print(f"  cannot list: {e}")
                continue
            except UnicodeDecodeError:
                print("  (directory has non-UTF8 jBASE filenames; use stat on exact paths instead)")
                continue
            shown = [e for e in entries if args.grep in e]
            for e in shown:
                print(f"  {e}")
            print(f"  ({len(shown)} shown / {len(entries)} total)")
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    main()
