# Changelog

## [0.1.0] - 2026-09-19

First packaged release. Previously this lived as loose files in `~/.config/claude-muse` on one machine.

### Added

- **`./install.sh` sets the whole thing up** - proxy, launch agent, Claude Code profile and shell function - and is safe to re-run after a pull. `--check` validates the environment first, `--copy` freezes copies instead of symlinks, `--no-agent` places files without touching a running proxy.
- **`./uninstall.sh` removes it again**, leaving session history alone unless you pass `--purge`.
- **The proxy records every distinct request shape it sees** to `shapes.json`, so questions about which fields and beta headers Claude Code sends are answered by looking rather than guessing.
- **`proxy.log` rotates at 1 MiB**, and `proxy.err` now means a crash rather than a second copy of the log.

### Fixed

- **`tool_choice: {"type":"none"}` is no longer rewritten to `auto`.** Only the named form is repaired, which is the only form the endpoint rejects. The old blanket rule turned "do not call tools" into "call tools if you like" - the proxy granting a permission rather than repairing a shape.
- **A 400 that names several unsupported fields now costs one retry instead of several.** Error bodies are read to 64 KiB, so a field named late in a long message is no longer invisible.
- **The last repair attempt is no longer wasted.** On its final pass the retry loop learned a field, rewrote the request and then returned without sending it, so the caller got an empty 400 and the fix only took effect on the following request.
- **A client disconnecting mid-stream no longer writes a stack trace.** Those accounted for every traceback in `proxy.err`.

### Changed

- **The status line's one-turn lag is documented as inherent**, not as a bug waiting on the `count_tokens` failure. The two are unrelated; the earlier note in the README was wrong.
