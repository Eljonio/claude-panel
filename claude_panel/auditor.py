"""Аудитор: промпт, права, команда запуска и текст «тика», который панель вставляет в его ввод."""
import json
import os
import re
import sys
from datetime import datetime

from . import config as C
from .collector import cut, fmt_dur, short_path, status_word, AGENT_WORDS

PROMPT_TEMPLATE = os.path.join(C.APP_DIR, "auditor", "prompt.md")

# Встроенные признаки «рискованного шага» в действиях сессий; свои — в config.json: auditor.risky_extra
RISKY = [
    r"--go\b", r"armed[\"']?\s*[:=]\s*true", r"\bdeploy", r"git\s+push",
    r"graph\.(facebook|instagram)\.com", r"-X\s*(POST|DELETE|PUT)\b", r"systemctl\s+(restart|stop)",
    r"rm\s+-rf?\b", r"\.env\b", r"DROP\s+TABLE|TRUNCATE", r"\bpublish",
    r"git\s+commit", r"scp\s",
]
_RISKY_CACHE = {}


def risky_re(cfg=None):
    extra = tuple((cfg or C.load_config())["auditor"].get("risky_extra") or ())
    if extra not in _RISKY_CACHE:
        _RISKY_CACHE[extra] = re.compile("|".join(RISKY + list(extra)), re.I)
    return _RISKY_CACHE[extra]


