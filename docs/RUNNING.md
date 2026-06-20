# Running the MCP server

The registry exposes its MCP server via `cregistry-mcp`. Two ways to run it.

## A. stdio (per-tool, nothing to manage)

Each tool spawns its own copy from the configured command and stops it when the
session ends. There is no daemon; "restart" = restart the tool session.

```bash
# the same command works in every tool:
uv run --directory /Users/skhemka/constraint-registry cregistry-mcp
```

Register it:
- **Claude Code:** `claude mcp add constraint-registry -- uv run --directory /Users/skhemka/constraint-registry cregistry-mcp`
- **Cursor / Codex:** an MCP entry with `command: uv`, `args: ["run","--directory","/Users/skhemka/constraint-registry","cregistry-mcp"]`.

## B. One shared HTTP server (start once, all tools connect)

Run a single long-lived process and point every URL-capable tool at it.

```bash
# foreground
uv run --directory /Users/skhemka/constraint-registry cregistry-mcp --http --port 8765
# background
nohup uv run --directory /Users/skhemka/constraint-registry cregistry-mcp --http --port 8765 \
  > /tmp/cregistry-mcp.log 2>&1 &
```

CLI flags: `--http` (or `--transport http`), `--host` (default `127.0.0.1`),
`--port` (default `8765`), `--config` (or `$CREGISTRY_CONFIG`),
`--reload-interval` (seconds; `0` = off, see below).

**Stop / restart:**
```bash
lsof -ti tcp:8765 | xargs kill        # stop
# restart = stop, then start again
```

### Hot reload (pick up constraint changes without restarting)

For an org where constraints change often, run with a refresh interval so the
server periodically re-imports from disk — no restart needed:

```bash
uv run --directory /Users/skhemka/constraint-registry cregistry-mcp \
  --http --port 8765 --reload-interval 60
```

How it behaves:
- Every `--reload-interval` seconds the server re-imports from the configured
  source paths and **publishes a new immutable bundle** as the latest version.
- It is a **no-op if nothing changed** (identical content hash).
- A reload that **fails** (e.g. an unresolvable precedence conflict or a
  config error) **keeps the last-good bundle serving** and logs the failure to
  stderr — the server never goes dark.
- Previous bundle versions remain retrievable by id (pin via the `version`
  argument), so in-flight consumers are unaffected by a swap.

Decoupling: the server reads constraints/policies from the configured source
paths on disk. Wire your constraints repos to sync/pull into those paths (a
separate ops job, e.g. a cron `git pull` or CI publish); the server picks up
whatever is on disk at the next refresh. With `--reload-interval` set you do
**not** need to restart after edits.

**Connect each tool** to `http://127.0.0.1:8765/mcp`:
- **Claude Code:** `claude mcp add --transport http constraint-registry http://127.0.0.1:8765/mcp`
- **Cursor** (`~/.cursor/mcp.json`): `{"mcpServers":{"constraint-registry":{"url":"http://127.0.0.1:8765/mcp"}}}`
- Tools that only speak stdio can keep the stdio command (Section A) alongside.

## C. Auto-start on login (macOS launchd)

Use the template at `deploy/com.cregistry.mcp.plist` (edit the `uv` path, project
dir, and config path for your machine), then:

```bash
cp deploy/com.cregistry.mcp.plist ~/Library/LaunchAgents/com.cregistry.mcp.plist
launchctl load  ~/Library/LaunchAgents/com.cregistry.mcp.plist   # start + enable at login
launchctl list | grep cregistry                                   # status
launchctl kickstart -k gui/$(id -u)/com.cregistry.mcp             # restart (e.g. after editing constraints)
launchctl unload ~/Library/LaunchAgents/com.cregistry.mcp.plist  # stop + disable
```

With `KeepAlive` set, launchd restarts the server if it crashes; `kickstart -k`
is the clean "restart" once it is managed by launchd. The template sets
`--reload-interval 60`, so day-to-day constraint changes (and config edits like
adding a source/engine, which reload re-reads) are picked up automatically —
`kickstart -k` is only needed for code/dependency changes.
