#!/usr/bin/env python3
"""
recover_jbase_tar.py -- recover members from a (often corrupt) jBASE/T24 tar.

When T24 records are exported with `COPY ... TO <DIR.file>` and then tarred, the
resulting tar frequently has a damaged header after the first member, so plain
`tar -xf` only yields one record. This tool ignores tar's header chain and scans
for every `ustar` magic, rebuilding each member from its 512-byte header — so it
recovers all members regardless of the corruption.

  # list members
  python recover_jbase_tar.py TEMP.RECORDS.tar --list

  # extract every member to a folder (record names sanitised: '/' -> '__')
  python recover_jbase_tar.py TEMP.RECORDS.tar --extract recovered

  # find which member(s) contain a string, with the FM attribute index
  python recover_jbase_tar.py RECORDS.tar --grep MY.RECORD

T24 records are attribute-mark delimited: FM=0xFE (254), VM=0xFD (253), SM=0xFC
(252). --grep reports the 1-based attribute (FM) index where the match sits.
"""
import argparse
import os

BS = 512
FM = b"\xfe"
VM = b"\xfd"
SM = b"\xfc"


def recover(path):
    """Return {member_name: body_bytes} by scanning ustar headers."""
    data = open(path, "rb").read()
    offs, i = [], 0
    while True:
        j = data.find(b"ustar", i)
        if j < 0:
            break
        offs.append(j)
        i = j + 1
    recs = {}
    for j in offs:
        h = j - 257                       # header starts 257 bytes before the magic
        if h < 0:
            continue
        hdr = data[h:h + BS]
        name = hdr[0:100].split(b"\x00")[0].decode("latin1", "replace")
        prefix = hdr[345:500].split(b"\x00")[0].decode("latin1", "replace")
        if prefix:
            name = prefix + "/" + name
        typeflag = hdr[156:157]
        raw = hdr[124:136].split(b"\x00")[0].strip()
        try:
            size = int(raw, 8) if raw else 0
        except ValueError:
            size = 0
        if typeflag == b"5" or name.endswith("/"):
            continue                       # directory entry
        recs[name] = data[h + BS:h + BS + size]
    return recs


def main():
    ap = argparse.ArgumentParser(description="Recover members from a corrupt jBASE/T24 tar.")
    ap.add_argument("tar", help="path to the .tar")
    ap.add_argument("--list", action="store_true", help="list members + sizes (default)")
    ap.add_argument("--extract", metavar="DIR", help="write each member to DIR")
    ap.add_argument("--grep", metavar="STR", help="show members containing STR + the FM attribute index")
    args = ap.parse_args()

    recs = recover(args.tar)
    print(f"recovered {len(recs)} member(s) from {args.tar}\n")

    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
        for name, body in recs.items():
            safe = name.replace("/", "__")
            if safe:
                open(os.path.join(args.extract, safe), "wb").write(body)
        print(f"extracted {len(recs)} member(s) to {args.extract}/")
        return

    if args.grep:
        needle = args.grep.encode("latin1", "replace")
        hits = 0
        for name in sorted(recs):
            body = recs[name]
            if needle in body:
                hits += 1
                fields = body.split(FM)
                where = [k + 1 for k, f in enumerate(fields) if needle in f]
                print(f"  {name}  (attr {where})")
        print(f"\n{hits} member(s) matched '{args.grep}'")
        return

    for name in sorted(recs):
        print(f"  {name}  ({len(recs[name])} bytes)")


if __name__ == "__main__":
    main()
