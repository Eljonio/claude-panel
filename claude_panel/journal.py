"""Журнал сессий и летопись фактов — без ИИ и без сети.

journal_sessions.md — строка на сессию: когда закончилась, «закрыта» / «оборвалась», тема (как в пульте), последний
    запрос пользователя (≤200 знаков, секреты вычищены), что осталось незакрытым. Пишут:
      * хук SessionEnd (`claude-panel hook-stop`) — «закрыта»;
      * хук SessionStart и пульт — «оборвалась»: маркер сессии есть, а процесс умер без SessionEnd (перезагрузка, kill,
        закрытое окно). Маркеры живых сессий лежат в ~/.local/state/claude-panel/live/ — переживают перезагрузку.
    Оборвавшиеся ставятся в очередь (~/.local/state/claude-panel/review-queue/) — на ближайшем тике аудитор их разбирает.
journal_auto.md — летопись фактов, меняющих состояние (выкачено, подано, оплачено, решение пользователя, агент сдал,
    пользователь что-то сделал сам). Факты выписывает аудитор в audit.json["facts"], а дописывает сюда пульт: только в конец, без секретов,
    без повторов. «[ ]» — ещё не перенесено в стол агента / project_open_tasks; основная сессия переносит и ставит «[x]».
Оба файла ≤ max_kb: старые строки уезжают в архив по месяцам (journal.archive_dir).
"""
import fcntl
import glob
import hashlib
import json
import mmap
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import config as C
from .tail import iso_to_ts, mask_secrets

# ------------------------------------------------------------------ секреты
_EXTRA_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "***ключ***"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), "***"),          # JWT
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),                                                  # AWS
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{10,}"), "***"),                             # Stripe и т. п.
    (re.compile(r"\b(?:ya29|AIza)[A-Za-z0-9_\-]{20,}"), "***"),                                    # Google
    (re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@", re.I), r"\1***@"),                # user:pass@ в URL
    (re.compile(r"(echo\s+)(['\"]?)[^\s'\"|]+\2(\s*\|\s*sudo\s+-S)"), r"\1***\3"),                # echo 'пароль' | sudo -S
    (re.compile(r"(?i)\b(sshpass\s+-p\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)((?:код|code|otp|pin|пин)[\s:=—-]{1,3})\d{4,8}\b"), r"\1***"),              # коды подтверждения
    (re.compile(r"\b(?:\d[ -]?){12,15}(\d{4})\b"), r"****\1"),                                     # номер карты → последние 4
]
_PASS_RE = re.compile(r"(?i)(парол[ьяеи]\w*|password|passwd|pass|pwd|пасс\w*|secret|секрет\w*)(\s*[:=]\s*|\s+)"
                      r"([^\s,;«»\"'()]{4,})")
_LONG_TOKEN_RE = re.compile(r"\b(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{32,}\b")
_SAFE_WORDS = {"от", "для", "в", "на", "из", "не", "нет", "есть", "сейф", "сейфе", "сейфа", "файле", "заметке"}


def _pass_sub(m):
    word, sep, val = m.group(1), m.group(2), m.group(3)
    if val.lower() in _SAFE_WORDS:
        return m.group(0)
    looks_secret = (":" in sep or "=" in sep or re.search(r"\d", val) or re.search(r"[^\w]", val)
                    or (re.search(r"[a-z]", val) and re.search(r"[A-Z]", val)))
    return f"{word}{sep}***" if looks_secret else m.group(0)


def scrub(text):
    """Вычистить токены, пароли, ключи, коды, номера карт. Лучше лишнее закрыть, чем выпустить секрет в память."""
    s = mask_secrets(str(text or ""))
    for rx, rep in _EXTRA_SECRETS:
        s = rx.sub(rep, s)
    s = _PASS_RE.sub(_pass_sub, s)
    s = _LONG_TOKEN_RE.sub("***", s)
    return s


def one_line(text, n):
    s = " ".join(str(text or "").split())
    s = re.sub(r"</?(command-[a-z]+|local-command-[a-z]+|system-reminder)>", " ", s)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ разбор переписки
_SKIP_PREFIXES = (b'{"type":"api-request', b'{"type":"file-history', b'{"type":"mode"', b'{"type":"permission-mode"',
                  b'{"type":"atis-latch"', b'{"type":"queue-operation"')
_QUESTION_TAIL = re.compile(r"\?\s*[)»\"*_`]*\s*$")


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") == "text")
    return ""


def is_question(text):
    """Последний абзац ответа Claude — вопрос пользователю?"""
    t = str(text or "").strip()
    if not t:
        return False
    paras = [p for p in re.split(r"\n\s*\n", t) if p.strip()]
    last = paras[-1] if paras else t
    if _QUESTION_TAIL.search(last):
        return True
    # «Го?» / «Делаем?» в последней строке, после которой идёт подпись или список вариантов
    lines = [x for x in last.splitlines() if x.strip()]
    return any(_QUESTION_TAIL.search(x) for x in lines[-2:])


