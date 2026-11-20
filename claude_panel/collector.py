"""Сборщик состояния всех живых сессий Claude Code. Только читает файлы — токенов не тратит.

Источники:
  ~/.claude/sessions/<pid>.json                         — живые процессы и их статус
  ~/.claude/projects/<slug>/<sid>.jsonl                 — переписка (название, запросы, правки)
  ~/.claude/projects/<slug>/<sid>/subagents/agent-*.{meta.json,jsonl} — агенты сессии
"""
import glob
import hashlib
import json
import os
import time
from datetime import datetime

from . import config as C
from .tail import Tail, mask_secrets


def _scrub(text):
    from .journal import scrub  # вычистка секретов посильнее mask_secrets (пароли, коды, ключи)
    return scrub(text)

STATUS_WORDS = {
    "busy": "работает",
    "idle": "ждёт тебя",
    "waiting": "ждёт ответа",
    "shell": "фоновая команда",
}
WAITING_WORDS = {
    "permission prompt": "нужно разрешение",
    "input needed": "задал вопрос",
    "dialog open": "открыт диалог",
    "sandbox request": "просит сеть",
    "worker request": "запрос воркера",
}
AGENT_WORDS = {
    "running": "работает",
    "stale": "молчит",
    "done": "готово",
    "failed": "упал",
    "killed": "остановлен",
    "blocked": "ждёт ввода",
}
AGENT_PAST = {
    "done": "закончил",
    "failed": "упал",
    "killed": "остановлен",
    "blocked": "ждёт ввода",
}
FINISH_REASONS = {"end_turn", "stop_sequence", "max_tokens", "refusal"}


# ---------------------------------------------------------------- утилиты
def fmt_dur(sec):
    if sec is None:
        return "?"
    sec = max(0, int(sec))
    if sec < 60:
        return "<1 мин"
    m = sec // 60
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m} мин" if m else f"{h} ч"
    d, h = divmod(h, 24)
    return f"{d} д {h} ч"


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


def short_path(p):
    if not p:
        return ""
    return "~" + p[len(C.HOME):] if p == C.HOME or p.startswith(C.HOME + "/") else p


