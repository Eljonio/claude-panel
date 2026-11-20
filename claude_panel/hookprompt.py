"""Хук UserPromptSubmit: короткая сводка о ДРУГИХ открытых сессиях Claude + свежие факты летописи.

stdout этого хука Claude Code добавляет в контекст сессии как обычный текст.
Правила: < 200 мс, ≤ max_lines строк и ≤ max_chars символов, при любой ошибке — тишина и код 0.
Без повторов: замечание аудитора показывается сессии один раз и не старше hook.audit_max_age_min;
факт летописи без [x] — один раз (своей сессии, если она открыта, иначе первой, кто спросит).
"""
import json
import os
import sys
import time

from . import config as C


def _load_board(cfg):
    try:
        st = os.stat(C.BOARD_JSON)
        if time.time() - st.st_mtime <= cfg["hook"]["board_stale_s"]:
            with open(C.BOARD_JSON, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    # панели нет или board устарел — считаем сами, облегчённо (маленький хвост)
    from .collector import Collector
    cfg["board"]["tail_kb"] = 128
    return Collector(cfg).collect()


def _audit_items(cfg):
    if not C.auditor_enabled(cfg):
        return []  # аудитор выключен — его старые замечания из audit.json сессиям не показываем
    try:
        with open(C.AUDIT_JSON, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items") if isinstance(data, dict) else data
        upd = data.get("updated") if isinstance(data, dict) else None
        return [dict(i, _updated=upd) for i in items or [] if isinstance(i, dict)]
    except (OSError, ValueError):
        return []


def _facts_block(sid, board, cfg, seen, now):
    """≤ facts_max свежих фактов летописи без [x], ещё не показанных. Отмечает показанные в seen."""
    from . import journal as J
    if not cfg.get("journal", {}).get("enabled", True):
        return []
    live = {s.get("short") or str(s.get("sid", ""))[:8] for s in board.get("sessions", [])}
    shown = seen.setdefault("facts", {})
    mine = sid[:8]
    rows = []
    for fid, ts, body in reversed(J.unsynced(cfg, None, max_age_h=24, now=now)):  # свежие первыми
        if fid in shown:
            continue
        owner = body.split(" ", 1)[0]
        if owner != mine and owner in live:
            continue  # своя сессия открыта — перенесёт она
        rows.append((fid, ts, body))
        if len(rows) >= cfg["hook"].get("facts_max", 3):
            break
    if not rows:
        return []
    path = J.short_cwd(C.jpath(cfg, "auto")) or C.jpath(cfg, "auto")
    out = [f"[Летопись пульта: факты, которых ещё нет в памяти — перенеси в стол агента / project_open_tasks "
           f"и поставь [x] в {path}]"]
    from datetime import datetime
    for fid, ts, body in sorted(rows, key=lambda r: r[1]):
        shown[fid] = now
        out.append(f"• {datetime.fromtimestamp(ts):%d.%m %H:%M} {J.one_line(body, 170)} #{fid}")
    return out


def build(sid, board, audit, cfg, now=None, seen=None):
    from .collector import cut, fmt_dur, short_path, WAITING_WORDS, AGENT_WORDS, is_human_session
    now = now or time.time()
    words = {"busy": "работает", "idle": "ждёт пользователя", "shell": "ждёт пользователя, в фоне команда"}
    mine = [s for s in board.get("sessions", []) if s.get("sid") == sid]
    others = [s for s in board.get("sessions", []) if s.get("sid") != sid and is_human_session(s)]
    lines = []
    # один чат в нескольких терминалах — это про меня, важно
    if len(mine) > 1:
        lines.append(f"⚠ Этот же чат открыт ещё в {len(mine) - 1} терминале(ах) (pid "
                     f"{', '.join(str(s['pid']) for s in mine)}): ответы могут перемешаться, попроси пользователя закрыть лишний.")
    for s in mine[:1]:
        for w in s.get("warnings", []):
            if w.get("kind") == "repo":
                lines.append(f"⚠ {w['text']} — перед правкой git status, чужое не трогать.")
    n_mine = len(lines)  # сюда же встанут замечания аудитора — важнее списка других сессий
    # другие сессии: сначала работающие, потом по свежести
    by_sid = {}
    for s in others:
        by_sid.setdefault(s["sid"], []).append(s)
    groups = sorted(by_sid.values(), key=lambda g: (g[0].get("status") != "busy",
                                                   -(g[0].get("last_activity") or 0)))
    for g in groups:
        s = g[0]
        st = s.get("status")
        word = WAITING_WORDS.get(s.get("waiting_for") or "", "ждёт ответа") if st == "waiting" else words.get(st, st)
        since = s.get("status_since")
        part = f"• «{cut(s.get('title'), 48)}»"
        if len(g) > 1:
            part += f" ({len(g)} терминала)"
        part += f" — {word}"
        if since:
            part += f" {fmt_dur(now - since)}"
        if s.get("repo"):
            part += f"; правит {short_path(s['repo'])}"
        run = [a for a in s.get("agents", []) if a.get("state") in ("running", "stale")]
        if run:
            part += "; агенты: " + ", ".join(f"{a['type']} «{cut(a['desc'], 30)}»" for a in run[:2])
            if len(run) > 2:
                part += f" +{len(run) - 2}"
        if any(w.get("kind") == "unaccepted" for w in s.get("warnings", [])):
            part += "; результат агента ещё не принят"
        if s.get("last_prompt"):
            part += f"; запрос: «{cut(s['last_prompt'], 70)}»"
        lines.append(part)
    # замечания аудитора про эту сессию: каждое — один раз и не старше часа (иначе оно «повторяется весь вечер»)
    from . import journal as J
    seen = seen if seen is not None else {}
    first = J.load_json(os.path.join(C.RUN_DIR, "audit-seen.json"), {})
    max_age = cfg["hook"].get("audit_max_age_min", 60) * 60
    shown = seen.setdefault("audit", {}).setdefault(sid[:8], {})
    aud_lines = []
    for it in audit:
        target = str(it.get("session") or "")
        if target and sid and (sid.startswith(target) or target.startswith(sid[:8])):
            k = J.audit_key(it)
            if k in shown or now - first.get(k, J.item_first_ts(it, it.get("_updated"), now)) > max_age:
                continue
            if len(aud_lines) >= 3:
                break
            shown[k] = now
            aud_lines.append(f"Аудитор ({it.get('level', 'info')}): {cut(it.get('text'), 180)}")
    lines[n_mine:n_mine] = aud_lines[:3]
    facts = _facts_block(sid, board, cfg, seen, now)
    if not others and not any(l.startswith(("⚠", "Аудитор")) for l in lines):
        lines = []
    out = []
    if lines:
        head = "[Пульт Claude: другие открытые сессии пользователя — справка для координации, не пересказывай без повода]"
        hc = cfg["hook"]
        out, total = [head], len(head)
        for l in lines:
            if len(out) >= hc["max_lines"] or total + len(l) + 1 > hc["max_chars"]:
                break
            out.append(l)
            total += len(l) + 1
    return "\n".join(out + facts)


def _prune(seen, max_age_s=3 * 86400):
    now = time.time()
    seen["facts"] = {k: v for k, v in (seen.get("facts") or {}).items() if now - v < max_age_s}
    aud = {}
    for s8, d in (seen.get("audit") or {}).items():
        d = {k: v for k, v in d.items() if now - v < max_age_s}
        if d:
            aud[s8] = d
    seen["audit"] = aud


def main():
    try:
        if os.environ.get(C.AUDITOR_ENV) == "1":
            return
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        try:
            sid = (json.loads(raw) if raw.strip() else {}).get("session_id") or ""
        except ValueError:
            sid = ""
        sid = sid or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        cfg = C.load_config()
        board = _load_board(cfg)
        import fcntl
        C.ensure_run_dir()
        with open(C.HOOK_SEEN + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                with open(C.HOOK_SEEN, encoding="utf-8") as f:
                    seen = json.load(f)
            except (OSError, ValueError):
                seen = {}
            before = json.dumps(seen, sort_keys=True)
            text = build(sid, board, _audit_items(cfg), cfg, seen=seen)
            if json.dumps(seen, sort_keys=True) != before:
                _prune(seen)
                C.write_atomic(C.HOOK_SEEN, json.dumps(seen, ensure_ascii=False))
        if text:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # никогда не ломаем ввод пользователя
        pass
