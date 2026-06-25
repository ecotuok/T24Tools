#!/usr/bin/env python3
"""
codittle_connections.py — List all Codittle SSH stream connections with live status.

Reads from Codittle's embedded PGlite database (via codittle_connections.mjs)
and checks live SSH reachability for every configured server.

Usage:
    python codittle_connections.py              # all connections
    python codittle_connections.py --live       # only SSH-reachable right now
    python codittle_connections.py --json       # raw JSON (pipe-friendly)
    python codittle_connections.py --no-check   # skip the SSH liveness probe
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# ── Locate Codittle's node.exe ────────────────────────────────────────────

def _find_codittle_node() -> Path | None:
    """Return path to Codittle's bundled node.exe, or None if not found."""

    # 1. Check the running codittle-desktop.exe process for its location
    try:
        out = subprocess.check_output(
            ['wmic', 'process', 'where', "name='codittle-desktop.exe'",
             'get', 'ExecutablePath'],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode(errors='replace')
        for line in out.splitlines():
            line = line.strip()
            if line and 'codittle-desktop.exe' in line.lower():
                node = Path(line).parent / 'node.exe'
                if node.exists():
                    return node
    except Exception:
        pass

    # 2. Search Downloads for Codittle_*/node.exe
    downloads = Path.home() / 'Downloads'
    for entry in sorted(downloads.glob('Codittle_*/node.exe'), reverse=True):
        if entry.exists():
            return entry

    return None


# ── SSH liveness probe ────────────────────────────────────────────────────

def _ssh_live(host: str, port: int = 22, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── Run the .mjs helper and parse its JSON ────────────────────────────────

def _query_database(node_exe: Path) -> dict:
    mjs = Path(__file__).parent / 'codittle_connections.mjs'
    if not mjs.exists():
        sys.exit(f'ERROR: {mjs} not found — it must live alongside this script.')

    try:
        result = subprocess.run(
            [str(node_exe), str(mjs)],
            capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError:
        sys.exit(f'ERROR: node.exe not found at {node_exe}')
    except subprocess.TimeoutExpired:
        sys.exit('ERROR: Codittle database query timed out.')

    if result.returncode != 0:
        sys.exit(f'ERROR: mjs query failed:\n{result.stderr.strip()}')

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f'ERROR: could not parse JSON output: {e}\n{result.stdout[:500]}')


# ── Formatting helpers ────────────────────────────────────────────────────

def _fmt_age(iso_ts: str | None) -> str:
    if not iso_ts:
        return 'never'
    try:
        dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
        delta = datetime.now(timezone.utc) - dt
        d = delta.days
        if d == 0:
            h = delta.seconds // 3600
            return f'{h}h ago' if h else 'just now'
        if d == 1:
            return 'yesterday'
        if d < 7:
            return f'{d}d ago'
        if d < 30:
            return f'{d // 7}w ago'
        return dt.strftime('%Y-%m-%d')
    except ValueError:
        return iso_ts[:10]


def _fmt_table(rows: list[dict], check: bool) -> str:
    cols = ['Name', 'Connection', 'SSH Home', 'Last Used']
    if check:
        cols.insert(3, 'SSH')

    data = []
    for r in rows:
        conn = f"{r['username']}@{r['host']}:{r['port']}"
        home = r['ssh_home'] or '(not set)'
        row = [r['name'], conn, home, _fmt_age(r['last_viewed_at'])]
        if check:
            row.insert(3, 'UP' if r.get('_live') else '--')
        data.append(row)

    widths = [max(len(cols[i]), *(len(d[i]) for d in data)) for i in range(len(cols))]
    sep = '  '.join('-' * w for w in widths)
    header = '  '.join(c.ljust(w) for c, w in zip(cols, widths))

    lines = [header, sep]
    for row in data:
        line = '  '.join(v.ljust(w) for v, w in zip(row, widths))
        lines.append(line)
    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='List Codittle SSH connections with live SSH status.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python codittle_connections.py              # full table
              python codittle_connections.py --live       # reachable servers only
              python codittle_connections.py --no-check   # skip SSH probe (faster)
              python codittle_connections.py --json       # JSON output
        """)
    )
    ap.add_argument('--live',     action='store_true', help='Show only SSH-reachable servers')
    ap.add_argument('--json',     action='store_true', help='Output raw JSON')
    ap.add_argument('--no-check', action='store_true', help='Skip SSH liveness probe')
    args = ap.parse_args()

    node_exe = _find_codittle_node()
    if not node_exe:
        sys.exit(
            'ERROR: Codittle node.exe not found.\n'
            'Make sure Codittle is installed (or running) and try again.'
        )

    data = _query_database(node_exe)
    connections = data['connections']
    projects    = data['projects']

    do_check = not args.no_check and not args.json

    if do_check:
        print(f'Probing SSH on {len(connections)} host(s)...', end='', flush=True)
        for c in connections:
            c['_live'] = _ssh_live(c['host'], c['port'])
        print(' done.\n')
    else:
        for c in connections:
            c['_live'] = None

    if args.json:
        print(json.dumps({'connections': connections, 'projects': projects}, indent=2))
        return

    if args.live:
        connections = [c for c in connections if c.get('_live')]
        if not connections:
            print('No servers are reachable via SSH right now.')
            return

    # ── Summary line ──────────────────────────────────────────────────────
    stream_info = ''
    if connections:
        c0 = connections[0]
        stream_info = f"  Bank: {c0['bank_name']}   Stream: {c0['stream_version']}/{c0['stream_runtime']}"

    live_count = sum(1 for c in connections if c.get('_live'))
    total      = len(connections)

    print(f'Codittle stream connections{stream_info}')
    if do_check:
        print(f'SSH reachable: {live_count}/{total}')
    print()

    # ── Connections table ─────────────────────────────────────────────────
    print(_fmt_table(connections, check=do_check))

    # ── Projects list ─────────────────────────────────────────────────────
    print(f'\nProjects in this stream ({len(projects)}):')
    for p in projects:
        print(f'  {p["name"]}')


if __name__ == '__main__':
    main()
