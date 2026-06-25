#!/usr/bin/env python3
"""
ctx_sync.py -- refresh a T24 project's context layer from Codittle's own metadata.

Reads each project's .codittle/versions.json + folder-mappings.json (pure JSON,
no Codittle running required) and:
  * regenerates the >>>CTX_AUTO>>> block of _ctx/project.yml
      (env ip / bnk.run / BP, classified artifacts, untracked files, last deploy)
  * rebuilds projects/INDEX.md (one row per project)

It NEVER touches Codittle's files or anything above the CTX_AUTO markers.
See projects/PLAYBOOK.md for the standard.

Usage:
    python ctx_sync.py <project-slug>      # one project
    python ctx_sync.py --all               # every project
    python ctx_sync.py --all --index-only  # just rebuild INDEX.md
    python ctx_sync.py <slug> --projects-root "<path to .../projects>"
"""
import argparse
import json
import os
import re
import sys

AUTO_START = "# >>>CTX_AUTO>>>"
AUTO_END = "# <<<CTX_AUTO<<<"


# ── locate the projects root ──────────────────────────────────────────────
def find_projects_root(override=None):
    if override:
        return os.path.abspath(override)
    env = os.environ.get("T24_PROJECTS_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    import glob
    here = os.path.dirname(os.path.abspath(__file__))          # .../DevTools/t24-tools
    desktop = os.path.abspath(os.path.join(here, "..", ".."))  # .../Desktop
    home = os.path.expanduser("~")
    # search for a Codittle <bank>/<stream>/projects dir (no hardcoded names)
    patterns = [
        os.path.join(desktop, "Codittle", "*", "*", "projects"),
        os.path.join(home, "*", "Desktop", "Codittle", "*", "*", "projects"),
        os.path.join(home, "Desktop", "Codittle", "*", "*", "projects"),
    ]
    for pat in patterns:
        hits = sorted(p for p in glob.glob(pat) if os.path.isdir(p))
        if hits:
            return hits[0]
    sys.exit("ERROR: could not find the projects root. Pass --projects-root or set T24_PROJECTS_ROOT.")


# ── read Codittle metadata ────────────────────────────────────────────────
def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def classify(name, all_names):
    """Bucket a tracked file's basename into an artifact category."""
    low = name.lower()
    if low.endswith(".jar") or low.endswith(".java"):
        return "java"
    if "," in name:
        return "versions"
    if name.startswith("I_"):
        return "includes"
    if name.endswith(".PARAM") or name.endswith(".PARAM.FIELDS"):
        return "params"
    if name.endswith(".FIELDS"):
        return "files"
    if (name + ".FIELDS") in all_names:      # a file def whose .FIELDS is also tracked
        return "files"
    return "routines"


def derive(proj_dir):
    """Return the auto-facts dict for a project from its .codittle/* + disk."""
    cod = os.path.join(proj_dir, ".codittle")
    versions = load_json(os.path.join(cod, "versions.json")) or {}
    mappings = load_json(os.path.join(cod, "folder-mappings.json")) or {}
    files = versions.get("files", {})

    # env ip = most common remoteHost among tracked files
    hosts = {}
    for meta in files.values():
        h = meta.get("remoteHost")
        if h:
            hosts[h] = hosts.get(h, 0) + 1
    ip = max(hosts, key=hosts.get) if hosts else "<unknown>"

    # bnk.run base + BP from folder-mappings, else from a remotePath
    working = mappings.get("workingBp") or mappings.get("projectBp") or ""
    bnk_run, bp_from_map = "", ""
    if working:
        bnk_run = working.rsplit("/", 1)[0]
        bp_from_map = working.rsplit("/", 1)[-1]
    if not bnk_run:
        for meta in files.values():
            rp = meta.get("remotePath", "")
            if "/bnk.run/" in rp:
                bnk_run = rp.split("/bnk.run/")[0] + "/bnk.run"
                break
    bnk_run = bnk_run or "<unknown>"

    # BP set: from mapping + every tracked key prefix + on-disk *.BP dirs
    bps = set()
    if bp_from_map:
        bps.add(bp_from_map)
    for key in files:
        if "/" in key and key.split("/", 1)[0].endswith(".BP"):
            bps.add(key.split("/", 1)[0])
    for entry in os.listdir(proj_dir):
        if entry.endswith(".BP") and os.path.isdir(os.path.join(proj_dir, entry)):
            bps.add(entry)

    # classify tracked artifacts by basename
    basenames = {key.rsplit("/", 1)[-1] for key in files}
    buckets = {k: [] for k in ("routines", "versions", "includes", "files", "params", "java")}
    for key in files:
        name = key.rsplit("/", 1)[-1]
        buckets[classify(name, basenames)].append(name)
    for b in buckets.values():
        b.sort()

    # untracked: files inside *.BP dirs on disk but not tracked, not compiled junk
    tracked_keys = set(files.keys())
    untracked = []
    skip_ext = (".o", ".obj")
    for bp in sorted(d for d in bps if os.path.isdir(os.path.join(proj_dir, d))):
        for root, _dirs, fnames in os.walk(os.path.join(proj_dir, bp)):
            for fn in fnames:
                if fn.endswith(skip_ext):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), proj_dir).replace("\\", "/")
                if rel not in tracked_keys:
                    untracked.append(rel)
    untracked.sort()

    # last deploy = newest history entry that is an actual deploy
    last = None
    for key, meta in files.items():
        for h in meta.get("history", []):
            msg = (h.get("commitMessage") or "")
            ts = h.get("ts", "")
            if msg.lower().startswith("deploy") and (last is None or ts > last["at"]):
                last = {"file": key, "version": h.get("v"), "at": ts, "env": h.get("env", "")}

    # env precedence: a human-declared override WINS (covers "we moved to box X"
    # and projects Codittle never mapped); else Codittle's last-seen host; else none.
    override = read_env_override(os.path.join(proj_dir, "_ctx", "project.yml"))
    if override and override.get("ip"):
        note = override.get("note", "")
        if ip != "<unknown>" and ip != override["ip"]:
            extra = f"codittle last-seen {ip}"
            note = f"{note}; {extra}" if note else extra
        env = {"label": override.get("label", "<UNKNOWN>"), "ip": override["ip"],
               "bnk_run": override.get("bnk_run") or bnk_run, "bp": sorted(bps),
               "source": "declared", "note": note}
    elif ip != "<unknown>":
        env = {"label": "<UNKNOWN>", "ip": ip, "bnk_run": bnk_run,
               "bp": sorted(bps), "source": "codittle", "note": ""}
    else:
        env = {"label": "<UNKNOWN>", "ip": "<unmapped>", "bnk_run": bnk_run,
               "bp": sorted(bps), "source": "none", "note": ""}

    return {
        "env": env,
        "artifacts": {**buckets, "untracked": untracked},
        "last_deploy": last,
        "tracked_count": len(files),
    }


