# Launching Muse from other tools

The shell function covers the terminal you type in. Anything else that starts Claude Code can start a Muse session too: a script, a tmux binding, an editor task, a session manager. Run the launcher wherever it would run `claude`:

```bash
~/.config/claude-muse/claude-muse -p "Reply with exactly: ok"
```

It takes every flag `claude` takes and passes them through untouched. Before it hands over, it sets the Muse identity from `model-env.sh` and runs `preflight.sh`, so the proxy is up and running the current engine. The shell function forwards to this same file, so a session you start by hand and one a tool starts are one program.

## Three things a tool has to know

**It is a program, not a function.** `env`, `exec`, `subprocess`, `tmux send-keys` and a terminal tab typed into by a script can all run it. None of them can run a shell function, which is why the launcher exists as a file.

**Sessions live in `~/.claude-profiles/muse`, not `~/.claude`.** A tool that decides whether a session is still running by reading `~/.claude/sessions/`, or finds transcripts under `~/.claude/projects/`, will not see a Muse session at all, and may start it a second time. Read both stores. For the same reason, `--resume <id>` only finds a Muse session when it goes through the launcher, because Claude Code looks the id up in its own store.

**The process is still `claude`.** The launcher ends with `exec claude`, so a tool that finds sessions by process name keeps working.

## medallion's md CLI and Switchboard

The repo ships an [`md-plugin.toml`](../md-plugin.toml), so medallion's `md` CLI can install it and launch sessions on it:

```bash
md plugin install claude-muse    # clones it, or adopts ~/projects/claude-muse, and runs install.sh
md driver list                   # Claude and Muse, both available
md review swarm run 1234 --auto --driver muse
```

**Once it is installed, Switchboard shows a "Runs on" picker** in Settings › Sessions, the Plan work dialog, the review-loop dialog and Autopilot's Scan. md runs `bin/claude-muse` where it would run `claude`, with the same flags, sets `CLAUDE_CONFIG_DIR` to the Muse store, and reads sessions from it afterwards, so the rail shows a Muse session live and resumes it on Muse.

The manifest's `[driver]` table is the whole integration: a label, the launcher, and the store it writes to. Another tool that launches Claude Code needs the same two facts.
