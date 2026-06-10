# Nudge watchers

Reference implementations of the nudge flag-file contract (see main README
"Wake-up nudges"). Pick the one that matches your environment or copy one as
a starting point.

| Watcher | Wakes | When to use |
|---------|-------|-------------|
| `cmux-nudge-watcher` | a cmux workspace (via `cmux send` / `cmux send-key`) | You run agents inside cmux (Claude Code multiplexer). Dynamic workspace discovery — no hardcoded agent names. |
| `tmux-nudge-watcher` | a tmux window/pane (via `tmux send-keys`) | You run each agent inside its own tmux session. |
| `notify-nudge-watcher` | a macOS or Linux desktop notification | Your agent runs in a GUI app (e.g. Claude Desktop) with no terminal to type into. |

## Running

Watchers poll `WATCH_DIR` (defaults to the parent of the watcher's own
directory — put the watcher next to your `.relay-env` and it finds flag files
dropped there by `relay-msg send`).

```bash
# Long-running in its own tmux pane (recommended)
tmux new-window -n Nudger './watchers/tmux-nudge-watcher'

# Or under launchd / systemd as a user service
```

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
