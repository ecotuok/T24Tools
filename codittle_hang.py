#!/usr/bin/env python3
"""
codittle_hang.py -- diagnose a frozen Codittle desktop: WHICH layer is wedged, and why.

When Codittle stops responding, the culprit is one of three layers, and each needs a
different fix (see CODITTLE-HARDENING.md items 12 / 14 / 16 / 17):
  * the Tauri HOST   -- codittle-desktop.exe's single UI thread (owns every window)
  * a WebView2 RENDERER -- the page's JS thread (host fine, one window frozen)
  * a BACKEND        -- API sidecar / Theia node (host fine, UI waiting on data)
This answers it in one run -- stdlib + ctypes only (no wmic, no PowerShell, no psutil):

  python codittle_hang.py                  # full report on the running Codittle
  python codittle_hang.py --all-threads    # + a stack for every host thread, not just the UI one
  python codittle_hang.py --dump DIR       # + full-memory minidump of the host (for a bug report)
  python codittle_hang.py --pid N          # a specific codittle-desktop.exe instance

Report: hung windows (IsHungAppWindow) -> UI-thread wait state and since when -> its
user-mode stack, classified (re-entrant lock / blocked wait / idle / spinning) -> process
tree with a live CPU sample + memory + WebView2 role -> backend health probes -> verdict.

SAFE: read-only, apart from suspending each sampled thread for the milliseconds a stack
walk takes (what any profiler does). Never kills anything -- recovery steps are printed.
Stack frames inside codittle-desktop.exe show as module+offset (no PDB is shipped), so
offsets are only comparable across runs of the SAME build (exe timestamp is printed).
"""
import argparse
import ctypes
import ctypes.wintypes as w
import json
import msvcrt
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

k32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll
user32 = ctypes.windll.user32
psapi = ctypes.windll.psapi
dbghelp = ctypes.windll.dbghelp
k32.OpenProcess.restype = w.HANDLE
k32.OpenThread.restype = w.HANDLE
k32.GetTickCount64.restype = ctypes.c_uint64

HOST_EXE = "codittle-desktop.exe"
LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
RUN_DIR = LOCAL / "Codittle" / "run"
# Codittle moved workspaces from Codittle\ to CodittleData\ — check both, newest layout first.
WORKSPACE_ROOTS = [LOCAL / "CodittleData" / "workspaces", LOCAL / "Codittle" / "workspaces"]

PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED, PROCESS_VM_READ = 0x0400, 0x1000, 0x0010
THREAD_SUSPEND_RESUME, THREAD_GET_CONTEXT, THREAD_QUERY_INFORMATION = 0x0002, 0x0008, 0x0040
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
HOT_CPU_PCT, BIG_WS_BYTES = 50.0, 1 << 30

THREAD_STATES = {0: "Init", 1: "Ready", 2: "Running", 3: "Standby", 4: "Terminated",
                 5: "Waiting", 6: "Transition", 7: "DeferredReady"}
# KWAIT_REASON. 37 is how modern Windows parks SRW locks / Rust Mutex / WaitOnAddress;
# 13 is an idle GetMessage loop — the normal resting state of a healthy UI thread.
WAIT_REASONS = {0: "Executive", 4: "DelayExecution", 5: "Suspended", 6: "UserRequest",
                7: "WrExecutive", 13: "WrUserRequest", 15: "WrQueue", 16: "WrLpcReceive",
                17: "WrLpcReply", 27: "WrResource", 28: "WrPushLock", 29: "WrMutex",
                34: "WrFastMutex", 35: "WrGuardedMutex", 37: "WrAlertByThreadId"}
WR_USER_REQUEST = 13


