# Nudge watchers

Reference implementations of the nudge flag-file contract (see main README
"Wake-up nudges"). Pick the one that matches your environment or copy one as
a starting point.

| Watcher | Wakes | When to use |
|---------|-------|-------------|
| `relay-nudge-watcher` | cmux workspace **or** tmux session (auto-detected) | **Multi-machine setups.** Run ONE instance per machine: `relay-nudge-watcher <dir>`. The handled-agent set is discovered live from the local cmux/tmux sessions; flags for agents on other machines are left alone. |
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
  has a **live session on this machine**:
  - **Yes** — wake the session and remove the flag.
  - **No** — leave the flag in place for the other machine's watcher.

### Handled-agent set

The handled set is discovered **live from the terminal multiplexer** and
re-checked every poll — a machine handles exactly the agents it currently has
sessions open for. There is nothing to configure: open an agent's cmux
workspace (or tmux session) and that agent is handled; close it and its flags
are left for whichever machine does host it.

- **cmux** (preferred): workspace titles from `cmux tree --all --json`.
- **tmux** (when cmux is absent): session names from `tmux list-sessions`.

Matching is case-insensitive — `.nudge-PHD` wakes a workspace titled `PHD`.

**Override (optional):** pass `--agents a,b,c` to pin a static set instead —
useful on a machine whose multiplexer this script can't introspect.

```bash
./watchers/relay-nudge-watcher /shared/nudges --agents alice,bob
```

### Configuration

```
# .relay-env (in the relay directory or project root)
RELAY_NUDGE_DIR=/shared/nudges        # default watch dir when <dir> is omitted
```

### Running

```bash
# Watch a directory — agents auto-discovered from local cmux/tmux sessions
./watchers/relay-nudge-watcher /shared/nudges

# Default the directory from RELAY_NUDGE_DIR in .relay-env
./watchers/relay-nudge-watcher

# Long-running in its own tmux pane
tmux new-window -n NudgeWatcher './watchers/relay-nudge-watcher /shared/nudges'

# Or under launchd (macOS) — restart automatically on exit
# com.example.relay-nudge-watcher.plist → ProgramArguments pointing here.
# launchd has a minimal PATH: set EnvironmentVariables→PATH to include the
# directory holding the `cmux` (or `tmux`) binary, or wake calls will no-op.

# One-shot / cron mode (processes existing flags and exits)
./watchers/relay-nudge-watcher /shared/nudges --once

# Tuning
./watchers/relay-nudge-watcher /shared/nudge-flags \
  --interval 5 \
  --log /var/log/relay-nudge.log
```

### Wake mechanism

`relay-nudge-watcher` tries wake backends in order:

1. **cmux** (if `cmux` is on PATH) — discovers all workspaces via
   `cmux tree --all --json` (falling back to text parsing on older builds),
   matches agent name case-insensitively to workspace titles, then injects:
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
