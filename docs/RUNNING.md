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
`--port` (default `8765`), `--config` (or `$CREGISTRY_CONFIG`).

**Stop / restart:**
```bash
lsof -ti tcp:8765 | xargs kill        # stop
# restart = stop, then start again
```
> Restart after editing any constraint/source — the bundle is built once at
> startup, so a running server will not see edits until it is restarted.

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
is the clean "restart" once it is managed by launchd.
