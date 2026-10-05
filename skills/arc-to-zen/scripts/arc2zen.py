#!/usr/bin/env python3
"""Migrate Arc (macOS) spaces, pinned tabs, folders and favorites into Zen workspaces.

Commands:
  inventory                    Print Arc profiles/spaces with counts; save plan-<profile>.json
  dump <zen_profile_dir>       Print a Zen profile's zen-sessions.jsonlz4 as JSON
  write <arc_profile> <zen_profile_dir> [<template_profile_dir>]
                               Replace the Zen profile's sidebar with one Arc profile's spaces.
                               The template (a session saved by this Zen version, containing a
                               space and a folder) defaults to the target profile itself.
  verify <arc_profile> <zen_profile_dir>
                               Compare the Zen profile's saved sidebar with the Arc profile.
  stage-history <zen_profile_dir>...
                               Expose Arc's History files as "Chromium" profiles for Zen's
                               importer and lift the importer's 2000-URL / 180-day limits.
  unstage-history <zen_profile_dir>...
                               Undo stage-history.

<arc_profile> is "default" or an Arc profile folder such as "Profile 1" (see inventory).
Zen must be closed for write, verify and unstage-history. Work files go to $ARC2ZEN_WORKDIR
(default ~/arc2zen-migration).
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import lz4.block

ARC_SIDEBAR = Path.home() / "Library/Application Support/Arc/StorableSidebar.json"
WORK = Path(os.environ.get("ARC2ZEN_WORKDIR", "~/arc2zen-migration")).expanduser()
MOZLZ4_MAGIC = b"mozLz40\0"
UNPINNED_FOLDER = "Arc – unpinned"


# ---------- mozlz4 ----------

def read_mozlz4(path):
    raw = Path(path).read_bytes()
    if raw[:8] != MOZLZ4_MAGIC:
        raise ValueError(f"{path}: not a mozlz4 file")
    return json.loads(lz4.block.decompress(raw[8:]))


def write_mozlz4(path, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    Path(path).write_bytes(MOZLZ4_MAGIC + lz4.block.compress(payload, store_size=True))


# ---------- Arc parsing ----------

def pairs(flat):
    """Arc stores dicts as flat [key, value, key, value, ...] lists."""
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def profile_key(profile):
    if profile.get("default"):
        return "default"
    return profile["custom"]["_0"]["directoryBasename"]


def profile_machine(profile):
    return profile.get("custom", {}).get("_0", {}).get("machineID")


def space_icon(space):
    icon = (space.get("customInfo") or {}).get("iconType") or {}
    return icon.get("emoji_v2")


def parse_arc(path=ARC_SIDEBAR):
    data = json.loads(Path(path).read_text())
    container = next(c for c in data["sidebar"]["containers"] if "spaces" in c)
    items = {k: v for k, v in pairs(container["items"])}

    def node(item_id):
        item = items.get(item_id)
        if not item:
            return []
        d = item.get("data", {})
        if "tab" in d:
            t = d["tab"]
            return [{
                "type": "tab",
                "url": t.get("savedURL"),
                "title": item.get("title") or t.get("savedTitle") or t.get("savedURL"),
                "renamed": bool(item.get("title")),
            }]
        if "splitView" in d:  # flatten: keep the tabs, drop the split layout
            return [n for c in item.get("childrenIds", []) for n in node(c)]
        if "list" in d:
            return [{
                "type": "folder",
                "name": item.get("title") or "Folder",
                "children": [n for c in item.get("childrenIds", []) for n in node(c)],
            }]
        # Unknown kinds (easel, note, ...) are reported, not migrated.
        return [{"type": "skipped", "kind": next(iter(d), None), "title": item.get("title")}]

    def children(container_id):
        item = items.get(container_id)
        return [n for c in (item or {}).get("childrenIds", []) for n in node(c)]

    favorites = {}
    for prof, cid in pairs(container.get("topAppsContainerIDs", [])):
        favorites[(profile_key(prof), profile_machine(prof))] = children(cid)

    profiles = {}
    for _, space in pairs(container["spaces"]):
        if not isinstance(space, dict):
            continue
        prof = space.get("profile", {})
        key = profile_key(prof)
        ids = dict(pairs(space.get("containerIDs", [])))
        p = profiles.setdefault(key, {"spaces": [], "favorites": []})
        p["spaces"].append({
            "name": space.get("title") or "Space",
            "icon": space_icon(space),
            "pinned": children(ids.get("pinned")),
            "unpinned": children(ids.get("unpinned")),
        })
        if not p["favorites"]:
            fav = favorites.get((key, profile_machine(prof)))
            if fav is None:  # fall back to any device's favorites for this profile
                fav = next((v for (k, _), v in favorites.items() if k == key and v), [])
            p["favorites"] = fav
    return profiles


def count(nodes):
    tabs = folders = skipped = 0
    for n in nodes:
        if n["type"] == "tab":
            tabs += 1
        elif n["type"] == "folder":
            folders += 1
            t, f, s = count(n["children"])
            tabs, folders, skipped = tabs + t, folders + f, skipped + s
        else:
            skipped += 1
    return tabs, folders, skipped


def arc_extensions(profile_dir):
    """Names of user-installed extensions (location 1 = Web Store) in an Arc profile."""
    prefs = Path.home() / "Library/Application Support/Arc/User Data" / profile_dir / "Secure Preferences"
    try:
        settings = json.loads(prefs.read_text())["extensions"]["settings"]
    except (OSError, KeyError, ValueError):
        return []
    return sorted({v["manifest"]["name"] for v in settings.values()
                   if v.get("location") == 1 and v.get("manifest", {}).get("name")})


def inventory():
    WORK.mkdir(parents=True, exist_ok=True)
    profiles = parse_arc()
    for key, p in profiles.items():
        print(f"== Arc profile {key!r}: {len(p['favorites'])} favorites -> Essentials")
        tot = [0, 0, 0, 0]
        for s in p["spaces"]:
            t, f, sk = count(s["pinned"])
            ut, _, _ = count(s["unpinned"])
            tot = [tot[0] + t, tot[1] + f, tot[2] + ut, tot[3] + sk]
            print(f"   {s['icon'] or ' '} {s['name']:<14} pinned {t:>3} tabs / {f:>2} folders"
                  f"   unpinned {ut:>3}   skipped {sk}")
        print(f"   total: {tot[0]} pinned tabs, {tot[1]} folders, {tot[2]} unpinned, {tot[3]} skipped")
        exts = arc_extensions("Default" if key == "default" else key)
        print(f"   extensions: {', '.join(exts) or 'none'}")
        out = WORK / f"plan-{key.replace(' ', '_')}.json"
        out.write_text(json.dumps(p, ensure_ascii=False, indent=2))
        print(f"   plan written to {out}")


# ---------- Zen writing ----------
# Shapes below mirror a zen-sessions.jsonlz4 written by Zen 1.23b.

def zen_running():
    return subprocess.run(["pgrep", "-f", "Zen.app/Contents/MacOS/zen"],
                          capture_output=True).returncode == 0


class Ids:
    def __init__(self):
        self.ms = int(time.time() * 1000)
        self.n = 0

    def tab(self):
        self.ms += 1
        return f"{self.ms}-{uuid.uuid4()}"

    def folder(self):
        self.ms += 1
        self.n += 1
        return f"{self.ms}-{self.n}"


def make_tab(sync_id, url, title, workspace_id, group_id=None, essential=False, static_label=None):
    t = {
        "entries": [{"url": url, "title": title}],
        "lastAccessed": int(time.time() * 1000),
        "pinned": True,
        "hidden": False,
        "zenWorkspace": None if essential else workspace_id,
        "zenSyncId": sync_id,
        "zenEssential": essential,
        "zenDefaultUserContextId": "true",
        "zenPinnedIcon": None,
        "zenIsEmpty": False,
        "zenHasStaticIcon": False,
        "zenGlanceId": None,
        "zenIsGlance": False,
        "_zenPinnedInitialState": {"entry": {"url": url, "title": title}, "image": None},
        "zenLiveFolderItemId": None,
        "searchMode": None,
        "userContextId": 0,
        "attributes": {},
        "index": 1,
        "userTypedValue": "",
        "userTypedClear": 0,
    }
    if group_id:
        t["groupId"] = group_id
    if static_label:
        t["zenStaticLabel"] = static_label
    return t


def make_empty_tab(tpl_empty, sync_id, workspace_id, group_id):
    e = copy.deepcopy(tpl_empty)
    e.update({"zenSyncId": sync_id, "zenWorkspace": workspace_id, "groupId": group_id,
              "lastAccessed": int(time.time() * 1000)})
    return e


def build_session(template, arc_profile):
    tpl_empty = next((t for t in template.get("tabs", []) if t.get("zenIsEmpty") and t.get("groupId")), None)
    tpl_space = next(iter(template.get("spaces") or []), None)
    if tpl_empty is None or tpl_space is None:
        sys.exit("Template needs at least one space and one folder. Create them in Zen, quit, rerun.")

    ids = Ids()
    tabs, folders, groups, spaces = [], [], [], []

    for fav in arc_profile["favorites"]:
        if fav["type"] == "tab":
            tabs.append(make_tab(ids.tab(), fav["url"], fav["title"], None, essential=True,
                                 static_label=fav["title"] if fav["renamed"] else None))

    def emit(nodes, workspace_id, parent_id, first_prev):
        """Append tabs/folders in sidebar order. Returns the empty-tab ids in this subtree."""
        prev = first_prev
        empty_ids = []
        for n in nodes:
            if n["type"] == "tab":
                t = make_tab(ids.tab(), n["url"], n["title"], workspace_id, parent_id,
                             static_label=n["title"] if n["renamed"] else None)
                tabs.append(t)
                prev = {"type": "tab", "id": t["zenSyncId"]}
            elif n["type"] == "folder":
                fid = ids.folder()
                empty_id = ids.tab()
                tabs.append(make_empty_tab(tpl_empty, empty_id, workspace_id, fid))
                folder = {
                    "pinned": True, "splitViewGroup": False, "id": fid, "name": n["name"],
                    "collapsed": True, "saveOnWindowClose": True, "parentId": parent_id,
                    "prevSiblingInfo": prev, "emptyTabIds": None, "userIcon": "",
                    "workspaceId": workspace_id,
                }
                folders.append(folder)
                groups.append({"pinned": True, "splitView": False, "id": fid, "name": n["name"],
                               "color": "zen-workspace-color", "collapsed": True,
                               "saveOnWindowClose": True})
                inner = emit(n["children"], workspace_id, fid, {"type": "tab", "id": empty_id})
                # A folder lists its own placeholder plus every placeholder below it.
                folder["emptyTabIds"] = [empty_id] + inner
                empty_ids += folder["emptyTabIds"]
                prev = {"type": "group", "id": fid}
        return empty_ids

    for s in arc_profile["spaces"]:
        sp = {"uuid": "{" + str(uuid.uuid4()) + "}", "name": s["name"],
              "theme": copy.deepcopy(tpl_space.get("theme")), "containerTabId": 0,
              "hasCollapsedPinnedTabs": False}
        if s["icon"]:
            sp["icon"] = s["icon"]
        spaces.append(sp)
        nodes = list(s["pinned"])
        if s["unpinned"]:
            nodes.append({"type": "folder", "name": UNPINNED_FOLDER, "children": s["unpinned"]})
        emit(nodes, sp["uuid"], None, {"type": "start", "id": None})

    return {"lastCollected": int(time.time() * 1000), "tabs": tabs, "folders": folders,
            "splitViewData": [], "groups": groups, "spaces": spaces}


def write(arc_key, zen_dir, template_dir=None):
    zen_dir = Path(zen_dir).expanduser()
    template_file = Path(template_dir or zen_dir).expanduser() / "zen-sessions.jsonlz4"
    if zen_running():
        sys.exit("Zen is running. Quit it first (⌘Q), then rerun.")
    session_file = zen_dir / "zen-sessions.jsonlz4"
    if not template_file.exists():
        sys.exit(f"{template_file} not found. Capture the template first (see the plan, Phase 1).")
    profiles = parse_arc()
    if arc_key not in profiles:
        sys.exit(f"Unknown Arc profile {arc_key!r}. Choices: {list(profiles)}")

    session = build_session(read_mozlz4(template_file), profiles[arc_key])

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = WORK / "backups" / f"{zen_dir.name}-{stamp}"
    backup.mkdir(parents=True)
    for name in ("zen-sessions.jsonlz4", "zen-sessions-backup", "sessionstore.jsonlz4",
                 "sessionstore-backups"):
        src = zen_dir / name
        if src.is_dir():
            shutil.copytree(src, backup / name)
        elif src.exists():
            shutil.copy2(src, backup / name)
    print(f"Backed up session files to {backup}")

    (WORK / f"written-{zen_dir.name}.json").write_text(json.dumps(session, ensure_ascii=False, indent=1))
    write_mozlz4(session_file, session)
    assert read_mozlz4(session_file) == session

    real = [t for t in session["tabs"] if not t["zenIsEmpty"]]
    print(f"Wrote {len(session['spaces'])} spaces, {len(real)} tabs "
          f"({sum(t['zenEssential'] for t in real)} essentials), "
          f"{len(session['folders'])} folders to {session_file}")


def verify(arc_key, zen_dir):
    if zen_running():
        sys.exit("Zen is running. Quit it first (⌘Q) so it flushes its session file, then rerun.")
    arc = parse_arc()[arc_key]
    session = read_mozlz4(Path(zen_dir).expanduser() / "zen-sessions.jsonlz4")
    real = [t for t in session.get("tabs", []) if not t.get("zenIsEmpty")]
    pinned = sum(count(s["pinned"])[0] + count(s["unpinned"])[0] for s in arc["spaces"])
    expected = {
        "spaces": len(arc["spaces"]),
        "essentials": sum(1 for f in arc["favorites"] if f["type"] == "tab"),
        "pinned tabs": pinned,
        "folders": sum(count(s["pinned"])[1] + bool(s["unpinned"]) for s in arc["spaces"]),
    }
    actual = {
        "spaces": len(session.get("spaces", [])),
        "essentials": sum(1 for t in real if t.get("zenEssential")),
        "pinned tabs": sum(1 for t in real if t.get("pinned") and not t.get("zenEssential")),
        "folders": len(session.get("folders", [])),
    }
    ok = True
    for k in expected:
        mark = "ok" if expected[k] == actual[k] else "MISMATCH"
        ok &= mark == "ok"
        print(f"{k:<12} arc {expected[k]:>4}   zen {actual[k]:>4}   {mark}")
    sys.exit(0 if ok else 1)


# ---------- History import staging ----------
# Zen's importer has no Arc entry, but its Chromium importer reads
# ~/Library/Application Support/Chromium/<profile>/History, and Arc's History is the same format.

CHROMIUM = Path.home() / "Library/Application Support/Chromium"
ARC_USER_DATA = Path.home() / "Library/Application Support/Arc/User Data"
STAGE_MARKER = CHROMIUM / ".arc2zen-staged.json"
USER_JS_HEADER = "// Temporary: Arc history import (remove after import)"
HISTORY_PREFS = {"browser.migrate.chrome.history.limit": 0,
                 "browser.migrate.history.maxAgeInDays": 3650}


def arc_profile_names():
    info = json.loads((ARC_USER_DATA / "Local State").read_text())["profile"]["info_cache"]
    return {d: v.get("name") or d for d, v in info.items() if (ARC_USER_DATA / d / "History").exists()}


def stage_history(zen_dirs):
    if (CHROMIUM / "Local State").exists() and not STAGE_MARKER.exists():
        sys.exit(f"{CHROMIUM} holds a real Chromium install; staging would clobber it. Aborting.")
    created = []
    names = arc_profile_names()
    for d in names:
        dst = CHROMIUM / d
        dst.mkdir(parents=True, exist_ok=True)
        created.append(str(dst))
        # Chromium copies its History while the browser runs; integrity_check confirms the copy.
        shutil.copy2(ARC_USER_DATA / d / "History", dst / "History")
        res = subprocess.run(["sqlite3", "-readonly", str(dst / "History"), "pragma integrity_check;"],
                             capture_output=True, text=True).stdout.strip()
        if res != "ok":
            sys.exit(f"Copied History for {d} is inconsistent ({res}). Quit Arc and rerun.")
    (CHROMIUM / "Local State").write_text(json.dumps(
        {"profile": {"info_cache": {d: {"name": f"Arc – {n}"} for d, n in names.items()}}},
        ensure_ascii=False))
    created.append(str(CHROMIUM / "Local State"))
    for zd in zen_dirs:
        user_js = Path(zd).expanduser() / "user.js"
        if user_js.exists() and not user_js.read_text().startswith(USER_JS_HEADER):
            sys.exit(f"{user_js} already exists with your own settings. Set the prefs in about:config instead: "
                     + ", ".join(f"{k}={v}" for k, v in HISTORY_PREFS.items()))
        user_js.write_text(USER_JS_HEADER + "\n" + "".join(
            f'user_pref("{k}", {v});\n' for k, v in HISTORY_PREFS.items()))
    STAGE_MARKER.write_text(json.dumps(created))
    for d, n in names.items():
        print(f"Staged Arc profile {d!r} as Chromium profile \"Arc – {n}\"")
    print("Restart Zen, then File → Import From Another Browser… → Chromium, once per Zen profile.")


def unstage_history(zen_dirs):
    if zen_running():
        sys.exit("Zen is running. Quit it first (⌘Q), then rerun.")
    if STAGE_MARKER.exists():
        for p in map(Path, json.loads(STAGE_MARKER.read_text())):
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        STAGE_MARKER.unlink()
        print(f"Removed staged Arc history from {CHROMIUM}")
    for zd in zen_dirs:
        zd = Path(zd).expanduser()
        user_js = zd / "user.js"
        if user_js.exists() and user_js.read_text().startswith(USER_JS_HEADER):
            user_js.unlink()
        # user.js values persist in prefs.js after the file is gone; reset them to defaults.
        prefs = zd / "prefs.js"
        if prefs.exists():
            lines = prefs.read_text().splitlines(keepends=True)
            kept = [l for l in lines if not any(f'"{k}"' in l for k in HISTORY_PREFS)]
            prefs.write_text("".join(kept))
        print(f"Reset history-import prefs in {zd.name}")


def main(argv):
    cmd, args = (argv[1], argv[2:]) if len(argv) > 1 else (None, [])
    if cmd == "inventory" and not args:
        inventory()
    elif cmd == "dump" and len(args) == 1:
        print(json.dumps(read_mozlz4(Path(args[0]).expanduser() / "zen-sessions.jsonlz4"),
                         ensure_ascii=False, indent=1))
    elif cmd == "write" and len(args) in (2, 3):
        write(*args)
    elif cmd == "verify" and len(args) == 2:
        verify(*args)
    elif cmd == "stage-history" and args:
        stage_history(args)
    elif cmd == "unstage-history" and args:
        unstage_history(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
