# Agent Relay

Lightweight inter-agent messaging for AI coding agents. Enables multiple agents to send messages to each other across separate terminal sessions, VMs, containers, or any environment where agents run.

Works with any AI agent that can execute shell commands — Claude Code, Cursor, Windsurf, Cline, Aider, custom agents, or plain scripts.

## Quick Start

### 1. Install

```bash
git submodule add https://github.com/vinnyusestrict/agent-relay.git lib/relay
```

Or clone directly:
```bash
git clone https://github.com/vinnyusestrict/agent-relay.git
```

### 2. Configure the database

Two backends supported. Pick one:

**Option A — SQLite** (zero deps, single file — good for solo setups):

```
# .relay-env
RELAY_DB_DRIVER=sqlite
RELAY_DB_PATH=~/.agent-relay.sqlite3
```

No server install needed. The file is auto-created on first use.

**Option B — MySQL / MariaDB** (default; good for multi-host / networked setups):

```bash
mysql -u root -proot -e "CREATE DATABASE IF NOT EXISTS agent_relay"
```

```
# .relay-env
RELAY_DB_USER=root
RELAY_DB_PASS=root
RELAY_DB_NAME=agent_relay
```

Either way: `.relay-env` lives in the relay directory (or your project root), is gitignored, and tables are auto-created on first use.

### 3. Register agents

Every agent registers itself with a unique name:

```bash
python3 lib/relay/relay-msg register alice \
  --cwd /path/to/alice-workspace \
  --group backend \
  --transport local
```

| Flag | Description |
|------|-------------|
| `--group <name>` | Join a broadcast group (repeatable) |
| `--cwd <pattern>` | Substring matched against working directory for auto-detection |
| `--transport local\|remote` | `local` = direct MySQL, `remote` = proxied (see below) |
| `--alias <name>` | Alternate name this agent also receives messages for (repeatable) |
| `--nudge-dir <path>` | Directory where this agent's `.nudge-<name>` flag file is written when nudged |

### 4. Send and receive

```bash
# Send to a specific agent
./relay-msg send alice "PR #37 is ready to merge"

# Send to a group
./relay-msg send backend "Deploy complete"

# Broadcast to all
./relay-msg send all "Going offline for maintenance"

# Check for unread messages (marks as read)
./relay-msg check

# Peek without marking as read
./relay-msg check --peek

# View full history
./relay-msg check --history
```

### 5. Set up auto-check (optional)

For agents that support hooks or startup scripts, auto-check on every prompt so incoming messages appear automatically.

**Claude Code** — add to `.claude/settings.json`:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 lib/relay/relay-msg check 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

**Cron / polling** — check every N seconds:
```bash
# In a crontab or background loop
* * * * * python3 /path/to/relay-msg check 2>/dev/null
```

**Custom agents** — call from your agent's message loop:
```python
import subprocess
result = subprocess.run(
    ["python3", "lib/relay/relay-msg", "check"],
    capture_output=True, text=True
)
if result.stdout.strip():
    # Feed messages into the agent's context
    print(result.stdout)
```

## Commands

| Command | Description |
|---------|-------------|
| `relay-msg send <target> <message>` | Send to an agent, group, or `all` |
| `relay-msg check` | Read unread messages (marks as read) |
| `relay-msg check --peek` | Read without marking as read |
| `relay-msg check --history` | Show all messages including read |
| `relay-msg register <name> [opts]` | Register or update an agent |
| `relay-msg unregister <name>` | Remove an agent |
| `relay-msg list` | List all agents, groups, aliases, and nudge dirs |
| `relay-msg nudge <agent> [message]` | Write a nudge flag file to the agent's registered nudge_dir |

## Identity Detection

When an agent runs a command, identity is resolved in order:

1. `RELAY_IDENTITY` env var — always wins
2. CWD pattern matching against registered agents' `cwd_pattern`
3. Falls back to `"default"`

## Remote Transport

For agents in VMs or containers without direct MySQL access, register with `--transport remote` and configure a proxy:

```
# In .relay-env
RELAY_TRANSPORT=remote
RELAY_PROXY_CMD=ssh myhost mysql
```

The proxy command receives the full `mysql -u ... -e '...'` invocation. If it contains `{}`, the MySQL command replaces the placeholder; otherwise it's appended.

## Wake-up nudges (optional)

