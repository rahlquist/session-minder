# Session Minder

A **Hermes Agent desktop plugin** that gives you a dedicated UI for browsing, searching, sorting, archiving, and purging your Hermes sessions — without modifying Hermes itself.

> Plugin id: `session-minder` · Display name: **Session Minder**

---

## Why

Hermes stores every conversation in a single SQLite database (`state.db`). Over time that grows to hundreds of sessions and hundreds of megabytes. The built-in `hermes sessions` CLI covers pruning and exporting, but there's no visual, point-and-click way to:

- see how big each session is,
- sort/filter the list,
- preview a session's content,
- move sessions into a separate archive database,
- export a single session to its own `.db` or `.json`,
- and do all of the above **safely** (automatic backup, restore, undo).

Session Minder fills that gap as a normal Hermes plugin: a Python backend (FastAPI) that talks to `SessionDB` directly, plus a React desktop half that renders as a full page inside the Hermes desktop app.

---

## Features

### Browse & inspect
- **Full-page view** (`/session-minder`) with a left session list and a right preview pane.
- **Search** by title or session id.
- **Sort** by last updated, date created, name, or size (asc/desc toggle).
- **Per-session size** computed from message content (shown in KB/MB).
- **Preview** the last 50 messages of any selected session without leaving the page.

### Select & act (bulk or single)
- Checkboxes for multi-select; action bar appears when rows are selected.
- **Delete** — soft-delete with an undo window (see below).
- **Archive** — copies the session into a dedicated archive profile's `state.db`, then marks the source row archived.
- **Export .db** — writes a standalone SQLite database containing a single session (portable, openable).
- **Export JSON** — downloads the session (with all messages) as a `.json` file.

### Safety first
- **Automatic backup** of `state.db` before any destructive operation, using SQLite's `backup()` API (consistent even while the gateway holds the db).
- **Free-space gate**: if there isn't ~2× the db size free, a full-screen warning requires typing `I ACCEPT THE RISK` to proceed.
- **Integrity check** runs on every backup; a corrupt backup aborts the operation.
- **Restore from backup** button (gateway must be stopped; the backend attempts to stop it on force).
- **Undo window**, user-selectable:
  - **30s auto-purge** (default) — soft-deleted rows are hidden and auto-purged after 30s.
  - **Hold until purge** — rows stay in a "Pending purge" tray until you click **Purge all now**.
  - Every actual deletion is still one atomic transaction per session, so a single bad row can't nuke a batch.
- **Protected sessions** (those with gateway runtime history that the delete guard refuses to touch) are hidden by default; a toggle reveals them with an explanatory dialog and offers Archive instead of Delete.

---

## Requirements