def write_files():
    """Сгенерировать промпт и настройки аудитора в RUN_DIR (пути подставляются)."""
    C.ensure_run_dir()
    with open(PROMPT_TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    cfg = C.load_config()
    text = (text.replace("{BOARD_TXT}", C.BOARD_TXT).replace("{BOARD_JSON}", C.BOARD_JSON)
            .replace("{AUDIT_JSON}", C.AUDIT_JSON).replace("{DELTA_TXT}", C.DELTA_TXT)
            .replace("{REVIEW_DIR}", C.REVIEW_DIR).replace("{JOURNAL_AUTO}", C.jpath(cfg, "auto"))
            .replace("{JOURNAL_SESSIONS}", C.jpath(cfg, "sessions"))
            .replace("{VAULT_PROJECT}", os.path.dirname(C.jpath(cfg, "auto")))
            .replace("{UNSYNCED_MIN}", str(cfg["journal"]["unsynced_min"])))
    C.write_atomic(C.AUDITOR_PROMPT, text)
    guard_cmd = f"{sys.executable} {os.path.join(C.APP_DIR, 'bin', 'claude-panel')} guard"
    settings = {
        # Пользовательские настройки Claude Code аудитору НЕ грузятся (--setting-sources=): ни allow-правила
        # пользователя (curl, python3, ssh…), ни его хуки. Всё, что можно аудитору, — здесь.
        "permissions": {
            "defaultMode": "dontAsk",   # всё, что не разрешено явно и требует спроса, — сразу отказ
            "allow": [
                f"Edit(/{C.AUDIT_JSON})",   # единственный файл, который аудитор может писать
                f"Read(/{C.RUN_DIR}/**)",
                "Read(~/.claude/projects/**)",
                f"Read(/{os.path.expanduser(C.VAULT_DIR)}/**)",
            ],
            "deny": [
                "WebFetch", "WebSearch", "Agent", "Task", "NotebookEdit", "SendMessage", "Skill", "mcp__*",
                f"Edit(/{C.RUN_DIR}/board*)", f"Edit(/{C.RUN_DIR}/auditor-*)", f"Edit(/{C.RUN_DIR}/panel*)",
                "Read(~/**/.env)", "Read(~/**/.env.*)", "Read(~/.secrets/**)", "Read(~/.ssh/**)",
                "Read(~/.claude/.credentials.json)", "Read(~/.claude/settings.local.json)",
            ],
        },
        # страж срабатывает раньше правил: Write/Edit — только audit.json, Bash — только чтение
        "hooks": {
            "PreToolUse": [{
                "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash|Read|Grep|Glob|WebFetch|WebSearch|Agent|Task|SendMessage|Skill|mcp__.*",
                "hooks": [{"type": "command", "command": guard_cmd, "timeout": 10}],
            }],
        },
        "language": "russian",
        "theme": "dark",
    }
    C.write_atomic(C.AUDITOR_SETTINGS, json.dumps(settings, ensure_ascii=False, indent=1))


def argv(cfg):
    a = cfg["auditor"]
    cmd = [os.path.expanduser(a["claude_bin"]),
           "--name", C.AUDITOR_NAME,
           "--append-system-prompt-file", C.AUDITOR_PROMPT,
           "--settings", C.AUDITOR_SETTINGS,
           "--setting-sources=",          # без user/project/local настроек пользователя
           "--permission-mode", "dontAsk",
           "--tools", "Read,Grep,Glob,Write,Edit,Bash",
           "--strict-mcp-config",
           "--add-dir", C.RUN_DIR]
    if a.get("model"):
        cmd += ["--model", a["model"]]
    if a.get("effort"):
        cmd += ["--effort", a["effort"]]
    cmd += list(a.get("extra_args") or [])
    return cmd


def env():
    e = {k: v for k, v in os.environ.items()
         if not (k.startswith("CLAUDE") or k in ("NoDefaultCurrentDirectoryInExePath",))}
    e[C.AUDITOR_ENV] = "1"
    e["TERM"] = "xterm-256color"
    e["COLORTERM"] = "truecolor"
    return [f"{k}={v}" for k, v in e.items()]


def risky_steps(board, since, cfg=None):
    rx = risky_re(cfg)
    out = []
    for s in board["sessions"]:
        for act in s.get("recent_actions", []):
            if (act.get("ts") or 0) > since and rx.search(act.get("what", "")):
                out.append(f"«{cut(s['title'], 30)}» [{s['short']}]: {cut(act['what'], 150)}")
    return out[-6:]


def session_line(s, now):
    st = status_word(s)
    dur = fmt_dur(now - s["status_since"]) if s.get("status_since") else ""
    line = f"«{cut(s['title'], 45)}» [{s['short']} pid {s['pid']}] {st} {dur}"
    if s.get("repo"):
        line += f"; правит {short_path(s['repo'])}"
    if s.get("agents"):
        line += "; агенты: " + ", ".join(f"{a['type']} «{cut(a['desc'], 25)}» {AGENT_WORDS.get(a['state'], a['state'])}"
                                         for a in s["agents"][:3])
    if s.get("last_prompt"):
        line += f"; запрос: «{cut(s['last_prompt'], 60)}»"
    return line


def build_delta(board, since, now, closed=(), last_facts=(), limit_kb=8):
    """Новые реплики пользователя и Claude и отчёты агентов с прошлого тика — в DELTA_TXT, для летописи фактов.
    Вернёт число реплик (0 — файл не нужен). closed — сессии, закрывшиеся с прошлого тика (их уже нет в board)."""
    blocks, n = [], 0
    for s in list(board.get("sessions", [])) + list(closed):
        rows = []
        for m in s.get("msgs") or []:
            if (m.get("ts") or 0) > since:
                lim = 300 if m.get("who") == C.USER_LABEL else 600
                rows.append((m["ts"], f"{datetime.fromtimestamp(m['ts']):%H:%M} {m['who']}: {cut(m.get('text'), lim)}"))
        for a in s.get("agents") or []:
            if a.get("ended") and a["ended"] > since and a.get("state") in ("done", "failed", "killed", "blocked"):
                word = AGENT_WORDS.get(a["state"], a["state"])
                rows.append((a["ended"], f"{datetime.fromtimestamp(a['ended']):%H:%M} агент {a['type']} "
                                         f"«{cut(a.get('desc'), 50)}» — {word}: {cut(a.get('report') or '', 600)}"))
        if rows:
            rows.sort()
            n += len(rows)
            blocks.append((f"## «{cut(s.get('title'), 60)}» [{s.get('short') or str(s.get('sid', ''))[:8]}]",
                           [r for _, r in rows]))
    if not n:
        return 0
    head = [f"# Новые реплики с {datetime.fromtimestamp(since):%d.%m %H:%M} по {datetime.fromtimestamp(now):%H:%M}"]
    tail = ["", "# Последние строки летописи (эти факты уже записаны — не повторяй)"] + (list(last_facts) or ["(пусто)"])
    limit = limit_kb * 1024

    def render():
        out = list(head)
        for h, rows in blocks:
            out += ["", h] + rows
        return "\n".join(out + tail) + "\n"
    text = render()
    while len(text.encode()) > limit:
        # не влезает — выкидываем самые старые реплики самой длинной сессии
        h, rows = max(blocks, key=lambda b: len(b[1]))
        if len(rows) <= 1:
            break
        rows.pop(0)
        text = render()
    C.ensure_run_dir()
    C.write_atomic(C.DELTA_TXT, text)
    return n


def compose_tick(board, reason, events, since, now, extra=None):
    """Одна строка (без переводов строк — чтобы Enter не отправил сообщение по частям).
    extra: delta_n, reviews [(title, sid8, path)], unsynced [(id, body)], dropped [ids], delivered [ids]."""
    extra = extra or {}
    parts = [f"[ТИК АУДИТА {datetime.fromtimestamp(now):%H:%M} · {reason}]"]
    if events:
        parts.append("События: " + cut("; ".join(events), 600))
    if extra.get("delta_n"):
        parts.append(f"Новые реплики и отчёты агентов ({extra['delta_n']}): {C.DELTA_TXT} — прочитай и выпиши факты в facts.")
    if extra.get("reviews"):
        parts.append("Оборвавшиеся сессии к разбору (незакрытое → facts, вид «незакрыто»): " + "; ".join(
            f"«{cut(t, 40)}» [{sid}] {p}" for t, sid, p in extra["reviews"]))
    if extra.get("unsynced"):
        parts.append("Не перенесены в память дольше срока — сверь со столами и open_tasks: " + cut("; ".join(
            f"#{i} {cut(b, 90)}" for i, b in extra["unsynced"]), 700))
    if extra.get("dropped"):
        parts.append("Сняты пультом по давности (>1 ч без изменений), не возвращай без нового факта: "
                     + ", ".join(extra["dropped"][:10]))
    if extra.get("delivered"):
        parts.append("Уже показаны в своей сессии (после этого там был ответ) — сними, если нового нет: "
                     + ", ".join(extra["delivered"][:10]))
    risky = risky_steps(board, since)
    if risky:
        parts.append("Рискованные шаги с прошлой проверки: " + cut("; ".join(risky), 700))
    if board["warnings"]:
        parts.append("Предупреждения панели: " + cut("; ".join(w["text"] for w in board["warnings"]), 400))
    lines = [session_line(s, now) for s in board["sessions"]]
    parts.append("Сессии: " + (cut(" ¦ ".join(lines), 900) if lines else "нет"))
    parts.append(f"Подробно: {C.BOARD_JSON}. Перепиши {C.AUDIT_JSON} (items, facts, carried) и ответь одной строкой.")
    msg = " ".join(parts)
    return " ".join(msg.split())  # никаких \n и \r внутри
