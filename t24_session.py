#!/usr/bin/env python3
"""Persistent T24/jBASE session over SSH — connect ONCE, run MANY commands.

Every other tool here opens a fresh SSH connection and re-loads the jBASE
environment (the slow part each time). This holds a single
non-interactive `bash -s` channel open with jBASE already loaded, and exposes it
on a local socket so each command is just a round-trip.

  # 1) start the daemon (background) — connects, loads jBASE once
  #    a) by label (resolved from the shared env store):
  python t24_session.py serve --env ENV-01 --port 8765
  #    b) by Amethyst DB env id (password unsealed from the DPAPI store):
  python t24_session.py serve --env-id 19 --port 8765

  # 2) fire commands at it (cheap — reuses the live jBASE session)
  python t24_session.py send --port 8765 'LIST F.SOME.FILE WITH @ID LIKE "ABC..." ALL'
  python t24_session.py send --port 8765 --raw 'SELECT F.SOME.FILE'

  # 3) stop it
  python t24_session.py stop --port 8765

Non-PTY bash => no pager, no ANSI. Attribute marks FM/VM/SM (254/253/252) are
converted to \n / | / ^ unless --raw. Read-only intent; same creds source (shared store)
and jBASE env-load trick as the rest of the toolkit.
"""
import argparse
import os
import re
import socket
import sys
import time

import fetch_t24_sources as ft

# default location of the amethyst app (sibling of t24-tools under DevTools)
DEFAULT_AMETHYST = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "amethyst")
)


def env_from_db(amethyst_dir, env_id):
    """Resolve a single environment (by id) from the amethyst SQLite DB and
    return it in the CSV-style dict shape that ft.connect()/open_shell() expect.
    The SSH password is unsealed from the DPAPI store at call time."""
    amethyst_dir = os.path.abspath(amethyst_dir)
    if amethyst_dir not in sys.path:
        sys.path.insert(0, amethyst_dir)
    from app import db as adb, secrets as asec  # noqa: E402

    adb.init()
    match = [e for e in adb.environments(enabled_only=False) if e["id"] == env_id]
    if not match:
        sys.exit(f"no amethyst environment with id={env_id} in {amethyst_dir}")
    e = match[0]
    return {
        "label": e.get("label") or f"env{env_id}",
        "host": e["host"],
        "port": e.get("port") or 22,
        "user": e["username"],
        "pass": asec.unseal(e["password_enc"]),
        "bnk": e.get("bnk_run"),
    }

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]|\x1b.")

PROFILE = '"$HOME/.profile"'
EOC = "__T24_EOC__"            # end-of-command sentinel echoed after each command
READY = "__T24_READY__"        # printed once jBASE env is loaded
DEFAULT_BNK = os.environ.get("T24_BNK_RUN", "/t24/bnk/bnk.run")
CONV = r" | tr '\376\375\374' '\n|^'"


