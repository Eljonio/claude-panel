# claude-panel

[![tests](https://github.com/Eljonio/claude-panel/actions/workflows/test.yml/badge.svg)](https://github.com/Eljonio/claude-panel/actions/workflows/test.yml)

A desktop side panel for people who run several Claude Code sessions at once. It shows every open session with its
subagents, warns about conflicts between sessions, and runs a separate, locked-down Claude Code "auditor" session that
reviews what the others are doing and keeps a log of state-changing facts.

The panel only reads Claude Code's local files, so watching sessions costs no tokens. Only the optional auditor uses
the API.

## Where to look first

| to check | open |
|---|---|
| session discovery from Claude Code's local files, the PID-reuse check, incremental transcript reading | [`claude_panel/collector.py`](claude_panel/collector.py), [`claude_panel/tail.py`](claude_panel/tail.py) |
| the auditor's sandbox: a `PreToolUse` guard with a whitelist of read-only commands | [`claude_panel/guard.py`](claude_panel/guard.py) |
| the auditor's instructions (in Russian) | [`auditor/prompt.md`](auditor/prompt.md) |
| what each session is told about the other sessions on every prompt | [`claude_panel/hookprompt.py`](claude_panel/hookprompt.py) |
| secret scrubbing and the session journal | [`claude_panel/journal.py`](claude_panel/journal.py) |
| what is tested | [`tests/`](tests) — 34 tests, `python3 -m unittest discover -s tests`; the 5 GTK tests skip without GTK/VTE |

**Provenance.** Exported on 2026-11-20 from a private working copy. Session data, journals and names were removed, so the public history starts with the export.

## The problem

With five or more terminals running Claude Code, each with its own subagents, it gets hard to keep track:

- which session is waiting for an answer, and which subagent finished twenty minutes ago without anyone reading its
  report;
- two sessions editing the same repository at the same time;
- the same chat open in two terminals, so the answers get mixed up;
- risky steps (`git push`, deploys, `rm -rf`, POST requests to external APIs) taken without the user asking for them;
- results that never make it into the agent's long-term memory, because the session was closed or the machine
  rebooted.

## What it shows

A GTK3 window docked to the right edge of the screen. It stays on top, appears on all workspaces and is hidden from
the taskbar.

- **Sessions**: chat title, status (`busy`, `idle`, `waiting` with the reason, such as a permission prompt or a
  question, or a background shell), how long it has been in that status, the repository it is editing, the last
  prompt, Claude Code's away summary, and its subagents (running, silent, done, failed, killed), each with type, model
  and description. Clicking a card focuses that terminal window.
- **Warnings**: one chat open in two terminals; two sessions editing the same repository or file within 20 minutes;
  a subagent that finished more than 10 minutes ago with no reply in the chat since.
- **Audit**: open remarks from the auditor, each with a level (`high`, `warn`, `info`).
- **Auditor terminal**: an embedded VTE terminal running the auditor session. You can type to it directly.
- The `›` button collapses the panel into a 24 px strip with one status dot per session and a warning count.
  The panel can also be hidden completely while it keeps running.

## How it works

```
Claude Code files (read-only)
  ~/.claude/sessions/<pid>.json          ─┐
  ~/.claude/projects/<slug>/<sid>.jsonl  ─┼─► collector ─► board.json ─┬─► GTK panel
  …/<sid>/subagents/agent-*.jsonl        ─┘                            └─► hook-prompt (UserPromptSubmit)

GTK panel ── ticks ──► auditor (sandboxed claude) ──► audit.json ──► panel ──► journal_auto.md
SessionStart / SessionEnd hooks ──► journal_sessions.md, live markers, review queue for the auditor
```

- **Collector** (`collector.py`, `tail.py`): finds live sessions from `~/.claude/sessions/<pid>.json`. It checks that
  the process exists and that `procStart` matches `/proc/<pid>/stat`, so a reused PID is not mistaken for a session.
  On first sight it reads the last 512 KB of the transcript, and after that only new bytes. If the title isn't in the
  tail, it finds it with one `mmap.rfind` pass over the whole file. A subagent counts as done on its final
  `end_turn` reply or a `SubagentHandback`; failed or killed status comes from the parent's `<task-notification>`.
- **Hook** (`hookprompt.py`): on every prompt it prints up to 10 lines about the *other* sessions, the auditor's
  remarks about this session (each shown once and only if less than an hour old), and up to 3 fresh facts that
  haven't been moved into memory yet. It takes about 40 ms and stays silent, with exit code 0, on any error.
- **Session journal** (`journal.py`): one line per session in `journal_sessions.md`. Each line has the end time,
  `closed` or `dropped`, the title, the full session id for `claude --resume`, the folder, what was left open (an
  unanswered question, an interrupted request, subagents that never reported back), and the last prompt with secrets
  removed. It uses no LLM and no network. "Dropped" means one of two things: `SessionEnd` fired with a non-normal
  reason, or the process died without `SessionEnd` at all. A live-session marker in `~/.local/state/claude-panel/live/`
  survives reboots, so such sessions are found later. Dropped sessions are queued for the auditor to review.
- **Bot sessions** (`claude -p` or the Agent SDK called from scripts) are recognized by Claude Code's `entrypoint`
  (`cli`, `claude-vscode` and `claude-desktop` count as human) and left out of the journal and the audit. A working
  directory under `/tmp` is used as a fallback signal.
- **Auditor** (`auditor.py`, `auditor/prompt.md`): a separate `claude --name auditor` running in the embedded
  terminal. The panel sends it a one-line "tick" every 15 minutes (skipped when nothing has changed) and on events
  (a subagent finished, a session closed, a duplicate chat, a repo conflict), batching events for 20 s. It sends a
  tick only when the auditor is idle and you haven't typed into its terminal for 45 s. A tick points to a
  `delta.txt` with new messages since the last tick (8 KB at most, secrets removed) and to digests of dropped
  sessions. The auditor rewrites `audit.json` with its open remarks and new facts. The panel appends those facts to
  `journal_auto.md` without duplicates. A fact still unchecked after 30 minutes goes back to the auditor, which checks
  whether it has reached the notes and raises a remark if it hasn't.
- **Auditor sandbox**: user settings are not loaded (`--setting-sources=`). The auditor is started with
  `--tools Read,Grep,Glob,Write,Edit,Bash`, `--strict-mcp-config` and `--permission-mode dontAsk`, plus explicit
  allow and deny rules. A `PreToolUse` guard (`guard.py`) runs before the permission rules. It allows writing only
  `audit.json`, allows only a whitelist of read-only shell commands (no command substitution, no redirects except to
  `/dev/null`, no `find -exec`, read-only `git` subcommands only), and blocks secret paths, network tools and MCP.
- **Secret scrubbing** (`journal.scrub`) runs before any text reaches the journal or the auditor. It removes API
  tokens, JWTs, private keys, `user:pass@` in URLs, `echo … | sudo -S`, passwords, one-time codes and card numbers.

## Requirements

- Linux with an **X11** session. Docking, keep-above and window switching (libwnck) don't work under Wayland.
- `gnome-terminal`, which is used to find a session's window by its title.
- Python 3 (developed on 3.12) with PyGObject, GTK 3, VTE 2.91 and libwnck 3. On Ubuntu:
  `sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-wnck-3.0`
- Claude Code CLI, by default at `~/.local/bin/claude` (`auditor.claude_bin`). Developed against Claude Code 2.1.x.

There are no pip dependencies.

## Install and run

```bash
git clone <this repo> claude-panel
ln -sf "$PWD/claude-panel/bin/claude-panel" ~/bin/claude-panel
claude-panel open        # start the panel (needs $DISPLAY)
claude-panel board       # text summary of all sessions, no GUI
```

Hooks in `~/.claude/settings.json`. `hook-start` raises the panel if it isn't running; the panel closes itself,
along with the auditor, 60 s after the last session ends. Inside the auditor's own session the hooks do nothing.

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "/path/to/claude-panel/bin/claude-panel hook-start" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "/path/to/claude-panel/bin/claude-panel hook-stop" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/path/to/claude-panel/bin/claude-panel hook-prompt" }] }]
  }
}
```

Optional: bind a desktop shortcut (for example a GNOME custom shortcut) to `claude-panel toggle-hidden`.

## Commands

```
claude-panel open | close | status
claude-panel collapse | expand | toggle
claude-panel hide | show | toggle-hidden     # remove the window entirely / bring it back; the panel keeps running
claude-panel auditor on|off|status           # turn the auditor on or off (same as the switch in the panel)
claude-panel auditor show|hide               # show or hide the auditor terminal; the auditor keeps working
claude-panel restart-auditor | tick          # restart the auditor / send it a check now
claude-panel board                           # text summary without the GUI
claude-panel journal-reap                    # find sessions that died without SessionEnd
claude-panel journal-backfill [YYYY-MM-DD]   # one-off: add past sessions to the journal
claude-panel journal-purge-bots [--archive] [--dry]
claude-panel hook-start | hook-stop | hook-prompt | guard   # used by Claude Code hooks
```

When the auditor is off, its process is stopped, no ticks are sent, no tokens are spent, and no remarks are shown.
The session overview, the hook and the session journal keep working.

## Configuration

`~/.config/claude-panel/config.json` is merged over the defaults in `claude_panel/config.py`, section by section:

```json
{"panel":   {"width": 480, "refresh_s": 1.5, "terminal_fraction": 0.42, "autoclose_grace_s": 60},
 "board":   {"agents_recent_h": 2, "repo_window_min": 20, "unaccepted_min": 10, "conflict_ignore": []},
 "auditor": {"enabled": true, "window_visible": true, "model": null, "effort": null, "tick_min": 15,
             "event_debounce_s": 20, "cwd": "~", "risky_extra": []},
 "hook":    {"max_lines": 10, "max_chars": 1000, "facts_max": 3, "audit_max_age_min": 60},
 "journal": {"enabled": true, "max_kb": 40, "prompt_chars": 200, "review_kb": 150, "review_per_tick": 3,
             "unsynced_min": 30, "reap_s": 60}}