def cut(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def read_proc_stat(pid):
    """(ppid, starttime) из /proc/<pid>/stat или None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode(errors="replace")
    except OSError:
        return None
    rest = raw[raw.rfind(")") + 2:].split()
    # rest[0] = поле 3 (state); поле 4 = ppid → rest[1]; поле 22 = starttime → rest[19]
    try:
        return int(rest[1]), rest[19], int(rest[4])  # ppid, starttime, tty_nr
    except (IndexError, ValueError):
        return None


def proc_env_has(pid, key):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return (key.encode() + b"=1") in f.read().split(b"\0")
    except OSError:
        return False


def tty_name(tty_nr):
    major, minor = (tty_nr >> 8) & 0xFFF, (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:
        return f"pts/{(major - 136) * 256 + minor}"
    return None


# ---------------------------------------------------------------- сборщик
class Collector:
    def __init__(self, cfg=None):
        self.cfg = cfg or C.load_config()
        self.tail_kb = self.cfg["board"]["tail_kb"]
        self.tails = {}         # path -> Tail
        self.agent_meta = {}    # meta path -> (mtime, dict)
        self.git_cache = {}     # dir -> root|None
        self.auditor_cache = {}  # (pid, procStart) -> bool
        self.transcript_cache = {}  # (cwd, sid) -> path
        self.auditor_pids = set()   # панель сообщает pid своего аудитора

    # ---- живые процессы
    def live_records(self):
        out = []
        for path in glob.glob(os.path.join(C.SESSIONS_DIR, "*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
                pid = int(rec.get("pid") or os.path.basename(path).split(".")[0])
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            st = read_proc_stat(pid)
            if not st:
                continue
            ppid, start, tty_nr = st
            if rec.get("procStart") and str(rec["procStart"]) != start:
                continue  # pid переиспользован другим процессом — файл устарел
            key = (pid, start)
            if key not in self.auditor_cache:
                self.auditor_cache[key] = (
                    pid in self.auditor_pids
                    or (rec.get("name") == C.AUDITOR_NAME and rec.get("nameSource") in (None, "user", "peer"))
                    or proc_env_has(pid, C.AUDITOR_ENV))
            is_aud = self.auditor_cache[key] or pid in self.auditor_pids
            pst = read_proc_stat(ppid)
            rec["_ppid"] = ppid
            rec["_order"] = int(pst[1]) if pst else int(start)
            rec["_tty"] = tty_name(tty_nr)
            rec["_auditor"] = is_aud
            out.append(rec)
        return out

    # ---- файлы переписки
    def transcript_path(self, cwd, sid):
        key = (cwd, sid)
        p = self.transcript_cache.get(key)
        if p and os.path.exists(p):
            return p
        p = os.path.join(C.PROJECTS_DIR, C.slug_for_cwd(cwd or ""), sid + ".jsonl")
        if not os.path.exists(p):
            found = glob.glob(os.path.join(C.PROJECTS_DIR, "*", sid + ".jsonl"))
            p = found[0] if found else p
        self.transcript_cache[key] = p
        return p

    def tail(self, path, is_agent=False):
        t = self.tails.get(path)
        if t is None:
            t = self.tails[path] = Tail(path, self.tail_kb, is_agent)
        t.update()
        return t

    def git_root(self, path):
        d = os.path.dirname(path)
        chain = []
        root = None
        while d and d not in ("/", C.HOME):
            if d in self.git_cache:
                root = self.git_cache[d]
                break
            chain.append(d)
            if os.path.exists(os.path.join(d, ".git")):
                root = d
                break
            d = os.path.dirname(d)
        for x in chain:
            self.git_cache[x] = root
        return root

    # ---- агенты
    def agents_for(self, sess_dir, parent_tail, now):
        bcfg = self.cfg["board"]
        recent = bcfg["agents_recent_h"] * 3600
        stale = bcfg["agent_stale_min"] * 60
        out = []
        sub = os.path.join(sess_dir, "subagents")
        try:
            names = os.listdir(sub)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".jsonl") or not name.startswith("agent-"):
                continue
            aid = name[len("agent-"):-len(".jsonl")]
            jp = os.path.join(sub, name)
            try:
                mt = os.stat(jp).st_mtime
            except OSError:
                continue
            notif = parent_tail.notifications.get(aid)
            if now - mt > recent + 600 and jp not in self.tails:
                continue  # старьё — даже не открываем
            t = self.tail(jp, is_agent=True)
            mp = os.path.join(sub, f"agent-{aid}.meta.json")
            meta = {}
            try:
                mmt = os.stat(mp).st_mtime
                cached = self.agent_meta.get(mp)
                if cached and cached[0] == mmt:
                    meta = cached[1]
                else:
                    with open(mp, encoding="utf-8") as f:
                        meta = json.load(f)
                    self.agent_meta[mp] = (mmt, meta)
            except (OSError, ValueError):
                pass
            lm = t.last_msg
            last_act = max(t.last_ts or 0, 0) or mt
            state, ended = "running", None
            # закончил: последний ход — ответ без вызовов инструментов, или сданный отчёт (SubagentHandback + tool_result).
            # Если агента потом возобновили (SendMessage), новые записи перезапишут last_msg и он снова «работает».
            if lm and (lm[0] == "handback" or (lm[0] == "assistant" and lm[1] in FINISH_REASONS and not lm[2])):
                state, ended = "done", lm[3] or mt
                if notif and notif[0] in ("failed", "killed") and (notif[1] or 0) >= (ended or 0) - 5:
                    state, ended = notif[0], notif[1]
            elif notif and (notif[1] or 0) >= last_act - 2:
                state = {"completed": "done"}.get(notif[0], notif[0])
                ended = notif[1]
            elif now - mt > stale:
                state = "stale"
            if state in ("done", "failed", "killed", "blocked") and ended and now - ended > recent:
                continue
            edits = {}
            for p, ts in t.edits.items():
                edits[p] = ts
            # когда родитель получил отчёт агента (hand-back или task-notification) — от этого момента ждём ответа в чат
            marks = [x for x in (parent_tail.handbacks.get(aid), (notif or (None, None))[1]) if x]
            rep_ = t.report or t.last_text
            out.append({
                "report": cut(_scrub(rep_[1]), 700) if rep_ and state in ("done", "failed", "killed", "blocked") else "",
                "reported": max(marks) if marks else None,
                "id": aid,
                "type": meta.get("agentType") or "?",
                "desc": meta.get("description") or "",
                "model": meta.get("model") or "",
                "depth": meta.get("spawnDepth") or 1,
                "shape": meta.get("requestShape") or "",
                "state": state,
                "started": t.run_start or t.first_ts,
                "first": t.first_ts,
                "ended": ended,
                "last_activity": last_act,
                "_edits": edits,
                "_actions": list(t.actions)[-4:],
            })
        out.sort(key=lambda a: (a["state"] not in ("running", "stale"), -(a["ended"] or a["last_activity"] or 0)))
        return out

    # ---- главный проход
    def collect(self):
        now = time.time()
        bcfg = self.cfg["board"]
        recs = self.live_records()
        auditor = None
        sessions = []
        seen_paths = set()
        for rec in recs:
            if rec["_auditor"]:
                auditor = {"pid": rec["pid"], "sid": rec.get("sessionId"), "status": rec.get("status"),
                           "waiting_for": rec.get("waitingFor"),
                           "status_since": (rec.get("statusUpdatedAt") or 0) / 1000}
                continue
            sid = rec.get("sessionId") or ""
            cwd = rec.get("cwd") or ""
            tp = self.transcript_path(cwd, sid)
            seen_paths.add(tp)
            t = self.tail(tp)
            sess_dir = tp[:-len(".jsonl")]
            agents = self.agents_for(sess_dir, t, now)
            for a in agents:
                seen_paths.add(os.path.join(sess_dir, "subagents", f"agent-{a['id']}.jsonl"))
            name = rec.get("name") or ""
            user_named = rec.get("nameSource") in ("user", "peer") and name
            title, src = (t.custom_title, "custom") if t.custom_title else \
                (name, "name") if user_named else \
                (t.ai_title, "ai") if t.ai_title else \
                (t.agent_name, "agent") if t.agent_name else (name or sid[:8], "derived")
            # правки: свои + агентов
            edits = []
            for p, ts in t.edits.items():
                edits.append((p, ts, "сессия"))
            for a in agents:
                for p, ts in a.pop("_edits").items():
                    edits.append((p, ts, f"агент {a['type']}"))
            repos = {}
            for p, ts, by in edits:
                if not ts or p.startswith("/tmp/"):
                    continue
                root = self.git_root(p) or ""
                key = root or p
                cur = repos.get(key)
                if not cur or ts > cur["ts"]:
                    repos[key] = {"path": key, "git": bool(root), "ts": ts, "by": by, "file": p}
            repo_list = sorted(repos.values(), key=lambda r: -r["ts"])[:6]
            active_repo = next((r for r in repo_list if r["git"] and now - r["ts"] < 3600), None)
            actions = [(ts, s) for ts, s in t.actions]
            for a in agents:
                actions += [(ts, f"[{a['type']}] {s}") for ts, s in a.pop("_actions")]
            actions.sort(key=lambda x: x[0] or 0)
            away = None
            if t.away and t.away[0]:
                away = {"text": t.away[0], "ts": t.away[1],
                        "stale": bool(t.last_user_ts and t.away[1] and t.away[1] < t.last_user_ts)}
            sessions.append({
                "pid": rec["pid"],
                "sid": sid,
                "short": sid[:8],
                "cwd": cwd,
                "kind": rec.get("kind") or "",
                "entrypoint": rec.get("entrypoint") or "",
                "tty": rec["_tty"],
                "order": rec["_order"],
                "status": rec.get("status") or "?",
                "waiting_for": rec.get("waitingFor"),
                "status_since": (rec.get("statusUpdatedAt") or rec.get("updatedAt") or 0) / 1000 or None,
                "started": (rec.get("startedAt") or 0) / 1000 or None,
                "title": title or "",
                "title_src": src,
                "name": name,
                "last_prompt": t.last_prompt,
                "last_user_ts": t.last_user_ts,
                "last_reply_ts": t.last_reply_ts,
                "last_activity": t.last_ts,
                "away": away,
                "repo": active_repo["path"] if active_repo else None,
                "repos": repo_list,
                "recent_actions": [{"ts": ts, "what": s} for ts, s in actions[-10:]],
                # последние реплики — для «дельты» аудитору (летопись фактов); секреты вычищены
                "msgs": [{"ts": ts, "who": who, "text": cut(_scrub(txt), 700)} for ts, who, txt in list(t.msgs)[-8:]],
                "agents": agents,
                "warnings": [],
            })
        # выбросить хвосты файлов, которые больше не нужны
        for p in list(self.tails):
            if p not in seen_paths:
                del self.tails[p]
        warnings = self.warnings(sessions, now)
        sessions.sort(key=lambda s: (s["started"] or 0))
        board = {
            "version": 1,
            "generated": now,
            "generated_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "sessions": sessions,
            "auditor": auditor,
            "warnings": warnings,
        }
        board["digest"] = digest(board)
        return board

    # ---- предупреждения
    def warnings(self, sessions, now):
        b = self.cfg["board"]
        out = []
        by_sid = {}
        for s in sessions:
            by_sid.setdefault(s["sid"], []).append(s)
        for sid, group in by_sid.items():
            if len(group) > 1:
                pids = ", ".join(str(g["pid"]) for g in group)
                text = f"Один чат открыт в {len(group)} терминалах (pid {pids}) — ответы перемешаются, закрой лишний"
                out.append({"kind": "dup", "key": f"dup:{sid}", "sids": [sid], "pids": [g["pid"] for g in group],
                            "level": "high", "text": text})
                for g in group:
                    g["warnings"].append({"kind": "dup", "level": "high",
                                          "text": f"Этот чат открыт ещё в терминале: pid "
                                                  f"{', '.join(str(x['pid']) for x in group if x is not g)}"})
        # две разные сессии правят один репо/файл
        window = b["repo_window_min"] * 60
        ignore = {os.path.expanduser(x) for x in b.get("conflict_ignore") or []}
        touch = {}
        for s in sessions:
            for r in s["repos"]:
                if now - r["ts"] <= window and r["path"] not in ignore:
                    touch.setdefault(r["path"], {})
                    prev = touch[r["path"]].get(s["sid"])
                    if not prev or r["ts"] > prev[1]["ts"]:
                        touch[r["path"]][s["sid"]] = (s, r)
        for path, who in touch.items():
            if len(who) < 2:
                continue
            titles = [f"«{cut(s['title'], 40)}»" for s, _ in who.values()]
            out.append({"kind": "repo", "key": f"repo:{path}:{','.join(sorted(who))}", "sids": sorted(who),
                        "level": "high", "path": path,
                        "text": f"{short_path(path)} правят одновременно: {', '.join(titles)}"})
            for s, r in who.values():
                others = [f"«{cut(o['title'], 40)}»" for o, _ in who.values() if o is not s]
                s["warnings"].append({"kind": "repo", "level": "high",
                                      "text": f"{short_path(path)} правит ещё {', '.join(others)}"})
        # агент готов, а сессия ждёт пользователя и ответа нет
        lim = b["unaccepted_min"] * 60
        for s in sessions:
            if s["status"] != "idle":
                continue
            # «не принят» = после последнего отчёта агента в родителе нет ни ответа Claude с текстом, ни сообщения пользователя
            done = []
            for a in s["agents"]:
                if a["state"] not in ("done", "failed", "killed") or not a["ended"]:
                    continue
                mark = a.get("reported") or a["ended"]
                if (s.get("last_reply_ts") or 0) > mark or (s["last_user_ts"] or 0) > mark:
                    continue
                if now - mark > lim:
                    done.append(a)
            if not done:
                continue
            a = max(done, key=lambda a: a["ended"])
            more = f" (+ещё {len(done) - 1})" if len(done) > 1 else ""
            text = (f"Агент «{cut(a['desc'], 50)}» {AGENT_PAST[a['state']]} {fmt_dur(now - a['ended'])} назад"
                    f"{more}, а ответа в чате нет")
            s["warnings"].append({"kind": "unaccepted", "level": "warn", "text": text})
            out.append({"kind": "unaccepted", "key": f"unacc:{s['sid']}:{a['id']}", "sids": [s["sid"]],
                        "level": "warn", "text": f"«{cut(s['title'], 40)}»: {text}"})
        return out


# ---------------------------------------------------------------- отпечаток и события
def is_human_session(s):
    """Терминал человека, а не `claude -p` из скрипта (боты и стенды — сотни сессий в час). kind их не отличает
    (у `claude -p` он тоже interactive) — решает entrypoint карточки, а в старом board.json без поля — папка /tmp."""
    from .journal import is_bot_session
    return (not s.get("kind") or s["kind"] == "interactive") and not is_bot_session(s)


def digest(board):
    """Отпечаток смыслового состояния (без времени) — для пропуска планового тика аудитора."""
    parts = []
    for s in board["sessions"]:
        if not is_human_session(s):
            continue  # `claude -p` из скриптов (боты и т. п.) — не повод будить аудитор
        parts.append([s["sid"], s["pid"], s["status"], s["title"], s["last_prompt"], s["repo"],
                      [(a["id"], a["state"]) for a in s["agents"]],
                      (s["away"] or {}).get("text")])
    parts.append(sorted(w["key"] for w in board["warnings"]))
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def diff_events(prev, cur):
    """События для аудитора: агент завершился, сессия закрылась, новый дубль/конфликт."""
    if not prev:
        return []
    ev = []
    pa = {}
    for s in prev["sessions"]:
        for a in s["agents"]:
            pa[a["id"]] = (a, s)
    ca = {}
    for s in cur["sessions"]:
        for a in s["agents"]:
            ca[a["id"]] = (a, s)
    for aid, (a, s) in ca.items():
        old = pa.get(aid)
        if old and old[0]["state"] in ("running", "stale") and a["state"] in ("done", "failed", "killed", "blocked"):
            ev.append(f"агент {a['type']} «{cut(a['desc'], 60)}» в сессии «{cut(s['title'], 50)}» — "
                      f"{AGENT_PAST.get(a['state'], a['state'])}")
    cur_sids = {s["sid"] for s in cur["sessions"]}
    gone = {}
    for s in prev["sessions"]:
        if s["sid"] not in cur_sids and is_human_session(s):
            gone[s["sid"]] = s
    for s in gone.values():
        ev.append(f"сессия «{cut(s['title'], 50)}» закрыта (последний запрос: «{cut(s['last_prompt'], 80)}»)")
    prev_keys = {w["key"] for w in prev["warnings"]}
    for w in cur["warnings"]:
        if w["kind"] in ("dup", "repo") and w["key"] not in prev_keys:
            ev.append(("дубль чата: " if w["kind"] == "dup" else "конфликт: ") + w["text"])
    return ev


# ---------------------------------------------------------------- вывод
def status_word(s):
    st = s.get("status")
    if st == "waiting":
        return WAITING_WORDS.get(s.get("waiting_for") or "", "ждёт ответа")
    return STATUS_WORDS.get(st, st or "?")


def render_text(board, now=None):
    """Компактная текстовая версия board — для аудитора и людей."""
    now = now or time.time()
    lines = [f"Пульт Claude · {fmt_clock(board['generated'])} · сессий: {len(board['sessions'])}"]
    for s in board["sessions"]:
        head = (f"- «{s['title']}» [{s['short']} pid {s['pid']}] {status_word(s)} "
                f"{fmt_dur(now - s['status_since']) if s['status_since'] else ''}")
        if s["repo"]:
            head += f"; правит {short_path(s['repo'])}"
        lines.append(head)
        if s["last_prompt"]:
            lines.append(f"    запрос: «{cut(mask_secrets(s['last_prompt']), 160)}»")
        for a in s["agents"]:
            when = (f"{fmt_dur(now - a['started'])}" if a["state"] in ("running", "stale") and a["started"]
                    else f"{fmt_dur(now - a['ended'])} назад" if a["ended"] else "")
            lines.append(f"    агент {a['type']} «{cut(a['desc'], 70)}»: {AGENT_WORDS.get(a['state'], a['state'])} {when}")
        for w in s["warnings"]:
            lines.append(f"    ⚠ {w['text']}")
    return "\n".join(lines) + "\n"


def write_board(board):
    C.ensure_run_dir()
    C.write_atomic(C.BOARD_JSON, json.dumps(board, ensure_ascii=False, default=str))
    C.write_atomic(C.BOARD_TXT, render_text(board))


if __name__ == "__main__":  # отладка: python3 -m claude_panel.collector
    t0 = time.time()
    col = Collector()
    b = col.collect()
    t1 = time.time()
    b2 = col.collect()
    t2 = time.time()
    print(render_text(b))
    for w in b["warnings"]:
        print("WARN", w["text"])
    print(f"первый проход {1000 * (t1 - t0):.0f} мс, повторный {1000 * (t2 - t1):.1f} мс, auditor={b['auditor']}")
