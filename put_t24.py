#!/usr/bin/env python3
"""put_t24.py -- upload a file to a T24 host over SFTP, in BINARY, with verify.

Why: jars/binaries pushed through text-mode transfers (terminal paste, WinSCP
text mode) get CRLF/UTF-8 mangled ("Invalid or corrupt jarfile").  SFTP is
always binary; this also md5-verifies the remote copy after upload.

Usage:
    python put_t24.py --env 30 local/myfile.jar "$T24_BNK_RUN/SOME.BP/myfile.jar"
    # optional: --mode 664
NB: pass remote paths with MSYS_NO_PATHCONV=1 under Git-Bash.
"""
import argparse
import hashlib
import os
import sys

from fetch_t24_sources import load_environments, select_env, connect


def md5_local(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Binary SFTP upload to a T24 env, with md5 verify.")
    ap.add_argument("--env", required=True)
    ap.add_argument("--servers", default=None, help=argparse.SUPPRESS)  # deprecated: shared store
    ap.add_argument("--mode", default="664", help="chmod (octal) applied after upload, default 664")
    ap.add_argument("local")
    ap.add_argument("remote")
    args = ap.parse_args()

    matches = select_env(load_environments(args.servers), args.env)
    if len(matches) != 1:
        sys.exit("env selector matched: " + ", ".join(e["label"] for e in matches))
    env = matches[0]
    if not env.get("pass"):
        sys.exit(f"no password for '{env['label']}' — run: python t24_env.py passwd \"{env['label']}\"")
    if not os.path.isfile(args.local):
        sys.exit("local file not found: %s" % args.local)

    want = md5_local(args.local)
    size = os.path.getsize(args.local)
    print("# local  %s  %d bytes  md5 %s" % (args.local, size, want))

    client = connect(env)
    try:
        sftp = client.open_sftp()
        sftp.put(args.local, args.remote)
        sftp.chmod(args.remote, int(args.mode, 8))
        st = sftp.stat(args.remote)
        sftp.close()
        # md5 the remote copy via shell (binary-safe; no content comes back)
        _, out, _ = client.exec_command("md5sum '%s'" % args.remote, timeout=30)
        got = out.read().decode(errors="replace").split()[0] if out else ""
        print("# remote %s  %d bytes  md5 %s" % (args.remote, st.st_size, got))
        if st.st_size != size or got != want:
            sys.exit("VERIFY FAILED: size/md5 mismatch after upload")
        print("OK: uploaded and verified")
    finally:
        client.close()


if __name__ == "__main__":
    main()