An agent waiting for a message has no way to know one arrived until something wakes its session (a user prompt, a scheduled poll). For faster turn-around, `relay-msg send` can drop a **flag file** as a post-INSERT side-effect, and a separate watcher process picks up the flag and wakes the receiver's terminal pane.

### Per-agent nudge registry (recommended)

Register each agent with the path to its own project directory:

```bash
python3 relay-msg register alice \
  --cwd /workspace/alice \
  --nudge-dir /workspace/alice \
  --transport local
```

When any agent sends to `alice`, relay looks up `alice`'s `nudge_dir` and writes `/workspace/alice/.nudge-alice`. This solves cross-project nudging: each agent's watcher polls its own project directory, not a shared global path.

You can also write a flag directly without sending a message:

```bash
python3 relay-msg nudge alice
python3 relay-msg nudge alice "review PR #42"
```

`nudge` exits nonzero if the agent has no `nudge_dir` registered, or if the directory is missing/unwritable.

**Note:** DB-registered `nudge_dir` values are **not** subject to the `RELAY_NUDGE_AGENTS` allowlist — any registered recipient always gets nudged at their own path.

### Env-based fallback (legacy)

For setups where all agents share one directory, set two env vars in `.relay-env`:

```
RELAY_NUDGE_DIR=/path/to/shared/nudge-dir
RELAY_NUDGE_AGENTS=alice,bob,carol     # allowlist; unset = nudge everyone
```

This path is used only for recipients that have no `nudge_dir` in the DB. If `RELAY_NUDGE_DIR` is unset and the agent has no DB `nudge_dir`, nudging is skipped — no behavior change from the default.

**Flag-file contract** (for writing your own watcher):
- Filename: `.nudge-<agent-name>` in the agent's nudge dir.
- Contents: a single line of text (default: `check inbox`). Your watcher sends this text to the agent's pane.
- Watcher is expected to `rm -f` the flag after consuming.
- Best-effort: `relay-msg` logs a one-line warning to stderr on write failure and continues. Nudges are a wake-up optimization, not part of the delivery guarantee.

**Reference watchers** in `watchers/`:
- `cmux-nudge-watcher` — injects keystrokes to [cmux](https://github.com/anthropics/cmux) panes (only example; ships as template).
- `tmux-nudge-watcher` — `tmux send-keys` to a window/pane named after the agent.
- `notify-nudge-watcher` — macOS notification only (no keystroke injection). Useful for agents in GUI apps like Claude Desktop with no pane to type into.

Write your own watcher in any language — it's a plain directory-polling contract.

## Environment Variables

All configurable via `.relay-env` file or environment variables (env vars take precedence).

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_DB_DRIVER` | `mysql` | `mysql` or `sqlite` |
| `RELAY_DB_USER` | `root` | MySQL user *(mysql only)* |
| `RELAY_DB_PASS` | `root` | MySQL password *(mysql only)* |
| `RELAY_DB_NAME` | `agent_relay` | MySQL database name *(mysql only)* |
| `RELAY_DB_PATH` | `~/.agent-relay.sqlite3` | SQLite file path *(sqlite only)* |
| `RELAY_IDENTITY` | *(auto-detect)* | Force agent identity |
| `RELAY_TRANSPORT` | `local` | `local` or `remote` *(mysql only)* |
| `RELAY_PROXY_CMD` | *(none)* | Proxy command for remote transport *(mysql only)* |
| `RELAY_NUDGE_DIR` | *(unset)* | Directory to drop `.nudge-<agent>` flag files (unset = nudges disabled) |
| `RELAY_NUDGE_AGENTS` | *(all)* | Comma-separated allowlist of agents to nudge |

## Database

Four tables, auto-created on first use:

| Table | Purpose |
|-------|---------|
| `relay_agents` | Registered agents (name, transport, cwd_pattern, nudge_dir) |
| `relay_agent_groups` | Agent-to-group memberships |
| `relay_agent_aliases` | Alternate names an agent responds to |
| `relay_messages` | Message queue with read tracking |

Messages are never deleted — marked as read via `read_at` timestamp.

## Requirements

- Python 3.8+
- One of:
  - `sqlite3` CLI client (for SQLite backend — usually preinstalled on macOS / Linux)
  - `mysql` CLI client + MySQL or MariaDB server (for MySQL backend)