# ── emit YAML for the auto block (hand-rolled; only our known shape) ───────
def _flow_list(items):
    return "[" + ", ".join(items) + "]" if items else "[]"


def render_auto(facts):
    env, arts, last = facts["env"], facts["artifacts"], facts["last_deploy"]
    lines = [AUTO_START + " regenerated by ctx_sync.py from .codittle/* — do not edit below"]
    lines += [
        "env:",
        f"  label:   {env['label']}",
        f"  ip:      {env['ip']}",
        f"  bnk_run: {env['bnk_run']}",
        f"  bp:      {_flow_list(env['bp'])}",
        f"  source:  {env.get('source', 'codittle')}",   # declared | codittle | none
    ]
    if env.get("note"):
        lines.append(f"  note:    {env['note']}")
    lines.append("artifacts:")
    for k in ("routines", "versions", "includes", "files", "params", "java", "untracked"):
        lines.append(f"  {k + ':':10} {_flow_list(arts[k])}")
    lines.append("last_deploy:")
    if last:
        lines += [
            f"  file:    {last['file']}",
            f"  version: {last['version']}",
            f"  at:      {last['at']}",
            f"  env:     {last['env']}",
        ]
    else:
        lines += ["  file:    <none>", "  version: <none>", "  at:      <none>", "  env:     <none>"]
    lines.append(AUTO_END)
    return "\n".join(lines) + "\n"