# ── system snapshot (one NtQuerySystemInformation call = processes + threads + times) ──
def snapshot():
    size = 1 << 22
    while True:
        buf = ctypes.create_string_buffer(size)
        status = ntdll.NtQuerySystemInformation(5, buf, size, None) & 0xFFFFFFFF
        if status != STATUS_INFO_LENGTH_MISMATCH:
            break
        size *= 2
    if status:
        sys.exit(f"NtQuerySystemInformation failed: {status:#x}")
    raw, base, procs, off = buf.raw, ctypes.addressof(buf), {}, 0
    while True:
        nxt, nthreads = struct.unpack_from("<II", raw, off)
        user, kern = struct.unpack_from("<qq", raw, off + 0x28)
        name_len, name_ptr = struct.unpack_from("<H6xQ", raw, off + 0x38)
        pid, ppid = struct.unpack_from("<QQ", raw, off + 0x50)
        ws, = struct.unpack_from("<Q", raw, off + 0x90)
        name = raw[name_ptr - base: name_ptr - base + name_len].decode("utf-16-le") if name_ptr else "Idle"
        threads = []
        for i in range(nthreads):
            t = off + 0x100 + i * 0x50
            tk, tu, created = struct.unpack_from("<qqq", raw, t)
            wait_tick, = struct.unpack_from("<I", raw, t + 0x18)
            tid, = struct.unpack_from("<Q", raw, t + 0x30)
            _, state, reason = struct.unpack_from("<III", raw, t + 0x40)
            threads.append(dict(tid=tid, cpu=(tk + tu) / 1e7, created=created,
                                wait_tick=wait_tick, state=state, reason=reason))
        procs[pid] = dict(pid=pid, ppid=ppid, name=name, cpu=(user + kern) / 1e7, ws=ws, threads=threads)
        if not nxt:
            return procs
        off += nxt


def descendants(procs, root):
    kids = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    out = []

    def walk(pid, depth):
        out.append((pid, depth))
        for c in sorted(kids.get(pid, [])):
            if c != pid:
                walk(c, depth + 1)
    walk(root, 0)
    return out


def tick_ms():
    adj, inc, disabled = w.DWORD(), w.DWORD(), w.BOOL()
    k32.GetSystemTimeAdjustment(ctypes.byref(adj), ctypes.byref(inc), ctypes.byref(disabled))
    return inc.value / 10000.0 or 15.625


def waited_seconds(wait_tick):
    per = tick_ms()
    now_tick = int(k32.GetTickCount64() / per) & 0xFFFFFFFF
    return ((now_tick - wait_tick) & 0xFFFFFFFF) * per / 1000.0


# ── per-process details ─────────────────────────────────────────────────────
class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", ctypes.c_void_p)]


