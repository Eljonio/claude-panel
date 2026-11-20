"""Переход к окну терминала сессии.

Почему так, а не по PID: у всех окон gnome-terminal _NET_WM_PID = gnome-terminal-server (один процесс на все окна),
а связи «вкладка (GNOME_TERMINAL_SCREEN) → окно» gnome-terminal наружу не отдаёт (ни в D-Bus, ни в X-свойствах).
Зато Claude Code сам ставит заголовок терминала: «✳ <название чата>» (в работе — «◐/◑ <название>»),
а у окна заголовок = заголовок активной вкладки. Поэтому:
  1) ищем окна gnome-terminal, чей заголовок без значка равен названию чата — это точное совпадение;
  2) если одинаковых названий несколько (один чат в двух терминалах), сопоставляем по порядку создания:
     окна — по X id (растёт с созданием), сессии — по времени старта их shell-родителя. Это приближение, о нём пишем.
Сессия во неактивной вкладке окна не найдётся — честно сообщаем.
"""
import re

GLYPHS = re.compile(r"^[\s✳✱✻◐◑◒◓⠀-⣿·•*⏺✻✽✶✢]+")


def _norm(t):
    return GLYPHS.sub("", t or "").strip()


def activate(session, sessions, timestamp=0, dry_run=False):
    """Вернёт (ok, пояснение[, xid при dry_run])."""
    import gi
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck
    screen = Wnck.Screen.get_default()
    if screen is None:
        return False, "нет X-экрана"
    screen.force_update()
    wins = [w for w in screen.get_windows()
            if (w.get_class_group_name() or "").lower().startswith("gnome-terminal")]
    names = {x for x in (session.get("title"), session.get("name")) if x}
    cand = [w for w in wins if _norm(w.get_name()) in names]
    if not cand:
        # заголовок мог обрезаться — пробуем по началу
        cand = [w for w in wins if any(_norm(w.get_name()) and n.startswith(_norm(w.get_name())[:30]) for n in names)]
    if not cand:
        return False, "окно не найдено — сессия, видимо, в неактивной вкладке"
    note = ""
    if len(cand) > 1:
        same = sorted([s for s in sessions if s.get("title") == session.get("title")], key=lambda s: s.get("order") or 0)
        cand.sort(key=lambda w: w.get_xid())
        idx = next((i for i, s in enumerate(same) if s.get("pid") == session.get("pid")), 0)
        if len(cand) == len(same):
            win = cand[idx]
            note = "окон с таким названием несколько — выбрал по порядку открытия"
        else:
            win = cand[min(idx, len(cand) - 1)]
            note = "окон с таким названием несколько — выбор приблизительный"
    else:
        win = cand[0]
    if dry_run:
        return True, note, win.get_xid(), win.get_name()
    ws = win.get_workspace()
    if ws is not None and ws != screen.get_active_workspace():
        ws.activate(timestamp)
    win.activate(timestamp)
    return True, note
