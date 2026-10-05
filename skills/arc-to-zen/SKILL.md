---
name: arc-to-zen
description: Migrate from Arc to Zen browser on macOS — spaces, pinned tabs, folders and favorites become Zen workspaces, folders and Essentials; also history, passwords and extensions. Use when the user wants to switch from Arc to Zen or bring Arc data into Zen.
---

# Arc → Zen

Zen (Firefox-based) has no Arc importer, and Arc keeps its sidebar in its own `StorableSidebar.json`, not in Chromium bookmarks. `scripts/arc2zen.py` reads that file and writes Zen's sidebar file (`zen-sessions.jsonlz4`) directly, copying exact object shapes from a **template**: a session the user's own Zen build has saved. Everything else goes through Zen's own UI.

Mapping:

| Arc | Zen |
|---|---|
| Arc profile | Zen profile (one per Arc profile) |
| Space (name, emoji) | Workspace (name, icon) |
| Pinned tab / folder / nested folder | Pinned tab / folder / subfolder (renamed tabs keep their label) |
| Favorites (per profile) | Essentials |
| Unpinned "today" tabs | Collapsed folder "Arc – unpinned" in that workspace |
| Split view | Its tabs, unsplit |

## Setup

```sh
WORK=~/arc2zen-migration
python3 -m venv $WORK/.venv && $WORK/.venv/bin/pip install -q lz4
alias arc2zen="$WORK/.venv/bin/python <this skill>/scripts/arc2zen.py"
```

Zen profiles live in `~/Library/Application Support/zen/Profiles/<salt>.<name>`; `profiles.ini` beside them lists them. `arc2zen` with no arguments prints its commands.

## Steps

Ask nothing the environment answers; ask the user only where a step names them.

### 1. Inventory

Run `arc2zen inventory`. It prints each Arc profile's spaces with pinned-tab/folder/unpinned counts, favorites, and extensions. Also look for things wired to Arc: `grep -rIl -iE 'company\.thebrowser|Arc\.app|9222'` over `~/.config`, `~/.hammerspoon`, `~/.claude.json`, `~/.claude/settings.json`, LaunchAgents. Port 9222 means a Chrome DevTools Protocol consumer (e.g. a `chrome-devtools` MCP with `--browserUrl`); Zen cannot serve CDP, so plan to drop `--browserUrl` so the MCP launches its own Chrome, or keep a Chromium browser for it.

Done when you can show the user a table of Arc profiles → spaces → counts, and a list of Arc-dependent integrations.

### 2. Back up

Copy `~/Library/Application Support/Arc/Storable*.json` to a dated folder in `$WORK`. Arc stays installed and untouched until the user decides to remove it.

### 3. Create Zen profiles

With Zen closed: `/Applications/Zen.app/Contents/MacOS/zen -CreateProfile "<name>"` per Arc profile. Leave existing Zen profiles alone. To make one the default, in `profiles.ini` move `Default=1` to its `[ProfileN]` section and point the `[Install…]` section's `Default=` at its path (back up `profiles.ini` first).

Tell the user how to open each one: Zen normally opens the default; `open -na Zen --args -P "<name>"` opens another, side by side; `about:profiles` switches from inside Zen.

### 4. Template (user)

Ask the user to open the default new profile and: skip the welcome screen's "import from another browser" (it has no Arc option); create a space; pin a tab; create a folder containing a subfolder with a pinned tab; add one Essential; quit with ⌘Q. Then have them open each other new profile once and quit.

Done when `arc2zen dump <template_profile>` shows non-empty `spaces`, `folders`, and a tab with `zenIsEmpty: true` and a `groupId`. If keys differ from what `make_tab` / `build_session` emit (Zen changed its format), update the script to match the dump before writing.

### 5. Write

Zen must be fully quit (the script refuses otherwise). For each Arc profile:

```sh
arc2zen write "<arc_profile>" "<zen_profile_dir>" "<template_profile_dir>"
```

The previous session files are backed up under `$WORK/backups/`. This replaces the profile's sidebar: the template's test items disappear.

### 6. Verify round-trip

Open each profile, wait for it to load, quit it (`osascript -e 'tell application "Zen" to quit'`), then `arc2zen verify "<arc_profile>" "<zen_profile_dir>"`. Zen rewrites the file on quit, so this proves it accepted the data.

Done when every row prints `ok` for every profile. On `MISMATCH`, dump the file, find what Zen dropped, fix the shape, restore from `$WORK/backups/`, and rewrite.

### 7. Passwords (user)

Arc encrypts saved passwords with its own keychain key, so no importer can read them. The user exports each Arc profile to CSV (Arc settings → Passwords → Export), imports each into the matching Zen profile at `about:logins` → ⋯ → Import from a File, then deletes the CSVs. Password-manager extensions (Bitwarden, iCloud Passwords) just need installing and signing in.

### 8. History

```sh
arc2zen stage-history "<zen_profile_dir>" ...   # every target profile
```

This copies each Arc profile's `History` into `~/Library/Application Support/Chromium/<dir>/` as "Arc – <name>" (refusing if a real Chromium install lives there), and adds a temporary `user.js` lifting the importer's default cap of 2000 URLs / 180 days. The user then restarts Zen and, in each Zen profile, runs File → Import From Another Browser… → Chromium → the matching "Arc – <name>" → History only. Each profile imports only its own Arc profile.

After the user confirms, with Zen quit: `arc2zen unstage-history "<zen_profile_dir>" ...`.

### 9. Extensions (user)

Map each Arc extension from the inventory to its addons.mozilla.org equivalent, per Zen profile. Drop ones Firefox covers natively (JSON viewers) or that overlap (several ad blockers → uBlock Origin). Flag Chrome-only ones to keep in a Chromium browser. Tampermonkey scripts move via its Utilities → Export/Import.

### 10. Integrations and cutover

Update what step 1 found (Raycast's Arc extension → Zen extension, URL routers like Velja → Zen profiles, CDP consumers). Cookies don't migrate: the user signs in again. Setting Zen as default browser, uninstalling Arc, and deleting old Zen profiles are the user's call, after they've lived in Zen for a while; offer, don't do.