def _drain_until(chan, marker, timeout=120):
    """Read from the channel until a line containing `marker` is seen."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if chan.recv_ready():
            buf += chan.recv(65536)
            if marker.encode() in buf:
                break
        else:
            time.sleep(0.02)
    text = buf.decode("utf-8", "replace")
    # keep everything before the marker line
    idx = text.find(marker)
    return text[:idx] if idx != -1 else text


def open_shell(env, bnk):
    client = ft.connect(env)
    chan = client.get_transport().open_session()
    chan.exec_command("bash -s")
    boot = (
        f'cd "{bnk}" || {{ echo "ERROR: cannot cd {bnk}"; exit 3; }}\n'
        f". <(sed '/jpqn.*loginproc/,$d' {PROFILE}) 2>/dev/null\n"
        f'echo {READY}\n'
    )
    chan.sendall(boot.encode())
    banner = _drain_until(chan, READY, timeout=90)
    return client, chan, banner


def run_command(chan, cmd, raw=False):
    conv = "" if raw else CONV
    # wrap so the conversion applies only to the command's own output
    line = f"{{ {cmd} ; }}{conv}\necho {EOC}\n"
    chan.sendall(line.encode())
    out = _drain_until(chan, EOC, timeout=600)
    return ANSI.sub("", out).rstrip("\n")


def list_envs(args):
    """Print id / label / host:port / user for every environment in the amethyst
    DB, so the right --env-id is discoverable without opening the DB by hand."""
    amethyst_dir = os.path.abspath(args.amethyst)
    if amethyst_dir not in sys.path:
        sys.path.insert(0, amethyst_dir)
    from app import db as adb  # noqa: E402

    adb.init()
    rows = sorted(adb.environments(enabled_only=False), key=lambda e: e["id"])
    print(f"{'ID':>4}  {'LABEL':<16} {'HOST':<18} {'PORT':>5}  USER")
    for e in rows:
        print(f"{e['id']:>4}  {str(e.get('label','')):<16} {str(e.get('host','')):<18} "
              f"{str(e.get('port') or 22):>5}  {e.get('username','')}")


def serve(args):
    if args.env_id is not None:
        env = env_from_db(args.amethyst, args.env_id)
    else:
        if not args.env:
            sys.exit("provide either --env-id <n> (Amethyst DB) or --env <label> (shared store)")
        envs = ft.load_environments(args.servers)
        m = ft.select_env(envs, args.env)
        if len(m) != 1:
            sys.exit("env selector matched: " + ", ".join(e["label"] for e in m) or "nothing")
        env = m[0]
    bnk = args.bnk or env.get("bnk")
    if not bnk:                       # auto-detect the per-host bnk.run (varies by jBASE account)
        try:
            _c = ft.connect(env); bnk = ft.detect_bnk_run(_c); _c.close()
        except Exception:
            pass
    bnk = bnk or DEFAULT_BNK
    print(f"# connecting {env['user']}@{env['host']} ({env['label']}) bnk={bnk} ...", flush=True)
    client, chan, banner = open_shell(env, bnk)
    print(f"# jBASE loaded. listening on 127.0.0.1:{args.port}", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(4)
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                req = b""
                while not req.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    req += chunk
                line = req.decode("utf-8", "replace").rstrip("\n")
                if line == "::STOP::":
                    conn.sendall(b"# stopping\n")
                    break
                raw = False
                if line.startswith("::RAW::"):
                    raw, line = True, line[len("::RAW::"):]
                if not line.strip():
                    conn.sendall(b"")
                    continue
                t0 = time.monotonic()
                out = run_command(chan, line, raw=raw)
                dt = time.monotonic() - t0
                conn.sendall(out.encode("utf-8", "replace"))
                print(f"# ran ({dt:.1f}s): {line[:70]}", flush=True)
    finally:
        try:
            chan.sendall(b"exit\n")
        except Exception:
            pass
        client.close()
        srv.close()
        print("# session closed", flush=True)


def send(args):
    payload = args.cmd
    if args.raw:
        payload = "::RAW::" + payload
    with socket.create_connection(("127.0.0.1", args.port), timeout=640) as s:
        s.sendall((payload + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    out = b"".join(chunks).decode("utf-8", "replace")
    sys.stdout.write(out if out.endswith("\n") else out + "\n")


def stop(args):
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=10) as s:
            s.sendall(b"::STOP::\n")
            print(s.recv(1024).decode("utf-8", "replace").rstrip())
    except OSError as e:
        print(f"# no daemon on {args.port}? ({e})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    sp = sub.add_parser("serve", help="connect once and listen for commands")
    sp.add_argument("--env", help="env label selector (from the shared store)")
    sp.add_argument("--env-id", type=int, default=None,
                    help="amethyst DB environment id (resolves host/user/sealed-password)")
    sp.add_argument("--amethyst", default=DEFAULT_AMETHYST,
                    help="path to the amethyst app folder (default: sibling ../amethyst)")
    sp.add_argument("--servers", default=None, help=argparse.SUPPRESS)  # deprecated: shared store
    sp.add_argument("--bnk", default=None, help=f"remote bnk.run (default {DEFAULT_BNK})")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=serve)

    se = sub.add_parser("send", help="send one command to a running daemon")
    se.add_argument("cmd")
    se.add_argument("--port", type=int, default=8765)
    se.add_argument("--raw", action="store_true", help="do not convert attribute marks")
    se.set_defaults(func=send)

    st = sub.add_parser("stop", help="stop a running daemon")
    st.add_argument("--port", type=int, default=8765)
    st.set_defaults(func=stop)

    le = sub.add_parser("envs", help="list environments (id/label/host) from the amethyst DB")
    le.add_argument("--amethyst", default=DEFAULT_AMETHYST,
                    help="path to the amethyst app folder (default: sibling ../amethyst)")
    le.set_defaults(func=list_envs)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
