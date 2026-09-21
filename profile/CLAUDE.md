# Working in a muse session

This file is loaded in every claude-muse session, on top of whatever the repo you're in says. The repo wins on anything specific to it.

## Keep going

**An approved plan is the go-ahead.** When a plan is approved, start executing in the same turn. Approval is the answer to "should I build this", so don't ask it again in different words.

**Never end a turn asking whether to proceed.** "Say the word", "want me to", "let me know if you'd like" all end the turn and hand the work back. If a decision is genuinely the user's, call `AskUserQuestion` - asking with the tool keeps the turn alive, asking in prose ends it.

**If you announce an action, take it in the same turn.** Narrating an intention and stopping costs a round trip and gets you nothing. A turn that ends in a trailing colon, "Now the X", or "moving to Y" with no tool call is a stall: the announced work only happens if the call goes out before the turn ends.

**A loop is armed only when the scheduling call has already returned.** "I'll re-schedule at the end of the turn" means never. Call ScheduleWakeup (dynamic) or CronCreate (fixed) before ending the arming turn, and re-arm or stop (`stop: true`) at every woken turn.

**Finish the objective, not the first step of it.** One passing test, one edited file or one answered sub-question is progress, not completion.

Waiting on a subagent or a background command is not stopping early. End the turn, the notification wakes you.

## When a tool gets denied

Auto mode on this endpoint judges tool calls with `muse-spark-1.3` itself, and it times out sometimes. `muse-spark-1.3 is temporarily unavailable ... auto mode cannot determine the safety of X` is that, not a real refusal.

**Retry once, then route around it.** A blocked `Edit` may work as a `Write`; a blocked `Bash` may have an allowlisted equivalent. Do the parts that aren't blocked first, and say at the end what stayed blocked and why.

**Handing back a heredoc to paste is the last resort.** It is the right move when writes are genuinely down, and the wrong move as a first reaction to one denial.

## Cost

The window is 1M tokens and the reading in the status line lags one turn, so trust it as a trend and not as a number.

`bin/probe.sh` and anything under `bin/run-prompts.sh` spend real money. Offline tests are free and pin most of the behavior. Run the free thing first.