def cmdline(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ""
    buf = ctypes.create_string_buffer(65536)
    status = ntdll.NtQueryInformationProcess(w.HANDLE(h), 60, buf, len(buf), None)
    k32.CloseHandle(w.HANDLE(h))
    if status:
        return ""
    us = _UnicodeString.from_buffer(buf)
    return ctypes.wstring_at(us.Buffer, us.Length // 2)


def image_path(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ""
    buf, n = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
    k32.QueryFullProcessImageNameW(w.HANDLE(h), 0, buf, ctypes.byref(n))
    k32.CloseHandle(w.HANDLE(h))
    return buf.value


def read_pid(name):
    try:
        return int((RUN_DIR / name).read_text().strip())
    except (OSError, ValueError):
        return None


def role(p, cmd, sidecar_pid, theia_pid):
    if p["name"].lower() == HOST_EXE:
        return "HOST (Tauri shell)"
    if p["name"].lower() == "msedgewebview2.exe":
        kind = re.search(r"--type=(\S+)", cmd)
        sub = re.search(r"--utility-sub-type=\S*?(\w+)\.mojom", cmd)
        if not kind:
            return "webview: browser"
        return f"webview: {kind.group(1)}" + (f" ({sub.group(1)})" if sub else "")
    if p["pid"] == sidecar_pid:
        return "API sidecar (PGlite DB)"
    if p["pid"] == theia_pid:
        return "Theia backend"
    if p["name"].lower() == "node.exe":
        script = re.search(r"([\w.-]+\.(?:c?m?js))", cmd)
        return "extension host" if "exthost" in cmd else f"node {script.group(1) if script else ''}".strip()
    return ""


# ── windows ─────────────────────────────────────────────────────────────────
def host_windows(pid):
    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            n = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, title, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value == "Tauri Window":
                found.append((title.value or "(untitled)", bool(user32.IsWindowVisible(hwnd)),
                              bool(user32.IsHungAppWindow(hwnd))))
        return True
    user32.EnumWindows(visit, 0)
    return found


# ── stack walk (dbghelp, exports-only symbols) ──────────────────────────────
class _Addr64(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint64), ("Segment", ctypes.c_uint16), ("Mode", ctypes.c_uint32)]


class _StackFrame64(ctypes.Structure):
    _fields_ = [("AddrPC", _Addr64), ("AddrReturn", _Addr64), ("AddrFrame", _Addr64),
                ("AddrStack", _Addr64), ("AddrBStore", _Addr64), ("FuncTableEntry", ctypes.c_void_p),
                ("Params", ctypes.c_uint64 * 4), ("Far", w.BOOL), ("Virtual", w.BOOL),
                ("Reserved", ctypes.c_uint64 * 3), ("KdHelp", ctypes.c_byte * 512)]


class _ModuleInfo(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", w.DWORD), ("EntryPoint", ctypes.c_void_p)]


class StackWalker:
    CONTEXT_SIZE, CONTEXT_FULL_AMD64 = 1232, 0x10000B
    SYMBOL_INFO_SIZE, MAX_NAME = 88, 1000

    def __init__(self, pid):
        self.hp = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not self.hp:
            sys.exit(f"cannot open PID {pid} for reading (run as the same user as Codittle)")
        dbghelp.SymSetOptions(0x2 | 0x4)  # UNDNAME | DEFERRED_LOADS
        dbghelp.SymInitializeW(w.HANDLE(self.hp), None, True)
        dbghelp.StackWalk64.argtypes = [w.DWORD, w.HANDLE, w.HANDLE, ctypes.POINTER(_StackFrame64),
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_void_p]
        self.modules = self._modules()
        self.symbuf = ctypes.create_string_buffer(self.SYMBOL_INFO_SIZE + self.MAX_NAME + 8)

    def _modules(self):
        handles, need = (w.HMODULE * 2048)(), w.DWORD()
        psapi.EnumProcessModulesEx(w.HANDLE(self.hp), handles, ctypes.sizeof(handles), ctypes.byref(need), 3)
        mods = []
        for m in handles[: need.value // ctypes.sizeof(w.HMODULE)]:
            name, info = ctypes.create_unicode_buffer(260), _ModuleInfo()
            psapi.GetModuleBaseNameW(w.HANDLE(self.hp), w.HMODULE(m), name, 260)
            psapi.GetModuleInformation(w.HANDLE(self.hp), w.HMODULE(m), ctypes.byref(info), ctypes.sizeof(info))
            mods.append((info.lpBaseOfDll or 0, info.SizeOfImage, name.value))
        return mods

    def symbol(self, addr):
        module, offset = next(((n, addr - b) for b, s, n in self.modules if b <= addr < b + s), ("?", addr))
        struct.pack_into("<I", self.symbuf, 0, self.SYMBOL_INFO_SIZE)
        struct.pack_into("<I", self.symbuf, 80, self.MAX_NAME)
        disp = ctypes.c_uint64()
        # exports-only: a huge displacement means "nearest export", which would mislead
        if dbghelp.SymFromAddr(w.HANDLE(self.hp), ctypes.c_uint64(addr), ctypes.byref(disp), self.symbuf) \
                and disp.value < 0x10000:
            name = ctypes.string_at(ctypes.addressof(self.symbuf) + 84).decode(errors="ignore")
            return f"{module}!{name}+{disp.value:#x}"
        return f"{module}+{offset:#x}"

    def walk(self, tid, depth=40):
        ht = k32.OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, False, tid)
        if not ht:
            return ["<cannot open thread>"]
        raw = ctypes.create_string_buffer(self.CONTEXT_SIZE + 16)
        ctx = (ctypes.addressof(raw) + 15) & ~15
        struct.pack_into("<I", (ctypes.c_char * self.CONTEXT_SIZE).from_address(ctx), 0x30, self.CONTEXT_FULL_AMD64)
        frames = []
        k32.SuspendThread(w.HANDLE(ht))
        try:
            if not k32.GetThreadContext(w.HANDLE(ht), ctypes.c_void_p(ctx)):
                return ["<GetThreadContext failed>"]
            sf = _StackFrame64()
            for field, reg in (("AddrPC", 0xF8), ("AddrStack", 0x98), ("AddrFrame", 0xA0)):
                getattr(sf, field).Offset = ctypes.c_uint64.from_address(ctx + reg).value
                getattr(sf, field).Mode = 3  # AddrModeFlat
            fta = ctypes.cast(dbghelp.SymFunctionTableAccess64, ctypes.c_void_p)
            gmb = ctypes.cast(dbghelp.SymGetModuleBase64, ctypes.c_void_p)
            for _ in range(depth):
                if not dbghelp.StackWalk64(0x8664, w.HANDLE(self.hp), w.HANDLE(ht), ctypes.byref(sf),
                                           ctypes.c_void_p(ctx), None, fta, gmb, None) or not sf.AddrPC.Offset:
                    break
                frames.append(sf.AddrPC.Offset)
        finally:
            k32.ResumeThread(w.HANDLE(ht))
            k32.CloseHandle(w.HANDLE(ht))
        return [self.symbol(a) for a in frames]


def classify(frames):
    """Name the UI-thread pattern from its stack (build-independent — uses system frames only)."""
    text = " ".join(frames[:6])
    blocked_on_lock = "WaitOnAddress" in text or "WaitForAlertByThreadId" in text
    reentered = any("KiUserCallbackDispatcher" in f for f in frames) and \
        any("PeekMessage" in f or "GetMessage" in f or "SendMessage" in f for f in frames)
    if blocked_on_lock and reentered:
        return ("RE-ENTRANT LOCK DEADLOCK",
                "a window handler holding a lock pumped messages (PeekMessage/SendMessage); Windows "
                "dispatched a sent message back into this thread and its handler now waits on that "
                "same lock. Permanent -- the thread is waiting on itself (CODITTLE-HARDENING item 17).")
    if frames and ("NtUserGetMessage" in frames[0] or "MsgWaitForMultipleObjects" in frames[0]):
        return ("IDLE MESSAGE LOOP", "the UI thread is healthy and waiting for input.")
    if blocked_on_lock or "WaitForSingleObject" in text or "WaitForMultipleObjects" in text:
        return ("BLOCKED WAIT",
                "the UI thread is waiting on another thread/object -- e.g. a synchronous Tauri command "
                "waiting on a window op only this thread can service (CODITTLE-HARDENING item 16).")
    return ("UNCLASSIFIED", "compare the frames against CODITTLE-HARDENING.md.")


# ── backends ────────────────────────────────────────────────────────────────
def http_probe(url, timeout=5):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = " ".join(r.read(200).decode(errors="ignore").split())
            return r.status, (time.perf_counter() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, ""
    except Exception as e:  # timeout / refused — the answer we are looking for
        return None, (time.perf_counter() - t0) * 1000, type(e).__name__


def sidecar_port(pid):
    for root in WORKSPACE_ROOTS:
        for f in root.glob("*/sidecar.json"):
            try:
                meta = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("pid") == pid:
                return meta.get("port"), f.parent
    return None, None


def listening_ports(pid):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(m.group(1)) for m in re.finditer(r"127\.0\.0\.1:(\d+)\s+\S+\s+LISTENING\s+(\d+)", out)
            if int(m.group(2)) == pid]


# ── minidump ────────────────────────────────────────────────────────────────
def write_dump(pid, out_dir):
    out = Path(out_dir) / f"codittle-desktop-{pid}-{datetime.now():%Y%m%d-%H%M%S}.dmp"
    out.parent.mkdir(parents=True, exist_ok=True)
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    full_memory_with_thread_info = 0x2 | 0x1000 | 0x4  # FullMemory | ThreadInfo | HandleData
    with open(out, "wb") as f:
        ok = dbghelp.MiniDumpWriteDump(w.HANDLE(h), pid, w.HANDLE(msvcrt.get_osfhandle(f.fileno())),
                                       full_memory_with_thread_info, None, None, None)
    k32.CloseHandle(w.HANDLE(h))
    return out if ok else None


# ── report ──────────────────────────────────────────────────────────────────
def fmt_age(seconds):
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def report(host_pid, args):
    before = snapshot()
    if host_pid not in before:
        sys.exit(f"PID {host_pid} not running")
    time.sleep(args.sample)
    after = snapshot()
    tree = [(pid, d) for pid, d in descendants(after, host_pid) if pid in before]
    sidecar_pid, theia_pid = read_pid("api-sidecar.pid"), read_pid("theia.pid")
    cmds = {pid: cmdline(pid) for pid, _ in tree}
    version = next((m.group(1) for c in cmds.values() for m in [re.search(r"--webview-exe-version=(\S+)", c)] if m), "?")
    exe = image_path(host_pid)
    built = datetime.fromtimestamp(os.path.getmtime(exe)).strftime("%Y-%m-%d %H:%M") if exe else "?"

    print(f"== host  {HOST_EXE} PID {host_pid}  v{version}  (exe built {built})")
    wins = host_windows(host_pid)
    hung = [t for t, _, h in wins if h]
    for title, visible, is_hung in wins:
        print(f"   window  {'HUNG' if is_hung else 'ok  '}  {title}{'' if visible else '  [hidden]'}")

    threads0 = {t["tid"]: t for t in before[host_pid]["threads"]}
    threads1 = sorted(after[host_pid]["threads"], key=lambda t: t["created"])
    main = threads1[0]
    main_cpu = main["cpu"] - threads0.get(main["tid"], main)["cpu"]
    state = THREAD_STATES.get(main["state"], main["state"])
    reason = WAIT_REASONS.get(main["reason"], main["reason"])
    idle = main["state"] == 5 and main["reason"] == WR_USER_REQUEST
    age = waited_seconds(main["wait_tick"]) if main["state"] == 5 and not idle else 0
    since = (datetime.now() - timedelta(seconds=age)).strftime("%H:%M:%S")
    waiting = f" ({reason}{', idle' if idle else ''})" if main["state"] == 5 else ""
    print(f"   UI thread {main['tid']}: {state}{waiting}" + (f" since {since} -- {fmt_age(age)}" if age else "")
          + f";  CPU +{main_cpu:.2f}s over {args.sample:g}s")

    walker = StackWalker(host_pid)
    frames = walker.walk(main["tid"])
    print(f"\n== UI-thread stack")
    for i, f in enumerate(frames):
        print(f"   {i:2} {f}")
    verdict, why = classify(frames)
    print(f"   => {verdict}: {why}")

    if args.all_threads:
        for t in threads1[1:]:
            print(f"\n== thread {t['tid']}  {THREAD_STATES.get(t['state'], t['state'])}"
                  f" ({WAIT_REASONS.get(t['reason'], t['reason'])})")
            for i, f in enumerate(walker.walk(t["tid"], depth=12)):
                print(f"   {i:2} {f}")

    print(f"\n== process tree  (CPU sampled over {args.sample:g}s)")
    print(f"   {'PID':>6}  {'CPU%':>6}  {'CPU total':>9}  {'WS':>8}  role")
    hot = []
    for pid, depth in tree:
        p0, p1 = before[pid], after[pid]
        if p1["name"].lower() == "conhost.exe":
            continue
        pct = (p1["cpu"] - p0["cpu"]) / args.sample * 100
        r = role(p1, cmds[pid], sidecar_pid, theia_pid) or p1["name"]
        if r.startswith("webview: renderer") and (pct >= HOT_CPU_PCT or p1["ws"] >= BIG_WS_BYTES):
            hot.append((pid, pct, p1["ws"]))
        print(f"   {pid:>6}  {pct:6.1f}  {fmt_age(p1['cpu']):>9}  {p1['ws'] / 2**20:6.0f}MB  {'  ' * depth}{r}")

    print(f"\n== backends")
    backends_ok = True
    port, ws_dir = sidecar_port(sidecar_pid) if sidecar_pid else (None, None)
    probes = [("API sidecar", sidecar_pid, f"http://127.0.0.1:{port}/health" if port else None)]
    theia_ports = listening_ports(theia_pid) if theia_pid else []
    probes.append(("Theia backend", theia_pid, f"http://127.0.0.1:{theia_ports[0]}/" if theia_ports else None))
    for name, pid, url in probes:
        if not url:
            print(f"   {name:14} PID {pid}: no port found")
            backends_ok = False
            continue
        code, ms, body = http_probe(url)
        backends_ok &= code == 200
        print(f"   {name:14} PID {pid}: {url} -> {code or 'NO ANSWER'} in {ms:.0f} ms  {body[:60]}")
    if ws_dir:
        print(f"   workspace: {ws_dir}")

    if args.dump:
        out = write_dump(host_pid, args.dump)
        print(f"\n== minidump: {out or 'FAILED (MiniDumpWriteDump)'}")

    print(f"\n== verdict")
    if hung and verdict in ("RE-ENTRANT LOCK DEADLOCK", "BLOCKED WAIT") and main_cpu < 0.05 * args.sample:
        print(f"   HOST DEADLOCKED ({verdict.lower()}) -- {len(hung)}/{len(wins)} windows hung, UI thread idle-blocked "
              f"{fmt_age(age)}. It will not recover.")
        print("   Restart (unsaved edits in the hung windows are lost):")
        print(f"     1. taskkill /PID {host_pid} /F")
        print(f"     2. make sure the sidecar (PID {sidecar_pid}) and Theia (PID {theia_pid}) are gone before relaunch --")
        print("        a second sidecar opening the same pgdata is how the DB corrupts (taskkill /PID <n> /F)")
        print("     3. relaunch; if connections are missing, see codittle_db.py")
    elif hung and main_cpu >= 0.05 * args.sample:
        print("   HOST BUSY -- UI thread is burning CPU, not blocked. May recover; re-run in a minute.")
    elif hung:
        print(f"   HOST HUNG, pattern {verdict} -- capture --all-threads and --dump before restarting.")
    else:
        print("   Host responsive, no hung windows.")
    if hot:
        print("   Heavy renderer(s): " + ", ".join(f"PID {pid} {pct:.0f}% CPU {ws / 2**30:.1f} GB" for pid, pct, ws in hot)
              + " -- if a window freezes while the host is fine, that's the renderer family (item 14).")
    if not backends_ok:
        print("   Backend not answering -- backend-death family (item 12).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int, help=f"{HOST_EXE} PID (default: every running instance)")
    ap.add_argument("--sample", type=float, default=3, help="CPU sample window in seconds (default 3)")
    ap.add_argument("--all-threads", action="store_true", help="walk every host thread, not just the UI one")
    ap.add_argument("--dump", metavar="DIR", help="also write a full-memory minidump of the host into DIR (~150 MB)")
    args = ap.parse_args()
    if sys.platform != "win32":
        sys.exit("Windows only")
    hosts = [args.pid] if args.pid else [p["pid"] for p in snapshot().values() if p["name"].lower() == HOST_EXE]
    if not hosts:
        sys.exit(f"{HOST_EXE} is not running")
    for pid in hosts:
        report(pid, args)


if __name__ == "__main__":
    main()
