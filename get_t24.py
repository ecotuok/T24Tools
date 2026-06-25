#!/usr/bin/env python3
"""get_t24.py -- download a file from a T24 host over SFTP, in BINARY, with verify.

Counterpart of put_t24.py: SFTP is binary-safe (no CRLF/encoding mangling that
text-mode pulls through stdout suffer on Windows), and the local copy is
md5-verified against the remote.

Usage:
    python get_t24.py --env 30 "$T24_BNK_RUN/SOME.BP/MY.ROUTINE" MY.ROUTINE.local
NB: pass remote paths with MSYS_NO_PATHCONV=1 under Git-Bash.
"""
import argparse
import hashlib
import sys

from fetch_t24_sources import load_environments, select_env, connect


def md5_local(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", default=None, help=argparse.SUPPRESS)  # deprecated: shared store
    ap.add_argument("remote")
    ap.add_argument("local")
    args = ap.parse_args()

    envs = load_environments(args.servers)
    m = select_env(envs, args.env)
    if len(m) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in m))
    if not m[0].get("pass"):
        sys.exit(f"no password for '{m[0]['label']}' — run: python t24_env.py passwd \"{m[0]['label']}\"")
    client = connect(m[0])
    try:
        sftp = client.open_sftp()
        st = sftp.stat(args.remote)
        sftp.get(args.remote, args.local)
        h = hashlib.md5()
        with sftp.open(args.remote, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        remote_md5 = h.hexdigest()
        local_md5 = md5_local(args.local)
        print(f"remote: {args.remote}  ({st.st_size} bytes, mode {oct(st.st_mode & 0o777)})")
        print(f"local : {args.local}  md5 {local_md5}")
        if local_md5 != remote_md5:
            sys.exit(f"MD5 MISMATCH: remote {remote_md5}")
        print("md5 verified OK")
    finally:
        client.close()


if __name__ == "__main__":
    main()