def replace_auto_block(yml_text, new_block):
    if AUTO_START in yml_text and AUTO_END in yml_text:
        pre = yml_text.split(AUTO_START, 1)[0]
        post = yml_text.split(AUTO_END, 1)[1]
        return pre.rstrip() + "\n\n" + new_block + post.lstrip("\n")
    # no markers yet — append
    return yml_text.rstrip() + "\n\n" + new_block


# ── human-declared env override (lives in the editable region) ─────────────
def read_env_override(yml_path):
    """Parse the optional top-level `env_override:` block from the human region."""
    try:
        with open(yml_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    out, inside = {}, False
    for line in lines:
        if line.startswith(AUTO_START):
            break
        if re.match(r"^env_override:\s*$", line):
            inside = True
            continue
        if inside:
            if re.match(r"^\S", line):           # next top-level key ends the block
                break
            m = re.match(r"^\s+([a-z_]+):\s*(.*?)\s*$", line)
            if m and m.group(2):
                out[m.group(1)] = m.group(2).strip().strip('"')
    return out or None


def set_env_override(yml_path, label, ip, bnk_run=None, note=None):
    """Insert/replace the `env_override:` block in the human region (before CTX_AUTO)."""
    if not os.path.isfile(yml_path):
        sys.exit(f"ERROR: {yml_path} not found — copy projects/_template/ into the project first.")
    with open(yml_path, encoding="utf-8") as fh:
        lines = fh.readlines()

    block = ["env_override:\n", f"  label:   {label}\n", f"  ip:      {ip}\n"]
    if bnk_run:
        block.append(f"  bnk_run: {bnk_run}\n")
    if note:
        block.append(f'  note:    "{note}"\n')

    # drop any existing env_override block (human region only)
    out, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].startswith(AUTO_START):
            out.extend(lines[i:])
            break
        if re.match(r"^env_override:\s*$", lines[i]):
            i += 1
            while i < n and (lines[i].startswith((" ", "\t")) or lines[i].strip() == ""):
                if lines[i].strip() == "":
                    break
                i += 1
            continue
        out.append(lines[i]); i += 1
    else:
        pass

    # insert the new block just before the CTX_AUTO marker (or at end)
    insert_at = next((k for k, l in enumerate(out) if l.startswith(AUTO_START)), len(out))
    new = out[:insert_at] + block + ["\n"] + out[insert_at:]
    with open(yml_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(new)


# ── flat parser for the human scalars INDEX needs ─────────────────────────
def read_human_fields(yml_path):
    out = {}
    try:
        with open(yml_path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(AUTO_START):
                    break
                m = re.match(r"^([a-z_]+):\s*(.*?)\s*$", line)
                if m and m.group(2):
                    val = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'")
                    if val:
                        out[m.group(1)] = val
    except OSError:
        pass
    return out


# ── per-project sync ──────────────────────────────────────────────────────
def sync_project(root, slug):
    proj_dir = os.path.join(root, slug)
    if not os.path.isdir(proj_dir):
        print(f"  ! {slug}: not found"); return None
    ctx_dir = os.path.join(proj_dir, "_ctx")
    yml = os.path.join(ctx_dir, "project.yml")
    facts = derive(proj_dir)

    if os.path.isfile(yml):
        with open(yml, encoding="utf-8") as fh:
            text = fh.read()
        new = replace_auto_block(text, render_auto(facts))
        if new != text:
            with open(yml, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new)
            print(f"  [ok]   {slug}: project.yml refreshed "
                  f"({facts['tracked_count']} tracked, {len(facts['artifacts']['untracked'])} untracked)")
        else:
            print(f"  [=]    {slug}: already up to date")
    else:
        print(f"  [skip] {slug}: no _ctx/project.yml (copy projects/_template/ first)")
    return facts


# ── INDEX.md ──────────────────────────────────────────────────────────────
def list_projects(root):
    out = []
    for entry in sorted(os.listdir(root)):
        p = os.path.join(root, entry)
        if os.path.isdir(p) and not entry.startswith("_") and not entry.startswith("."):
            out.append(entry)
    return out


def build_index(root):
    rows = []
    for slug in list_projects(root):
        yml = os.path.join(root, slug, "_ctx", "project.yml")
        human = read_human_fields(yml)
        facts = derive(os.path.join(root, slug))
        last = facts["last_deploy"]
        deploy = f"{os.path.basename(last['file'])} v{last['version']}" if last else "—"
        src = facts["env"]["source"]
        env_cell = facts["env"]["ip"]
        if src == "declared":
            env_cell += " *(declared)*"
        rows.append({
            "slug": slug,
            "title": human.get("title", slug),
            "kind": human.get("kind", "—"),
            "ticket": human.get("ticket", "—"),
            "status": human.get("status", "—"),
            "env_cell": env_cell,
            "env_source": src,
            "deploy": deploy,
            "has_ctx": os.path.isfile(yml),
        })

    lines = [
        "# Projects index",
        "",
        "_Generated by `ctx_sync.py` — do not edit by hand. See `PLAYBOOK.md` for the standard._",
        "",
        "| Project | Kind | Ticket | Status | Env | Last deploy | Ctx |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ctx = "✓" if r["has_ctx"] else "—"
        env = "**unmapped**" if r["env_source"] == "none" else f"`{r['env_cell']}`"
        lines.append(f"| [{r['title']}]({r['slug']}/CLAUDE.md) | {r['kind']} | {r['ticket']} "
                     f"| {r['status']} | {env} | {r['deploy']} | {ctx} |")
    lines.append("")
    n_ctx = sum(1 for r in rows if r["has_ctx"])
    unmapped = [r["slug"] for r in rows if r["env_source"] == "none"]
    lines.append(f"_{len(rows)} projects · {n_ctx} with a context layer · "
                 f"{len(unmapped)} unmapped._")
    if unmapped:
        lines += ["", "**Unmapped (no env):** " + ", ".join(f"`{s}`" for s in unmapped),
                  "", "> Declare with `ctx_sync.py <slug> --set-env LABEL:IP[:BNK_RUN]`."]
    out = os.path.join(root, "INDEX.md")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nINDEX.md rebuilt: {len(rows)} projects ({n_ctx} with _ctx, {len(unmapped)} unmapped).")
    if unmapped:
        print("Unmapped: " + ", ".join(unmapped))
        print("  declare with:  python ctx_sync.py <slug> --set-env LABEL:IP[:BNK_RUN]")


# ── main ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", nargs="?", help="project folder name")
    ap.add_argument("--all", action="store_true", help="sync every project")
    ap.add_argument("--index-only", action="store_true", help="only rebuild INDEX.md")
    ap.add_argument("--set-env", metavar="LABEL:IP[:BNK_RUN]",
                    help="declare/override this project's env (wins over Codittle's host)")
    ap.add_argument("--note", help="note to record with --set-env (e.g. why it moved)")
    ap.add_argument("--projects-root", help="override path to .../projects")
    args = ap.parse_args()

    root = find_projects_root(args.projects_root)
    print(f"projects root: {root}\n")

    if args.set_env:
        if not args.slug:
            ap.error("--set-env needs a <slug>")
        parts = args.set_env.split(":", 2)
        if len(parts) < 2:
            ap.error("--set-env format is LABEL:IP[:BNK_RUN]")
        label, ip = parts[0], parts[1]
        bnk_run = parts[2] if len(parts) == 3 else None
        yml = os.path.join(root, args.slug, "_ctx", "project.yml")
        set_env_override(yml, label, ip, bnk_run, args.note)
        print(f"  declared env for {args.slug}: {label} / {ip}"
              + (f"  ({args.note})" if args.note else ""))

    if not args.index_only:
        if args.all:
            for slug in list_projects(root):
                sync_project(root, slug)
        elif args.slug:
            sync_project(root, args.slug)
        elif not args.set_env:
            ap.error("give a <slug>, or --all, or --index-only")

    build_index(root)


if __name__ == "__main__":
    main()