```

`auditor.risky_extra` adds your own regular expressions for "risky step" detection on top of the built-in ones.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `VAULT_DIR` | `~/claude-vault` | notes folder for `project/journal_sessions.md`, `project/journal_auto.md` and the archive |
| `CLAUDE_PANEL_USER` | `Пользователь` | label for the human's messages in digests sent to the auditor |
| `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME` | standard | runtime files, live markers and queue, config |

The journal files are capped at 40 KB each. Older lines move to monthly archive files.
The notes folder can be the same one served by the companion project vault-app.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests need no network and keep everything in a temp directory. They cover bot-session filtering, the journal and
its hooks, the auditor switch, and hiding the panel. The GTK tests skip themselves when GTK/VTE are missing.

## Limitations

- Linux with X11 and GNOME Terminal only.
- Jumping to a window works by title: Claude Code sets the terminal title to `✳ <chat title>`, and GNOME Terminal
  doesn't expose which window a tab belongs to. A session in an inactive tab can't be found. When the same chat is
  open in two windows, the choice is approximate.
- It reads Claude Code's internal, undocumented files (`~/.claude/sessions`, transcript JSONL). A Claude Code update
  can change their format.
- The UI, the hook output, the journal and the auditor prompt are in Russian.
- Auditor sessions are saved like normal chats and show up in `/resume` as "auditor".

## License

MIT, see [LICENSE](LICENSE).