- Hermes Agent with the desktop app (`hermes desktop` / `hermes gui`).
- Hermes `>= 0.22.x` (uses `SessionDB`, the dashboard plugin API, and the desktop plugin SDK).
- Python 3.11+ (matches Hermes' own interpreter).
- `plugins.enabled` must include `session-minder` (the enable command does this).

---

## Installation

### From this repository (manual)

```bash
# 1. Clone
git clone https://github.com/<your-org>/session-minder.git
cd session-minder

# 2. Copy into your Hermes plugins directory
cp -r . ~/.hermes/plugins/session-minder

# 3. Enable the plugin (Python backend + agent side)
hermes plugins enable session-minder

# 4. Expose the desktop half to the desktop app
ln -sfn ~/.hermes/plugins/session-minder/desktop ~/.hermes/desktop-plugins/session-minder

# 5. Validate
hermes plugins validate ~/.hermes/plugins/session-minder
```

Then in the Hermes desktop app: **⌘K → Reload desktop plugins** (or restart the app). The plugin also appears in **Settings → Plugins** where you can flip it on/off.

### From the Hermes plugin catalog (if published)

```bash
hermes plugins install <owner>/session-minder
hermes plugins enable session-minder
```

---

## Usage

1. Open the Hermes desktop app.
2. Click **Session Minder** in the left sidebar (or open it from the command palette).
3. Browse the session list on the left; click a row to preview its content on the right.
4. Use the controls bar to **search**, **sort**, toggle **Show protected** / **Show archived**, and **Restore from backup**.
5. Check rows and use the action bar to **Delete**, **Archive**, **Export .db**, or **Export JSON** in bulk.

### Settings

Undo mode is persisted in the plugin's own small SQLite store (`~/.hermes/plugins/session-minder/data/queue.db`):

- `undo_mode`: `30s` (default) or `hold`
- `undo_seconds`: countdown for auto-purge (default 30)

These are read/written through the plugin's `/settings` endpoint.

---

## Architecture

```
~/.hermes/plugins/session-minder/
├── plugin.yaml              # agent plugin manifest
├── __init__.py              # no-op register() (agent side)
├── dashboard/
│   ├── manifest.json        # dashboard plugin manifest (api: plugin_api.py)
│   └── plugin_api.py        # FastAPI backend -> SessionDB
├── desktop/
│   └── plugin.js            # desktop UI (React via @hermes/plugin-sdk)
└── data/
    └── queue.db             # plugin-local state (pending purge queue + settings)
```

- **Backend** (`dashboard/plugin_api.py`) is mounted by Hermes at `/api/plugins/session-minder/`. All `state.db` access goes through `SessionDB`, so FTS triggers, the delete guard, and WAL handling stay intact.
- **Desktop half** (`desktop/plugin.js`) is a single uncompiled ESM file using `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime`. It calls the backend via `ctx.rest`.
- **No core changes**: nothing in Hermes itself is modified; the plugin only reads/writes through public seams (`SessionDB`, the dashboard plugin API, the desktop plugin SDK).

### Key endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + db path |
| GET | `/disk` | db size + free space |
| GET | `/holders` | processes holding state.db |
| GET | `/backups` | list available backups |
| POST | `/backup` | create a safety backup |
| GET | `/list` | paginated, sortable, searchable session list (with computed size) |
| GET | `/session/{id}` | metadata + last 50 messages |
| GET | `/export_json/{id}` | full session as JSON |
| POST | `/soft_delete` | hide + queue for purge (undo window) |
| POST | `/purge` | hard-delete queued sessions (one transaction each) |
| POST | `/delete` | immediate delete |
| POST | `/archive` | copy to archive profile db, then mark archived |
| POST | `/export_db` | write a standalone single-session `.db` |
| POST | `/restore` | restore state.db from a backup |
| GET/POST | `/pending`, `/cancel_purge` | manage the purge queue |
| GET/POST | `/settings` | undo mode & seconds |

---

## Safety model

1. **Backup before every destructive batch.** The first destructive action in a UI session ensures a fresh backup exists (reused if <1h old). Manual "Backup now" is also available.
2. **Free-space gate.** Needs ~2× the db size free; otherwise a full-screen "Proceed at risk" dialog requires typing `I ACCEPT THE RISK`.
3. **Integrity check** on every backup before it's trusted.
4. **Restore** refuses while any process holds the db (gateway/desktop/dashboard/cron); `force` attempts to stop the gateway first.
5. **Protected sessions** can't be deleted at all (the delete guard would fire); the UI only offers Archive.
6. **Undo** is non-destructive until purge; pending rows are excluded from normal lists.

---

## Development

```bash
# Validate without installing
hermes plugins validate ./session-minder

# Run the backend import check
cd session-minder/dashboard && python3 -c "import plugin_api; print('ok')"
```

The desktop half is a single uncompiled ESM file — edit `desktop/plugin.js` and use **⌘K → Reload desktop plugins** to see changes instantly (no build step).

---

## Disclosure (catalog rule 13)

What a user should know before installing:

- **No network calls.** The plugin makes no requests to any third-party service. All data stays local.
- **Reads `state.db`.** The plugin reads and (on destructive actions) writes the Hermes session store (`$HERMES_HOME/state.db`) through `SessionDB`. This is the plugin's core purpose.
- **Writes backups.** Safety backups are written to `$HERMES_HOME/backups/` (up to ~2× the db size). The archive profile db is created at `$HERMES_HOME/profiles/archive/state.db`.
- **Shell commands.** On a forced restore, the plugin runs `hermes gateway stop` to release the db lock. No other shell commands are spawned.
- **Long-running background process.** A daemon thread runs every 5 seconds to auto-purge soft-deleted sessions (only in 30s undo mode).
- **No credentials.** The plugin does not read, store, or transmit any secrets, tokens, or auth data.
- **Telemetry.** None.

## License

MIT
EOF
echo written