def scan_transcript(path, tail_kb=512, keep_msgs=0):
    """Быстрый разбор хвоста переписки: тема, последний запрос, кто сказал последним, висящий вопрос.
    keep_msgs > 0 — ещё и последние реплики (для выжимки аудитору)."""
    info = {"path": path, "ai_title": None, "custom_title": None, "agent_name": None, "last_prompt": None,
            "entrypoint": None, "cwd": None, "first_ts": None, "last_ts": None, "human": None, "reply": None,
            "tool_ts": None, "ask_pending": False, "away": None, "msgs": [], "size": 0}
    try:
        st = os.stat(path)
    except OSError:
        return None
    info["size"] = st.st_size
    start = max(0, st.st_size - tail_kb * 1024)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]  # первая строка неполная
    ask_ids = set()
    msgs = []
    for raw in lines:
        if not raw or raw.startswith(_SKIP_PREFIXES):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "ai-title":
            info["ai_title"] = d.get("aiTitle") or info["ai_title"]
            continue
        if t == "custom-title":
            info["custom_title"] = d.get("customTitle") or info["custom_title"]
            continue
        if t == "agent-name":
            info["agent_name"] = d.get("agentName") or info["agent_name"]
            continue
        if t == "last-prompt":
            info["last_prompt"] = d.get("lastPrompt") or info["last_prompt"]
            continue
        ts = iso_to_ts(d.get("timestamp"))
        if d.get("entrypoint"):
            info["entrypoint"] = d["entrypoint"]
        if d.get("cwd") and not info["cwd"]:
            info["cwd"] = d["cwd"]
        if ts:
            info["first_ts"] = info["first_ts"] or ts
            info["last_ts"] = ts if not info["last_ts"] or ts > info["last_ts"] else info["last_ts"]
        if d.get("isSidechain"):
            continue
        if t == "system" and d.get("subtype") == "away_summary":
            info["away"] = re.sub(r"\s*\(disable recaps in /config\)\s*$", "", str(d.get("content") or ""))
            continue
        if t == "attachment":
            att = d.get("attachment") or {}
            if att.get("type") == "queued_command" and (att.get("origin") or {}).get("kind") == "human":
                txt = str(att.get("prompt") or "")
                info["human"] = (ts, txt)
                if keep_msgs:
                    msgs.append((ts, C.USER_LABEL, txt))
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if t == "user":
            has_result = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
            if has_result:
                info["tool_ts"] = ts
                for x in content:
                    if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in ask_ids:
                        ask_ids.discard(x.get("tool_use_id"))
                continue
            if (d.get("origin") or {}).get("kind") == "human" and not d.get("isMeta"):
                txt = _text_of(content)
                info["human"] = (ts, txt)
                if keep_msgs:
                    msgs.append((ts, C.USER_LABEL, txt))
        elif t == "assistant" and isinstance(content, list):
            for x in content:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "text" and str(x.get("text", "")).strip():
                    info["reply"] = (ts, str(x["text"]))
                    if keep_msgs:
                        msgs.append((ts, "Claude", str(x["text"])))
                elif x.get("type") == "tool_use":
                    info["tool_ts"] = ts
                    if x.get("name") == "AskUserQuestion" and x.get("id"):
                        ask_ids.add(x["id"])
                    if keep_msgs and x.get("name") in ("Agent", "Task"):
                        inp = x.get("input") or {}
                        msgs.append((ts, "Claude", f"[запустил агента {inp.get('subagent_type', '')}: "
                                                   f"{inp.get('description', '')}]"))
    info["ask_pending"] = bool(ask_ids)
    if keep_msgs:
        info["msgs"] = msgs[-keep_msgs:]
    if start > 0 and not (info["ai_title"] or info["custom_title"]) or (start > 0 and not info["last_prompt"]):
        _rfind_meta(path, info)
    return info


def _rfind_meta(path, info):
    """Тема и последний запрос, если их нет в хвосте: один проход mmap.rfind (в C, мс на 15 МБ)."""
    want = []
    if not info["custom_title"]:
        want.append((b'{"type":"custom-title"', "custom_title", "customTitle"))
    if not info["ai_title"]:
        want.append((b'{"type":"ai-title"', "ai_title", "aiTitle"))
    if not info["last_prompt"]:
        want.append((b'{"type":"last-prompt"', "last_prompt", "lastPrompt"))
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for pat, key, field in want:
                i = mm.rfind(pat)
                if i < 0:
                    continue
                b = mm.find(b"\n", i)
                if b < 0:
                    b = len(mm)
                if b - i > 256 * 1024:
                    continue
                try:
                    info[key] = json.loads(mm[i:b]).get(field) or info[key]
                except ValueError:
                    pass
    except (OSError, ValueError):
        pass


def open_flags(info):
    """Что осталось незакрытым — по последним репликам."""
    flags = []
    human, reply, tool_ts = info.get("human"), info.get("reply"), info.get("tool_ts")
    h_ts = (human or (None, ""))[0] or 0
    r_ts = (reply or (None, ""))[0] or 0
    if info.get("ask_pending"):
        flags.append("вопрос ассистента без ответа (AskUserQuestion)")
    elif human and h_ts > r_ts:
        if tool_ts and tool_ts > h_ts:
            flags.append("работа по запросу пользователя прервана, ответа не было")
        else:
            flags.append("запрос пользователя без ответа")
    elif reply and is_question(reply[1]):
        flags.append("вопрос ассистента без ответа")
    elif tool_ts and r_ts and tool_ts > r_ts + 1:
        flags.append("прервана на ходу")
    return flags


