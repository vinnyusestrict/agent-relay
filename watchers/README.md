# Nudge watchers

Reference implementations of the nudge flag-file contract (see main README
"Wake-up nudges"). Pick the one that matches your environment or copy one as
a starting point.

| Watcher | Wakes | When to use |
|---------|-------|-------------|
| `relay-nudge-watcher` | cmux workspace **or** tmux session (auto-detected) | **Multi-machine setups.** Run ONE instance per machine; it derives the local agent set from the relay DB and ignores flags for agents on other machines. |
| `cmux-nudge-watcher` | a cmux workspace (via `cmux send` / `cmux send-key`) | Single-machine setups where all agents run inside cmux. Dynamic workspace discovery — no hardcoded agent names. |
| `tmux-nudge-watcher` | a tmux window/pane (via `tmux send-keys`) | Single-machine setups where each agent has its own tmux session. |
| `notify-nudge-watcher` | a macOS or Linux desktop notification | Your agent runs in a GUI app (e.g. Claude Desktop) with no terminal to type into. |

## relay-nudge-watcher — one watcher per machine

`relay-nudge-watcher` is the recommended entry point for teams running agents
across multiple machines (laptops, VMs, CI hosts, Mac Minis, etc.).

### How it works

- All agents write flag files to **one shared directory** (e.g. a shared NFS
  mount, or each machine's own `RELAY_NUDGE_DIR`).
- Each machine runs its own `relay-nudge-watcher` pointing at that directory.
- When a `.nudge-<agent>` flag appears, the watcher checks whether `<agent>`
  belongs to **this machine**:
  - **Handled** — wake the session and remove the flag.
  - **Not handled** — leave the flag in place for the other machine's watcher.

### Handled-agent set

Two ways to specify which agents this machine owns:

**1. Automatic (recommended):** leave `--agents` unset. The watcher queries
the relay DB for agents whose `cwd_pattern` resolves to an existing path on
the local filesystem.

```bash
# Register alice with her workspace path as cwd_pattern + nudge_dir
python3 relay-msg register alice \
  --cwd /home/alice/project \
  --nudge-dir /home/alice/project \
  --transport local
```

When `relay-nudge-watcher` starts on a machine where `/home/alice/project`
exists, it automatically includes `alice` in its handled set.

**2. Explicit:** pass `--agents a,b,c`.

```bash
./watchers/relay-nudge-watcher --dir /shared/nudges --agents alice,bob
```

### Configuration

```
# .relay-env (in the relay directory or project root)
RELAY_NUDGE_DIR=/home/alice/project   # default watch dir for --dir
RELAY_DB_DRIVER=sqlite                # or mysql
RELAY_DB_PATH=~/.agent-relay.sqlite3  # sqlite only
```

### Running

```bash
# Long-running in its own tmux pane
tmux new-window -n NudgeWatcher './watchers/relay-nudge-watcher'

# Or under launchd (macOS) — restart automatically on exit
# com.example.relay-nudge-watcher.plist → ProgramArguments pointing here

# One-shot / cron mode (processes existing flags and exits)
./watchers/relay-nudge-watcher --once

# Explicit options
./watchers/relay-nudge-watcher \
  --dir /shared/nudge-flags \
  --agents alice,bob \
  --interval 5 \
  --log /var/log/relay-nudge.log
```

### Wake mechanism

`relay-nudge-watcher` tries wake backends in order:

1. **cmux** (if `cmux` is on PATH) — discovers all workspaces via
   `cmux tree --all`, matches agent name case-insensitively to workspace
   titles, then injects:
   ```
   cmux send --workspace <ws> <message>
   cmux send-key --workspace <ws> enter
   ```
2. **tmux** (if `tmux` is on PATH) — checks `TMUX_TARGET_<AGENT_UPPER>` env
   var first, then falls back to a session named after the agent:
   ```
   tmux send-keys -t <target> C-u <message> Enter
   ```

If no session is found, a `WARN` line is logged but the flag is still removed
(the message was already delivered to the relay DB — this is a wake-up
optimisation, not part of the delivery guarantee).

---

## Per-project watchers

For single-machine setups where every agent lives in one place, the simpler
per-project watchers are a good fit:

```bash
# Long-running in its own tmux pane (recommended)
tmux new-window -n Nudger './watchers/tmux-nudge-watcher'

# Or under launchd / systemd as a user service
```

`relay-nudge-watcher` and the per-project watchers are fully backwards
compatible — you can run both simultaneously without conflict as long as the
per-project watcher only handles agents on its own machine.

## Writing your own

The contract is deliberately minimal — any language that can list a directory
and fire a side-effect works. See the main README for the exact file shape.

```python
# 30-line Python example
import os, time, pathlib, subprocess

WATCH = pathlib.Path(os.environ.get("WATCH_DIR", "."))
while True:
    for flag in WATCH.glob(".nudge-*"):
        agent = flag.name[len(".nudge-"):]
        msg = flag.read_text().strip() or "check inbox"
        flag.unlink(missing_ok=True)
        # Your wake-up logic here (shell out, socket write, etc.)
        subprocess.run(["echo", f"wake {agent}: {msg}"])
    time.sleep(3)
```