def running_agents(transcript, last_ts, window_h=3, limit=12):
    """Агенты сессии, которые не сдали отчёт (по своим jsonl)."""
    sub = os.path.join(transcript[:-len(".jsonl")], "subagents")
    try:
        names = [n for n in os.listdir(sub) if n.startswith("agent-") and n.endswith(".jsonl")]
    except OSError:
        return []
    paths = []
    for n in names:
        p = os.path.join(sub, n)
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            continue
        if last_ts and mt < last_ts - window_h * 3600:
            continue
        paths.append((mt, p))
    out = []
    for _, p in sorted(paths, reverse=True)[:limit]:
        if agent_done(p):
            continue
        meta = {}
        try:
            with open(p[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        out.append(f"{meta.get('agentType') or '?'} «{one_line(meta.get('description'), 40)}»")
    return out


def agent_done(path, tail_kb=96):
    """Агент сдал отчёт: SubagentHandback с ответом или последний ответ без вызова инструментов — и после этого
    не было нового задания. Служебные task-notification (Claude Code шлёт их агенту и при закрытии сессии) —
    не задание."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_kb * 1024))
            data = f.read()
    except OSError:
        return False
    lines = data.split(b"\n")
    if size > tail_kb * 1024:
        lines = lines[1:]
    done, handback_ids = False, set()
    for raw in lines:
        if not raw or raw.startswith(_SKIP_PREFIXES):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        content = (d.get("message") or {}).get("content")
        if t == "assistant" and isinstance(content, list):
            tools = [x for x in content if isinstance(x, dict) and x.get("type") == "tool_use"]
            for x in tools:
                if x.get("name") == "SubagentHandback":
                    handback_ids.add(x.get("id"))
            if tools:
                done = False
            elif (d.get("message") or {}).get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal") \
                    and any(isinstance(x, dict) and x.get("type") == "text" for x in content):
                done = True
        elif t == "user":
            if isinstance(content, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content):
                if any(isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in handback_ids
                       and not x.get("is_error") for x in content):
                    done = True
                continue
            if (d.get("origin") or {}).get("kind") in ("task-notification",) or d.get("isMeta"):
                continue  # служебное уведомление, не новое задание
            done = False  # новое задание (первое или через SendMessage)
    return done


def is_auditor(info):
    return C.AUDITOR_NAME in (info.get("custom_title"), info.get("agent_name"))


def title_of(info, rec=None):
    rec = rec or {}
    name = rec.get("name") or ""
    if info.get("custom_title"):
        return info["custom_title"]
    if name and rec.get("nameSource") in ("user", "peer"):
        return name
    return info.get("ai_title") or info.get("agent_name") or name or ""


# ------------------------------------------------------------------ файлы журнала
@contextmanager
def _locked(name):
    os.makedirs(C.STATE_DIR, exist_ok=True)
    with open(os.path.join(C.STATE_DIR, name), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


SESSIONS_HEAD = """---
name: journal_sessions
description: "Журнал сессий Claude — строка на сессию: когда закончилась, закрыта или оборвалась, тема, последний запрос пользователя, что осталось незакрытым. Пишет пульт сам (хук SessionEnd + поиск оборвавшихся), без ИИ"
metadata:
  type: project
---

# Журнал сессий Claude

Пишет пульт ([[project_claude_panel]]) сам — руками не править. Строка на сессию, новые внизу; сессию открыли снова и
закрыли — её строка переезжает вниз. Больше {max_kb} КБ — старое уезжает в архив `journal_sessions-ГГГГ-ММ.md`.
**закрыта** — пользователь вышел сам (SessionEnd); **оборвалась** — процесс умер без SessionEnd (перезагрузка, kill, закрыли окно),
такие сессии аудитор разбирает и пишет незакрытое в [[journal_auto]]; ⏳ — что осталось незакрытым.
Вернуть сессию: `claude --resume <id>` в той же папке (папка указана, если не `~`).

<!-- строки ниже пишет пульт -->
"""

AUTO_HEAD = """---
name: journal_auto
description: "Летопись фактов, меняющих состояние дел (выкачено, подано, оплачено, решение пользователя, агент сдал, пользователь сделал сам). Факты выписывает аудитор пульта, дописывает пульт; [ ] — ещё не перенесено в стол агента / project_open_tasks"
metadata:
  type: project
---

# Летопись (авто)

Факты выписывает аудитор пульта ([[project_claude_panel]]) из реплик всех сессий и из оборвавшихся сессий
([[journal_sessions]]); пульт дописывает их сюда — только в конец, с датой, без секретов и повторов.
Это страховка, а не память: основная сессия переносит факт в стол агента / [[project_open_tasks]] и ставит `[x]`.
`[ ]` дольше {unsynced} мин — аудитор сверяет со столами и поднимает замечание. Больше {max_kb} КБ — старое уезжает
в архив `journal_auto-ГГГГ-ММ.md`.
Формат: `- [ ] ГГГГ-ММ-ДД ЧЧ:ММ · <сессия> «тема» · <вид> · <факт> · #id`

<!-- строки ниже пишет пульт -->
"""

_LINE_DATE = re.compile(r"^- (?:\[[ x]\] )?(\d{4}-\d\d-\d\d) ")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _split(text, head):
    """(шапка, строки записей). Шапка — всё до маркера; нет маркера — шапку берём свою."""
    marker = "<!-- строки ниже пишет пульт -->"
    if text is None:
        return head, []
    i = text.find(marker)
    if i < 0:
        return head, [x for x in text.splitlines() if x.startswith("- ")]
    j = i + len(marker)
    body = text[j:].lstrip("\n")
    return text[:j] + "\n", [x for x in body.splitlines() if x.strip()]


def _write(path, head, lines, cfg, archive_name):
    """Записать, а если больше max_kb — старые строки (по дате) в архив по месяцам."""
    limit = cfg["journal"]["max_kb"] * 1024
    body = "\n".join(lines) + ("\n" if lines else "")
    if len((head + body).encode()) > limit:
        target = int(limit * 0.75)
        moved = []
        while lines and len((head + "\n".join(lines) + "\n").encode()) > target:
            moved.append(lines.pop(0))
        _archive(cfg, archive_name, moved)
        body = "\n".join(lines) + ("\n" if lines else "")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    C.write_atomic(path, head + body, mode=0o664)


def _archive(cfg, name, lines):
    if not lines:
        return
    adir = C.jpath(cfg, "archive_dir")
    os.makedirs(adir, exist_ok=True)
    by_month = {}
    for ln in lines:
        m = _LINE_DATE.match(ln)
        by_month.setdefault(m.group(1)[:7] if m else datetime.now().strftime("%Y-%m"), []).append(ln)
    for month, lns in by_month.items():
        p = os.path.join(adir, f"{name}-{month}.md")
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write(f"# {name} — архив {month}\n\nСтроки, уехавшие из {name}.md по размеру.\n\n")
            f.write("\n".join(lns) + "\n")


def short_cwd(cwd):
    if not cwd or cwd == C.HOME:
        return ""
    return "~" + cwd[len(C.HOME):] if cwd.startswith(C.HOME + "/") else cwd


def session_line(cfg, sid, info, status, when, rec=None, agents=None, flags=None):
    pc = cfg["journal"]["prompt_chars"]
    title = one_line(scrub(title_of(info, rec)), 70) or sid[:8]
    parts = [f"- {datetime.fromtimestamp(when):%Y-%m-%d %H:%M}", status, f"«{title}»", f"`{sid}`"]
    cwd = short_cwd((rec or {}).get("cwd") or info.get("cwd"))
    if cwd:
        parts.append(cwd)
    for fl in (flags if flags is not None else open_flags(info)):
        parts.append(f"⏳ {fl}")
    if agents:
        parts.append(f"⏳ агенты не сдали отчёт: {', '.join(agents[:4])}" + (f" +{len(agents) - 4}" if len(agents) > 4 else ""))
    prompt = info.get("last_prompt") or (info.get("human") or (None, ""))[1]
    if prompt:
        parts.append("» " + one_line(scrub(prompt), pc))
    return " · ".join(p.replace("\n", " ") for p in parts)


def write_session(cfg, sid, line):
    """Строка на сессию: старая строка этой сессии убирается, новая — внизу."""
    path = C.jpath(cfg, "sessions")
    with _locked("journal.lock"):
        head, lines = _split(_read(path), SESSIONS_HEAD.replace("{max_kb}", str(cfg["journal"]["max_kb"])))
        lines = [x for x in lines if f"`{sid}`" not in x]
        lines.append(line)
        lines.sort(key=lambda x: x[2:18] if _LINE_DATE.match(x) else "")  # по времени конца, новые внизу
        _write(path, head, lines, cfg, "journal_sessions")


def journal_has(cfg, sid):
    return f"`{sid}`" in (_read(C.jpath(cfg, "sessions")) or "")


# ------------------------------------------------------------------ процессы и маркеры
def proc_start(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode(errors="replace")
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def claude_pid():
    """pid процесса claude, запустившего хук: $CLAUDE_PID или вверх по родителям до ~/.claude/sessions/<pid>.json."""
    env = os.environ.get("CLAUDE_PID")
    if env and env.isdigit():
        return int(env)
    pid = os.getppid()
    for _ in range(6):
        if os.path.exists(os.path.join(C.SESSIONS_DIR, f"{pid}.json")):
            return pid
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode(errors="replace")
            pid = int(raw[raw.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if pid <= 1:
            return None
    return None


def session_record(pid):
    if not pid:
        return {}
    try:
        with open(os.path.join(C.SESSIONS_DIR, f"{pid}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_interactive(pid, rec):
    """Интерактивный терминал человека, а не `claude -p` из скрипта (боты, пайплайны и т. п.)."""
    if rec.get("kind"):
        return rec["kind"] == "interactive"
    if rec.get("entrypoint"):
        return rec["entrypoint"] == "cli"
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
        return not any(a in (b"-p", b"--print") for a in args)
    except OSError:
        return True


# Люди: терминал («cli»), VS Code, приложение Claude. Всё прочее — скрипты: sdk-cli (`claude -p`), sdk-ts / sdk-py
# (Agent SDK), mcp, bench, github-action…
HUMAN_ENTRYPOINTS = frozenset({"cli", "claude-vscode", "claude-desktop"})


def _is_tmp(path):
    p = str(path or "")
    return p == "/tmp" or p.startswith("/tmp/")


def is_bot_session(rec=None, info=None, env=None, cwd=None):
    """Сессия из скрипта (боты и стенды: `claude -p` в /tmp, сотни в час), а не терминал человека.
    Такие не пишутся в журнал, не встают в очередь аудитору и не считаются событием.
    Решает entrypoint из первого источника, где он есть: env (CLAUDE_CODE_ENTRYPOINT в окружении хука) → rec
    (~/.claude/sessions/<pid>.json или карточка сессии пульта) → info (строки переписки). Claude Code ставит его сам
    при старте: `-p` → sdk-cli, даже если унаследовал cli от терминала; Agent SDK → sdk-ts/sdk-py. По kind их не
    отличить: у `claude -p` (2.1.309) он тоже «interactive». Папка /tmp — запасной признак, только когда entrypoint
    неизвестен (старая карточка board.json без поля, переписка без строк). env передавать только из хука: окружение
    самого пульта унаследовано от того, кто его запустил."""
    for src, key in ((env, "CLAUDE_CODE_ENTRYPOINT"), (rec, "entrypoint"), (info, "entrypoint")):
        ep = str((src or {}).get(key) or "").strip()
        if ep:
            return ep not in HUMAN_ENTRYPOINTS
    return any(_is_tmp(c) for c in (cwd, (rec or {}).get("cwd"), (info or {}).get("cwd")))


def marker_path(sid):
    return os.path.join(C.LIVE_DIR, f"{sid}.json")


def mark_live(sid, pid, start, cwd, transcript, title=""):
    if not sid or not pid:
        return
    os.makedirs(C.LIVE_DIR, exist_ok=True)
    data = {"sid": sid, "pid": pid, "procStart": start, "cwd": cwd, "transcript": transcript,
            "title": title, "marked": time.time()}
    C.write_atomic(marker_path(sid), json.dumps(data, ensure_ascii=False))


def read_marker(sid):
    try:
        with open(marker_path(sid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def unmark(sid):
    try:
        os.unlink(marker_path(sid))
    except OSError:
        pass


def _ended_path(sid):
    return os.path.join(C.STATE_DIR, "ended", f"{sid}.json")


def mark_ended(sid, pid, reason=""):
    """Отметка «SessionEnd отработал для этого процесса» — чтобы пульт не вернул маркер и не записал «оборвалась»."""
    os.makedirs(os.path.dirname(_ended_path(sid)), exist_ok=True)
    C.write_atomic(_ended_path(sid), json.dumps({"pid": pid, "ts": time.time(), "reason": reason}))


def ended_by_hook(sid, pid):
    try:
        with open(_ended_path(sid), encoding="utf-8") as f:
            return json.load(f).get("pid") == pid
    except (OSError, ValueError):
        return False


def _clean_ended(max_age_s=86400):
    d = os.path.join(C.STATE_DIR, "ended")
    try:
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if time.time() - os.path.getmtime(p) > max_age_s:
                os.unlink(p)
    except OSError:
        pass


def marker_alive(m):
    pid = m.get("pid")
    if not pid:
        return False
    st = proc_start(pid)
    return st is not None and (not m.get("procStart") or str(m["procStart"]) == st)


def find_transcript(sid, cwd=None, hint=None):
    if hint and os.path.exists(hint):
        return hint
    if cwd:
        p = os.path.join(C.PROJECTS_DIR, C.slug_for_cwd(cwd), sid + ".jsonl")
        if os.path.exists(p):
            return p
    found = glob.glob(os.path.join(C.PROJECTS_DIR, "*", sid + ".jsonl"))
    return found[0] if found else None


def enqueue_review(sid, title, transcript, ended, reason):
    os.makedirs(C.QUEUE_DIR, exist_ok=True)
    C.write_atomic(os.path.join(C.QUEUE_DIR, f"{sid}.json"),
                   json.dumps({"sid": sid, "title": title, "transcript": transcript, "ended": ended,
                               "reason": reason, "queued": time.time()}, ensure_ascii=False))


def record_orphan(cfg, m, reason="процесс умер без SessionEnd"):
    """Маркер есть, процесса нет → «оборвалась» в журнал + в очередь разбора аудитору."""
    sid = m["sid"]
    tp = find_transcript(sid, m.get("cwd"), m.get("transcript"))
    info = scan_transcript(tp, cfg["journal"]["tail_kb"]) if tp else None
    if info is None or is_auditor(info) or is_bot_session(info=info, cwd=m.get("cwd")):
        unmark(sid)  # бот-сессия (`claude -p` из скрипта): маркер мог поставить пульт старой версии — не обрыв
        return None
    if not (info.get("human") or info.get("last_prompt") or info.get("ai_title")):
        unmark(sid)  # открыли и сразу закрыли — писать нечего
        return None
    rec = {"cwd": m.get("cwd"), "name": m.get("title") or "", "nameSource": "user" if m.get("title") else None}
    when = info.get("last_ts") or os.path.getmtime(tp)
    agents = running_agents(tp, info.get("last_ts"))
    line = session_line(cfg, sid, info, "оборвалась", when, rec, agents)
    write_session(cfg, sid, line)
    enqueue_review(sid, title_of(info, rec), tp, when, reason)
    unmark(sid)
    return line


def reap(cfg, skip_sids=()):
    """Найти сессии, умершие без SessionEnd (перезагрузка, kill, закрытое окно). Вернёт строки журнала."""
    out = []
    _clean_ended()
    try:
        names = os.listdir(C.LIVE_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        sid = n[:-5]
        m = read_marker(sid)
        if not m:
            unmark(sid)
            continue
        if sid in skip_sids or marker_alive(m):
            continue
        if ended_by_hook(sid, m.get("pid")):
            unmark(sid)  # закрыта штатно, маркер вернули в последнюю секунду — не обрыв
            continue
        try:
            line = record_orphan(cfg, m)
            if line:
                out.append(line)
        except Exception:  # одна битая сессия не должна ронять остальные
            unmark(sid)
    return out


# ------------------------------------------------------------------ хуки
def on_session_start(data):
    """SessionStart: если у этой сессии висит маркер умершего процесса (возобновили после перезагрузки) —
    сначала записать, что она обрывалась; потом поставить маркер живой сессии."""
    cfg = C.load_config()
    if not cfg["journal"].get("enabled", True):
        return
    sid = data.get("session_id") or ""
    if not sid:
        return
    pid = claude_pid()
    rec = session_record(pid)
    if is_bot_session(rec, env=os.environ, cwd=data.get("cwd")):
        return  # `claude -p` из скрипта — ни маркера, ни поиска оборвавшихся (сотни раз в час)
    if pid and not is_interactive(pid, rec):
        return
    old = read_marker(sid)
    if old and old.get("pid") != pid and not marker_alive(old):
        record_orphan(cfg, old, "возобновлена после обрыва")
    reap(cfg, skip_sids={sid})  # заодно — другие оборвавшиеся (после перезагрузки пульт мог ещё не подняться)
    mark_live(sid, pid, proc_start(pid) if pid else None, data.get("cwd") or rec.get("cwd"),
              data.get("transcript_path"), rec.get("name") if rec.get("nameSource") in ("user", "peer") else "")


NORMAL_END = {"prompt_input_exit", "clear", "logout", "resume", "bypass_permissions_disabled"}


def on_session_end(data):
    """SessionEnd: строка «закрыта» в журнал, маркер снять. Быстро, без сети и ИИ."""
    cfg = C.load_config()
    if not cfg["journal"].get("enabled", True):
        return
    sid = data.get("session_id") or ""
    if not sid:
        return
    pid = claude_pid()
    rec = session_record(pid)
    marker = read_marker(sid)
    if is_bot_session(rec, env=os.environ, cwd=data.get("cwd")):
        unmark(sid)  # маркер бот-сессии мог поставить пульт старой версии
        return
    if not marker and pid and not is_interactive(pid, rec):
        return
    tp = find_transcript(sid, data.get("cwd") or rec.get("cwd"), data.get("transcript_path"))
    info = scan_transcript(tp, cfg["journal"]["tail_kb"]) if tp else None
    unmark(sid)
    if pid:
        mark_ended(sid, pid, data.get("reason") or "")
    if info is None or is_auditor(info) or is_bot_session(rec, info, env=os.environ, cwd=data.get("cwd")):
        return
    if not (info.get("human") or info.get("last_prompt") or info.get("ai_title")):
        return
    reason = data.get("reason") or ""
    normal = reason in NORMAL_END
    status = ("закрыта" + {"clear": " (/clear)", "logout": " (logout)", "resume": " (/resume)"}.get(reason, "")
              if normal else f"оборвалась (окно закрыто или сигнал, SessionEnd: {reason or '?'})")
    rec = dict(rec)
    rec.setdefault("cwd", data.get("cwd"))
    if marker and marker.get("title") and not rec.get("name"):
        rec.update(name=marker["title"], nameSource="user")
    agents = running_agents(tp, info.get("last_ts"))
    write_session(cfg, sid, session_line(cfg, sid, info, status, time.time(), rec, agents))
    if not normal:  # пользователь сам не выходил (упал X, закрыли окно, kill -HUP) — аудитор разберёт хвост
        enqueue_review(sid, title_of(info, rec), tp, info.get("last_ts") or time.time(), f"SessionEnd: {reason or '?'}")


# ------------------------------------------------------------------ выжимка оборвавшейся сессии для аудитора
def review_digest(cfg, q):
    """Короткая выжимка хвоста (≤ review_kb) оборвавшейся сессии: реплики пользователя и Claude, без вывода инструментов.
    Аудитору читать её, а не сырой jsonl — в разы меньше токенов."""
    tp = q.get("transcript")
    info = scan_transcript(tp, cfg["journal"]["review_kb"], keep_msgs=40) if tp else None
    if not info:
        return None
    lines = [f"# Оборвавшаяся сессия «{one_line(scrub(q.get('title') or ''), 80)}» [{q['sid'][:8]}]",
             f"Конец: {datetime.fromtimestamp(q.get('ended') or time.time()):%Y-%m-%d %H:%M} · {q.get('reason', '')}",
             f"Переписка: {tp} (хвост {cfg['journal']['review_kb']} КБ разобран ниже)"]
    flags = open_flags(info)
    if flags:
        lines.append("Незакрытое по последним репликам: " + "; ".join(flags))
    agents = running_agents(tp, info.get("last_ts"))
    if agents:
        lines.append("Агенты не сдали отчёт: " + ", ".join(agents))
    if info.get("away"):
        lines.append("Сводка Claude Code: " + one_line(scrub(info["away"]), 400))
    lines.append("")
    body = []
    for ts, who, txt in info["msgs"]:
        n = 300 if who == C.USER_LABEL else 500
        body.append(f"{datetime.fromtimestamp(ts or 0):%H:%M} {who}: {one_line(scrub(txt), n)}")
    limit = 6000
    while body and len("\n".join(lines + body)) > limit:
        body.pop(0)  # не влезает — оставляем самое свежее
    return "\n".join(lines + body) + "\n"


def take_reviews(cfg, n):
    """До n сессий из очереди: выжимка в RUN_DIR/review/<sid8>.txt, из очереди убрать. [(title, sid8, path)]"""
    try:
        names = sorted(os.listdir(C.QUEUE_DIR))
    except OSError:
        return []
    items = []
    for nm in names:
        p = os.path.join(C.QUEUE_DIR, nm)
        try:
            with open(p, encoding="utf-8") as f:
                items.append((json.load(f), p))
        except (OSError, ValueError):
            try:
                os.unlink(p)
            except OSError:
                pass
    items.sort(key=lambda x: x[0].get("ended") or 0)
    out = []
    os.makedirs(C.REVIEW_DIR, mode=0o700, exist_ok=True)
    for q, p in items[:n]:
        try:
            text = review_digest(cfg, q)
        except Exception:
            text = None
        if text:
            dst = os.path.join(C.REVIEW_DIR, f"{q['sid'][:8]}.txt")
            C.write_atomic(dst, text)
            out.append((q.get("title") or q["sid"][:8], q["sid"][:8], dst))
        try:
            os.unlink(p)
        except OSError:
            pass
    return out


def queue_size():
    try:
        return sum(1 for n in os.listdir(C.QUEUE_DIR) if n.endswith(".json"))
    except OSError:
        return 0


# ------------------------------------------------------------------ летопись
FACT_KINDS = ("выкачено", "подано", "оплачено", "решение", "агент", "сам", "незакрыто", "другое")
_AUTO_RE = re.compile(r"^- \[( |x)\] (\d{4}-\d\d-\d\d \d\d:\d\d) · (.*) · #([0-9a-f]{6})\s*$")


def fact_id(session, text):
    norm = re.sub(r"[^\w]+", " ", str(text).lower()).strip()
    return hashlib.sha1(f"{str(session)[:8]}|{norm}".encode()).hexdigest()[:6]


def parse_auto(text):
    out = []
    for ln in (text or "").splitlines():
        m = _AUTO_RE.match(ln)
        if m:
            out.append({"done": m.group(1) == "x", "when": m.group(2), "body": m.group(3), "id": m.group(4),
                        "line": ln})
    return out


def _fact_time(f, now):
    t = str(f.get("time") or "").strip()
    m = re.match(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d)", t)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2}):(\d\d)$", t)
    nd = datetime.fromtimestamp(now)
    if m:
        dt = nd.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)) % 60, second=0, microsecond=0)
        if dt > nd + timedelta(minutes=5):
            dt -= timedelta(days=1)  # «23:50», записанное после полуночи
        return dt
    return nd.replace(second=0, microsecond=0)


def _seen_ids():
    try:
        with open(os.path.join(C.STATE_DIR, "facts-seen.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_seen(ids):
    os.makedirs(C.STATE_DIR, exist_ok=True)
    C.write_atomic(os.path.join(C.STATE_DIR, "facts-seen.json"), json.dumps(ids[-3000:]))


def append_facts(cfg, facts, titles=None, now=None):
    """Дописать факты аудитора в летопись: только в конец, без повторов (по #id), секреты вычищены."""
    if not facts or not isinstance(facts, list):
        return []
    now = now or time.time()
    titles = titles or {}
    path = C.jpath(cfg, "auto")
    added = []
    with _locked("journal-auto.lock"):
        head, lines = _split(_read(path), AUTO_HEAD.replace("{max_kb}", str(cfg["journal"]["max_kb"]))
                             .replace("{unsynced}", str(cfg["journal"]["unsynced_min"])))
        have = {x["id"] for x in parse_auto("\n".join(lines))}
        seen = _seen_ids()
        seen_set = set(seen)
        for f in facts[:20]:
            if not isinstance(f, dict):
                continue
            text = one_line(scrub(f.get("text") or ""), 240)
            if len(text) < 4:
                continue
            sess = re.sub(r"[^0-9a-f]", "", str(f.get("session") or "").lower())[:8]
            fid = fact_id(sess, text)
            if fid in have or fid in seen_set:
                continue
            kind = str(f.get("kind") or "другое").strip().lower()
            kind = kind if kind in FACT_KINDS else "другое"
            title = one_line(scrub(f.get("title") or titles.get(sess) or ""), 40)
            who = (sess or "общее") + (f" «{title}»" if title else "")
            ln = f"- [ ] {_fact_time(f, now):%Y-%m-%d %H:%M} · {who} · {kind} · {text} · #{fid}"
            lines.append(ln)
            have.add(fid)
            seen.append(fid)
            seen_set.add(fid)
            added.append(ln)
        if added:
            _write(path, head, lines, cfg, "journal_auto")
            _save_seen(seen)
    return added


def mark_carried(cfg, ids):
    """[ ] → [x] у фактов, которые аудитор нашёл на столах / в open_tasks."""
    ids = {str(i).lstrip("#").lower()[:6] for i in ids or [] if i}
    if not ids:
        return 0
    path = C.jpath(cfg, "auto")
    n = 0
    with _locked("journal-auto.lock"):
        text = _read(path)
        if not text:
            return 0
        out = []
        for ln in text.splitlines():
            m = _AUTO_RE.match(ln)
            if m and m.group(1) == " " and m.group(4) in ids:
                ln = ln.replace("- [ ] ", "- [x] ", 1)
                n += 1
            out.append(ln)
        if n:
            C.write_atomic(path, "\n".join(out) + "\n", mode=0o664)
    return n


def unsynced(cfg, older_than_min=None, max_age_h=48, now=None):
    """Факты летописи без [x]: [(id, when_ts, body)], старые первыми."""
    now = now or time.time()
    out = []
    for x in parse_auto(_read(C.jpath(cfg, "auto"))):
        if x["done"]:
            continue
        try:
            ts = datetime.strptime(x["when"], "%Y-%m-%d %H:%M").timestamp()
        except ValueError:
            continue
        age = now - ts
        if age > max_age_h * 3600:
            continue
        if older_than_min is not None and age < older_than_min * 60:
            continue
        out.append((x["id"], ts, x["body"]))
    return out


def last_facts(cfg, n=8):
    return [x["line"] for x in parse_auto(_read(C.jpath(cfg, "auto")))][-n:]


# ------------------------------------------------------------------ разовое заполнение журнала прошлыми сессиями
def backfill(cfg, since_ts, live_sids=()):
    """Сессии с перепиской после since_ts, которых нет в журнале и которые не живы, — строкой «без отметки»
    (до журнала не знали, закрыли их или они оборвались). В очередь аудитору НЕ ставятся."""
    out = []
    for tp in glob.glob(os.path.join(C.PROJECTS_DIR, "*", "*.jsonl")):
        try:
            mt = os.path.getmtime(tp)
        except OSError:
            continue
        if mt < since_ts:
            continue
        sid = os.path.basename(tp)[:-len(".jsonl")]
        if sid in live_sids or journal_has(cfg, sid):
            continue
        info = scan_transcript(tp, cfg["journal"]["tail_kb"])
        if not info or is_bot_session(info=info):
            continue
        if not info.get("human") and not info.get("last_prompt"):
            continue
        if is_auditor(info):
            continue
        out.append((info.get("last_ts") or mt, sid, info, tp))
    lines = []
    for when, sid, info, tp in sorted(out):
        line = session_line(cfg, sid, info, "без отметки (до журнала)", when, None, running_agents(tp, info.get("last_ts")))
        write_session(cfg, sid, line)
        lines.append(line)
    return lines


# ------------------------------------------------------------------ чистка строк бот-сессий (шум старых версий)
_LINE_SID = re.compile(r"`([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`")


def _line_is_bot(ln, cache):
    """Строка журнала — бот-сессии? По переписке (entrypoint), а нет переписки — по папке в строке (/tmp…)."""
    m = _LINE_SID.search(ln)
    if not m:
        return False
    sid = m.group(1)
    parts = ln[m.end():].split(" · ")
    cwd = parts[1].strip() if len(parts) > 1 and parts[1].strip()[:1] in ("/", "~") else None
    if sid not in cache:
        tp = find_transcript(sid, os.path.expanduser(cwd) if cwd else None)
        info = scan_transcript(tp, 64) if tp else None
        cache[sid] = is_bot_session(info=info, cwd=cwd)
    return cache[sid]


def purge_bot_lines(cfg, archive=False, dry=False):
    """Убрать строки бот-сессий из журнала (и из архивов journal_sessions-*.md, если archive). Остальные строки и
    шапка — байт в байт. Под тем же замком, что и запись журнала. Вернёт {путь: [убранные строки]}."""
    paths = [C.jpath(cfg, "sessions")]
    if archive:
        paths += sorted(glob.glob(os.path.join(C.jpath(cfg, "archive_dir"), "journal_sessions-*.md")))
    out, cache = {}, {}
    with _locked("journal.lock"):
        for path in paths:
            text = _read(path)
            if not text:
                continue
            keep, gone = [], []
            for ln in text.split("\n"):
                (gone if ln.startswith("- ") and _line_is_bot(ln, cache) else keep).append(ln)
            if gone:
                out[path] = gone
                if not dry:
                    C.write_atomic(path, "\n".join(keep), mode=0o664)
    return out


# ------------------------------------------------------------------ замечания аудитора: возраст и «без повторов»
def audit_key(item):
    """Ключ замечания: id + отпечаток текста. Тот же id с новым текстом — уже другое замечание."""
    return f"{item.get('id') or ''}|{hashlib.sha1(str(item.get('text') or '').encode()).hexdigest()[:6]}"


def item_first_ts(item, updated=None, now=None):
    """Когда замечание появилось, по его полю time (ЧЧ:ММ) и дате обновления audit.json — если пульт его не застал."""
    now = now or time.time()
    m = re.match(r"^\s*(\d{1,2}):(\d\d)\s*$", str(item.get("time") or ""))
    if not m:
        return now
    try:
        base = datetime.fromisoformat(str(updated).replace("Z", "+00:00")) if updated else datetime.fromtimestamp(now)
        if base.tzinfo:
            base = base.astimezone().replace(tzinfo=None)
    except ValueError:
        base = datetime.fromtimestamp(now)
    dt = base.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)) % 60, second=0, microsecond=0)
    if dt > base + timedelta(minutes=5):
        dt -= timedelta(days=1)
    return min(now, dt.timestamp())


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
