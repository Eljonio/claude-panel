"""Пульт сессий Claude — боковая панель GTK3 справа поверх всех окон.

Сверху — живые сессии Claude Code с агентами и предупреждениями, посередине — замечания аудитора,
снизу — встроенный терминал (Vte) с сессией Claude-аудитора.
Сигналы: SIGHUP — развернуть, SIGUSR1 — перезапустить аудитор, SIGUSR2 — тик аудита сейчас, SIGTERM — закрыть.
"""
import fcntl
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

from . import auditor as A  # noqa: E402
from . import config as C  # noqa: E402
from . import journal as J  # noqa: E402
from .collector import (AGENT_WORDS, Collector, cut, diff_events, fmt_dur, is_human_session,  # noqa: E402
                        short_path, status_word, write_board)

AUDIT_SEEN = os.path.join(C.RUN_DIR, "audit-seen.json")  # ключ замечания → когда впервые появилось

ACCENT = "#1F5BFF"
CSS = """
window.cp, .cp-main, .cp-strip { background-color: #15171b; color: #d8dbe0; }
.cp-main { border-left: 1px solid #2a2e35; }
.cp-strip { border-left: 1px solid #2a2e35; }
.cp-header { padding: 7px 8px 7px 12px; border-bottom: 1px solid #262a31; }
.cp-title { font-weight: 700; font-size: 12.5px; color: #e6e8eb; }
.dim { color: #8b919b; }
.dimmer { color: #666c76; }
.small { font-size: 10.5px; }
.tiny { font-size: 9.5px; }
.section-h { font-size: 10px; font-weight: 700; color: #7d838d; padding: 8px 12px 4px 12px; }
.card { background-color: #1c1f24; border: 1px solid #272b32; border-radius: 6px; padding: 7px 9px; }
.card.hover { background-color: #21252b; border-color: #1F5BFF; }
.card-title { font-weight: 600; font-size: 12px; color: #e6e8eb; }
.dot-busy, .st-busy { color: #6f95ff; }
.dot-shell, .st-shell { color: #6f95ff; }
.dot-idle, .st-idle { color: #d2a13a; }
.dot-waiting, .st-waiting { color: #f0b429; font-weight: 700; }
.dot-dead { color: #666c76; }
.warn-high { color: #f2555a; }
.warn-warn { color: #d2a13a; }
.ag-running { color: #6f95ff; }
.ag-stale { color: #8b919b; }
.ag-done { color: #3fb27f; }
.ag-failed, .ag-killed { color: #f2555a; }
.ag-blocked { color: #f0b429; }
.audit-item { background-color: #1c1f24; border-left: 3px solid #666c76; padding: 5px 8px; }
.audit-item.high { border-left-color: #e5484d; }
.audit-item.warn { border-left-color: #d2a13a; }
.audit-item.info { border-left-color: #1F5BFF; }
.lvl-high { color: #f2555a; font-weight: 700; }
.lvl-warn { color: #d2a13a; font-weight: 700; }
.lvl-info { color: #6f95ff; font-weight: 700; }
button.cp-btn { background-image: none; background-color: transparent; border: 1px solid #2c3038;
  color: #c9cdd3; padding: 1px 8px; min-height: 20px; border-radius: 4px; box-shadow: none; text-shadow: none;
  font-size: 10.5px; }
button.cp-btn:hover { border-color: #1F5BFF; color: #ffffff; }
button.cp-btn:disabled { border-color: #23262c; color: #555a63; }
button.cp-btn.primary { background-color: #1F5BFF; border-color: #1F5BFF; color: #ffffff; }
.toast { color: #d2a13a; font-size: 10.5px; padding: 0 12px 4px 12px; }
.term-head { padding: 5px 8px 5px 12px; border-top: 1px solid #262a31; }
.term-head switch { min-height: 16px; min-width: 34px; background-color: #2c3038; border: none; }
.term-head switch:checked { background-color: #1F5BFF; }
.term-head switch slider { min-height: 14px; min-width: 14px; }
paned > separator { background-color: #262a31; min-height: 3px; background-image: none; }
.strip-count { font-size: 10px; font-weight: 700; }
scrolledwindow, viewport { background-color: #15171b; }
"""

VTE_PALETTE = ["#1c1f24", "#e5484d", "#3fb27f", "#d2a13a", "#4d7dff", "#b779e8", "#3fb5c4", "#c9cdd3",
               "#4a5059", "#f2555a", "#58c794", "#f0b429", "#6f95ff", "#c996f0", "#5fd0dd", "#eef0f2"]


def rgba(s):
    c = Gdk.RGBA()
    c.parse(s)
    return c


def log(*a):
    print(datetime.now().strftime("%F %T"), *a, file=sys.stderr, flush=True)


def label(text="", classes=(), xalign=0.0, wrap=False, ellipsize=False, markup=False, selectable=False):
    lb = Gtk.Label()
    if markup:
        lb.set_markup(text)
    else:
        lb.set_text(text)
    lb.set_xalign(xalign)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lb.set_max_width_chars(30)
    if ellipsize:
        lb.set_ellipsize(Pango.EllipsizeMode.END)
    lb.set_selectable(selectable)
    for c in classes:
        lb.get_style_context().add_class(c)
    return lb


def add_class(w, *classes):
    for c in classes:
        w.get_style_context().add_class(c)
    return w


class Panel:
    def __init__(self, started_by_hook=False):
        self.cfg = C.load_config()
        self.pcfg = self.cfg["panel"]
        self.acfg = self.cfg["auditor"]
        self.state = C.load_state()
        self.collapsed = bool(self.state.get("collapsed"))
        self.hidden = bool(self.state.get("hidden"))  # окна нет совсем; пульт при этом работает
        self.started_by_hook = started_by_hook
        self.collector = Collector(self.cfg)
        self.board = None
        self.audit = {"items": [], "updated": None}
        self.audit_mtime = 0
        self.view_sig = None
        self.strip_sig = None
        self.audit_sig = None
        self.stopping = False
        # аудитор
        self.auditor_pid = None
        self.auditor_started = 0
        self.auditor_alive = False
        self.pending_events = []
        self.first_event_at = None
        self.manual_tick = False
        self.last_tick_at = self.state.get("last_tick_at", 0)
        self.last_digest = self.state.get("last_digest")
        self.last_typed = 0
        self.sent_at = 0
        self.no_sessions_since = None
        self.seen_session = False
        self.toast_id = None
        # журнал и летопись
        self.jcfg = self.cfg["journal"]
        self.closed_buf = []          # сессии, закрывшиеся с прошлого тика: их реплики — в дельту
        self.audit_first = J.load_json(AUDIT_SEEN, {})
        self.dropped_new = []         # id замечаний, снятых по давности, — сказать аудитору один раз
        self.dropped_told = set()
        self.delivered_told = set()
        self.unsynced_due = []        # факты летописи без [x] старше срока — отдать аудитору на сверку
        self.unsynced_asked = set(self.state.get("unsynced_asked") or [])
        self.reaped_new = 0
        self.last_reap = 0
        self.live_marked = {}         # sid → (pid, procStart): маркер уже стоит
        self.build_ui()
        self.thread = threading.Thread(target=self.collect_loop, daemon=True)
        self.thread.start()
        for sig, fn in ((signal.SIGTERM, self.quit), (signal.SIGINT, self.quit), (signal.SIGHUP, self.expand_cb),
                        (signal.SIGUSR1, self.restart_auditor_cb), (signal.SIGUSR2, self.tick_now_cb)):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, fn)
        self.sync_auditor_ui()
        GLib.idle_add(self.start_auditor)

    # ================================================================ UI
    def build_ui(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        w = self.win = Gtk.Window(title="Пульт Claude")
        add_class(w, "cp")
        w.set_decorated(False)
        w.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        w.set_skip_taskbar_hint(True)
        w.set_skip_pager_hint(True)
        w.set_keep_above(True)
        w.set_focus_on_map(False)
        w.stick()
        w.connect("delete-event", lambda *_: True)  # закрывается только командой
        w.connect("map-event", lambda *_: self.place())

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        w.add(outer)
        # --- свёрнутая полоска
        self.strip = Gtk.EventBox()
        add_class(self.strip, "cp-strip")
        self.strip.set_tooltip_text("Развернуть пульт")
        self.strip.connect("button-press-event", lambda *_: self.set_collapsed(False))
        self.strip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.strip_box.set_margin_top(8)
        self.strip.add(self.strip_box)
        outer.pack_start(self.strip, False, False, 0)
        # --- развёрнутая панель
        main = self.main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        add_class(main, "cp-main")
        outer.pack_start(main, True, True, 0)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_class(head, "cp-header")
        head.pack_start(label("Пульт Claude", ["cp-title"]), False, False, 0)
        self.summary = label("", ["dim", "small"], ellipsize=True)
        head.pack_start(self.summary, True, True, 0)
        btn = add_class(Gtk.Button(label="›"), "cp-btn")
        btn.set_tooltip_text("Свернуть в полоску")
        btn.connect("clicked", lambda *_: self.set_collapsed(True))
        head.pack_end(btn, False, False, 0)
        hb = add_class(Gtk.Button(label="Скрыть пульт"), "cp-btn")
        hb.set_tooltip_text("Убрать пульт с экрана совсем (он продолжит работать).\n"
                            "Вернуть: Ctrl+Alt+P или команда claude-panel show")
        hb.connect("clicked", lambda *_: self.set_hidden(True))
        head.pack_end(hb, False, False, 0)
        main.pack_start(head, False, False, 0)
        self.toast = label("", ["toast"], wrap=True)
        self.toast.set_no_show_all(True)
        main.pack_start(self.toast, False, False, 0)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        main.pack_start(self.paned, True, True, 0)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.paned.pack1(top, True, False)
        # список сессий
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.list_box.set_margin_start(8)
        self.list_box.set_margin_end(8)
        self.list_box.set_margin_top(8)
        self.list_box.set_margin_bottom(8)
        sc.add(self.list_box)
        top.pack_start(sc, True, True, 0)
        # аудит
        ah = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        ah.pack_start(label("АУДИТ", ["section-h"]), False, False, 0)
        self.audit_meta = label("", ["dimmer", "tiny"], ellipsize=True)
        ah.pack_start(self.audit_meta, True, True, 0)
        top.pack_start(ah, False, False, 0)
        asc = Gtk.ScrolledWindow()
        asc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        asc.set_propagate_natural_height(True)
        asc.set_max_content_height(160)
        self.audit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.audit_box.set_margin_start(8)
        self.audit_box.set_margin_end(8)
        self.audit_box.set_margin_bottom(8)
        asc.add(self.audit_box)
        top.pack_start(asc, False, False, 0)
        # шапка аудитора — внизу верхней половины: когда окно аудитора скрыто, нижней половины нет вовсе
        # и всё место достаётся списку сессий, а шапка с кнопками остаётся
        th = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_class(th, "term-head")
        th.pack_start(label("АУДИТОР", ["section-h"]), False, False, 0)
        self.aud_switch = Gtk.Switch()
        self.aud_switch.set_valign(Gtk.Align.CENTER)
        self.aud_switch.connect("notify::active", self.on_aud_switch)
        th.pack_start(self.aud_switch, False, False, 0)
        self.aud_status = label("", ["dim", "small"], ellipsize=True)
        th.pack_start(self.aud_status, True, True, 0)
        b1 = add_class(Gtk.Button(label="Проверить"), "cp-btn")
        b1.set_tooltip_text("Отправить аудитору тик прямо сейчас")
        b1.connect("clicked", lambda *_: self.request_tick())
        b2 = add_class(Gtk.Button(label="↻"), "cp-btn")
        b2.set_tooltip_text("Перезапустить аудитор")
        b2.connect("clicked", lambda *_: self.restart_auditor())
        self.win_btn = add_class(Gtk.Button(), "cp-btn")
        self.win_btn.connect("clicked", lambda *_: self.set_window_visible(not self.acfg.get("window_visible", True)))
        th.pack_end(b2, False, False, 0)
        th.pack_end(b1, False, False, 0)
        th.pack_end(self.win_btn, False, False, 0)
        self.aud_buttons = (b1, b2, self.win_btn)
        top.pack_start(th, False, False, 0)
        # окно аудитора: встроенный терминал
        self.term_stack = Gtk.Stack()
        self.paned.pack2(self.term_stack, True, True)
        self.term = None
        dead = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dead.set_valign(Gtk.Align.CENTER)
        dead.pack_start(label("Аудитор закрыт", ["dim"], xalign=0.5), False, False, 0)
        rb = add_class(Gtk.Button(label="Перезапустить аудитор"), "cp-btn", "primary")
        rb.set_halign(Gtk.Align.CENTER)
        rb.connect("clicked", lambda *_: self.restart_auditor())
        dead.pack_start(rb, False, False, 0)
        self.term_stack.add_named(dead, "dead")
        self.term_holder = Gtk.Box()
        self.term_stack.add_named(self.term_holder, "term")

        outer.show_all()
        if not self.hidden:
            w.show()
        # Высоту терминала держим сами: при обеих половинах resize=True GTK пересчитывает разделитель
        # в пропорции, и одно мгновение низкого окна (выход из сна, смена экрана) оставляет терминалу 0 px.
        self.term_h = self.state.get("term_h")
        self.state.pop("paned_pos", None)  # старый ключ: в нём сохранялось и такое «нулевое» положение
        self.paned_alloc = 0
        self.setting_pos = False
        self.paned_ready = False
        self.paned.connect("size-allocate", self.on_paned_alloc)
        self.paned.connect("notify::position", self.on_paned_moved)
        Gdk.Screen.get_default().connect("monitors-changed", lambda *_: self.apply_collapsed())
        self.apply_collapsed()

    def workarea(self):
        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() or disp.get_monitor(0)
        return mon.get_workarea()

    def place(self):
        wa = self.workarea()
        width = self.pcfg["collapsed_width"] if self.collapsed else self.pcfg["width"]
        self.win.resize(width, wa.height)
        self.win.move(wa.x + wa.width - width, wa.y)
        return False

    def on_paned_alloc(self, paned, alloc):
        # сигнал идёт после того, как GTK уже переставил разделитель под новую высоту — возвращаем терминалу его высоту
        if alloc.height != self.paned_alloc:
            self.paned_alloc = alloc.height
            if alloc.height >= 3 * self.pcfg["terminal_min_px"]:
                GLib.idle_add(self.apply_term_h)

    def apply_term_h(self):
        h = self.paned_alloc
        lo = self.pcfg["terminal_min_px"]
        term_h = self.term_h or int(h * self.pcfg["terminal_fraction"])
        pos = h - max(lo, min(term_h, h - lo))
        if pos != self.paned.get_position():
            self.setting_pos = True
            self.paned.set_position(pos)
            self.setting_pos = False
        self.paned_ready = True
        return False

    def on_paned_moved(self, *_):
        h = self.paned.get_allocated_height()
        lo = self.pcfg["terminal_min_px"]
        if (not self.paned_ready or self.setting_pos or self.collapsed or not self.term_stack.get_visible()
                or h != self.paned_alloc or h < 3 * lo):
            return  # раскладка GTK или наша же установка — не перетаскивание мышью
        dragged = h - self.paned.get_position()
        term_h = max(lo, min(dragged, h - lo))
        if term_h != dragged:
            GLib.idle_add(self.apply_term_h)  # утащили к самому краю — оставляем минимум
        if term_h != self.term_h:
            self.term_h = self.state["term_h"] = term_h
            GLib.timeout_add(1500, self.save_state_once)

    def save_state_once(self):
        C.save_state(self.state)
        return False

    def apply_collapsed(self):
        wa = self.workarea()
        if self.collapsed:
            self.main.hide()
            self.strip.show_all()
            self.strip.set_size_request(self.pcfg["collapsed_width"], wa.height)
        else:
            self.strip.hide()
            self.main.show()
            self.main.set_size_request(self.pcfg["width"], wa.height)
        self.win.set_size_request(-1, -1)
        self.place()
        GLib.timeout_add(120, self.place)  # mutter иногда двигает окно после изменения размера

    def set_collapsed(self, value):
        if value == self.collapsed:
            return
        self.collapsed = value
        self.state["collapsed"] = value
        C.save_state(self.state)
        self.apply_collapsed()
        self.view_sig = self.strip_sig = None
        if self.board:
            self.render(self.board)

    def set_hidden(self, value):
        """Убрать окно пульта с экрана совсем / вернуть в прежнем виде (свёрнут или развёрнут).
        Сборщик, board для хука, аудитор и журнал от этого не зависят."""
        value = bool(value)
        if value == self.hidden:
            return
        self.hidden = value
        self.state["hidden"] = value
        C.save_state(self.state)
        log("пульт скрыт" if value else "пульт показан")
        if value:
            self.win.hide()
        else:
            self.win.show()
            self.apply_collapsed()
            self.view_sig = self.strip_sig = None
            if self.board:
                self.render(self.board)

    def show_toast(self, text, sec=6):
        self.toast.set_text(text)
        self.toast.show()
        if self.toast_id:
            GLib.source_remove(self.toast_id)
        self.toast_id = GLib.timeout_add_seconds(sec, self.hide_toast)

    def hide_toast(self):
        self.toast.hide()
        self.toast_id = None
        return False

    # ================================================================ отрисовка
    def render(self, board):
        now = time.time()
        sess = board["sessions"]
        busy = sum(1 for s in sess if s["status"] == "busy")
        waiting = sum(1 for s in sess if s["status"] in ("idle", "waiting", "shell"))
        nwarn = len(board["warnings"])
        summary = f"{len(sess)} сесс. · {busy} в работе · {waiting} ждут" + (f" · ⚠ {nwarn}" if nwarn else "")
        if self.summary.get_text() != summary:
            self.summary.set_text(summary)
        if self.collapsed:
            self.render_strip(board)
            return
        seen, cards = set(), []
        for s in sess:
            m = self.card_model(s, now)
            if s["sid"] in seen:  # тот же чат во втором терминале — без повтора запроса, сводки и агентов
                m.update(prompt="", away="", agents=[])
            seen.add(s["sid"])
            cards.append(m)
        sig = json.dumps(cards, ensure_ascii=False)
        if sig != self.view_sig:
            self.view_sig = sig
            for ch in self.list_box.get_children():
                self.list_box.remove(ch)
            if not sess:
                self.list_box.pack_start(label("Открытых сессий Claude нет", ["dim"], xalign=0.5), False, False, 12)
            for s, m in zip(sess, cards):
                self.list_box.pack_start(self.card_widget(s, m), False, False, 0)
            self.list_box.show_all()

    def card_model(self, s, now):
        since = fmt_dur(now - s["status_since"]) if s.get("status_since") else ""
        place = short_path(s["repo"]) if s.get("repo") else short_path(s["cwd"])
        meta = f"{'правит ' if s.get('repo') else ''}{place} · pid {s['pid']}"
        if s.get("tty"):
            meta += f" · {s['tty']}"
        if s.get("kind") and s["kind"] != "interactive":
            meta += f" · {s['kind']}"
        agents = []
        for a in s["agents"]:
            if a["state"] in ("running", "stale"):
                when = f"{fmt_dur(now - (a['started'] or now))}"
                if a["state"] == "stale":
                    when = f"молчит {fmt_dur(now - a['last_activity'])}"
                word = f"работает {when}" if a["state"] == "running" else when
                mark = "▸"
            else:
                word = f"{AGENT_WORDS.get(a['state'], a['state'])} {fmt_dur(now - a['ended'])} назад" if a["ended"] else a["state"]
                mark = "✓" if a["state"] == "done" else "✗"
            agents.append({"state": a["state"], "mark": mark, "type": a["type"], "desc": a["desc"],
                           "word": word, "model": a.get("model") or "", "depth": a.get("depth") or 1})
        away = s.get("away") or {}
        return {
            "title": s["title"] or s["short"], "status": s["status"], "word": status_word(s), "since": since,
            "meta": meta, "prompt": cut(s.get("last_prompt"), 120) if s.get("last_prompt") else "",
            "away": cut(away.get("text"), 150) if away.get("text") and not away.get("stale") else "",
            "warnings": [(w["level"], w["text"]) for w in s["warnings"]], "agents": agents,
        }

    def card_widget(self, s, m):
        eb = Gtk.EventBox()
        eb.set_tooltip_text("Перейти к окну этого терминала")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        add_class(box, "card")
        eb.add(box)
        r1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        st = m["status"] if m["status"] in ("busy", "idle", "waiting", "shell") else "dead"
        r1.pack_start(label("●", [f"dot-{st}"]), False, False, 0)
        r1.pack_start(label(m["title"], ["card-title"], ellipsize=True), True, True, 0)
        r1.pack_end(label(f"{m['word']} {m['since']}".strip(), [f"st-{st}", "small"]), False, False, 0)
        box.pack_start(r1, False, False, 0)
        box.pack_start(label(m["meta"], ["dimmer", "tiny"], ellipsize=True), False, False, 0)
        if m["prompt"]:
            box.pack_start(label("» " + m["prompt"], ["dim", "small"], wrap=True), False, False, 0)
        if m["away"]:
            box.pack_start(label(m["away"], ["dimmer", "small"], wrap=True), False, False, 0)
        for lvl, text in m["warnings"]:
            box.pack_start(label("⚠ " + text, [f"warn-{lvl}", "small"], wrap=True), False, False, 0)
        for a in m["agents"]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.set_margin_start(4 + 10 * (a["depth"] - 1))
            row.pack_start(label(a["mark"], [f"ag-{a['state']}", "small"]), False, False, 0)
            row.pack_start(label(a["type"], ["small"]), False, False, 0)
            row.pack_start(label(a["desc"], ["dim", "small"], ellipsize=True), True, True, 0)
            tail = a["word"] + (f" · {a['model']}" if a["model"] else "")
            row.pack_end(label(tail, [f"ag-{a['state']}", "tiny"]), False, False, 0)
            box.pack_start(row, False, False, 0)
        eb.connect("enter-notify-event", lambda *_: box.get_style_context().add_class("hover"))
        eb.connect("leave-notify-event", lambda *_: box.get_style_context().remove_class("hover"))
        eb.connect("button-press-event", lambda _w, ev: self.on_card_click(s, ev))
        return eb

    def on_card_click(self, s, ev):
        try:
            from .windows import activate
            ok, note = activate(s, self.board["sessions"] if self.board else [s], ev.time)
            if not ok or note:
                self.show_toast(note)
        except Exception as e:  # Wnck недоступен и т. п.
            self.show_toast(f"не удалось переключить окно: {e}")
        return True

    def render_strip(self, board):
        dots = [(s["status"], len(s["warnings"])) for s in board["sessions"]]
        nwarn = len(board["warnings"]) + sum(1 for i in self.visible_items() if i.get("level") == "high")
        sig = json.dumps([dots, nwarn])
        if sig == self.strip_sig:
            return
        self.strip_sig = sig
        for ch in self.strip_box.get_children():
            self.strip_box.remove(ch)
        self.strip_box.pack_start(label("‹", ["dim"], xalign=0.5), False, False, 0)
        for st, _ in dots:
            st = st if st in ("busy", "idle", "waiting", "shell") else "dead"
            self.strip_box.pack_start(label("●", [f"dot-{st}"], xalign=0.5), False, False, 0)
        if nwarn:
            self.strip_box.pack_start(label("⚠", ["warn-high"], xalign=0.5), False, False, 4)
            self.strip_box.pack_start(label(str(nwarn), ["warn-high", "strip-count"], xalign=0.5), False, False, 0)
        self.strip_box.show_all()

    def render_audit(self):
        items = self.visible_items()
        upd = self.audit.get("updated")
        try:
            upd_s = datetime.fromisoformat(str(upd).replace("Z", "+00:00")).astimezone().strftime("%H:%M") if upd else ""
        except ValueError:
            upd_s = str(upd)[:16]
        on = C.auditor_enabled(self.cfg)
        meta = (f"обновлено {upd_s} · " if upd_s else "") + (f"замечаний: {len(items)}" if items else "")
        if not on:
            meta = "выключен"
        titles = {s["short"]: s["title"] for s in (self.board or {}).get("sessions", [])}
        model = [(i.get("level", "info"), i.get("text", ""), i.get("session", ""), i.get("title", ""), i.get("time", ""))
                 for i in items]
        sig = json.dumps([meta, model], ensure_ascii=False)
        if sig == self.audit_sig:
            return
        self.audit_sig = sig
        self.audit_meta.set_text(meta)
        for ch in self.audit_box.get_children():
            self.audit_box.remove(ch)
        if not items:
            empty = "Аудитор выключен" if not on else "Замечаний нет" if upd else "Аудитор ещё не проверял"
            self.audit_box.pack_start(label(empty, ["dimmer", "small"]), False, False, 2)
        order = {"high": 0, "warn": 1, "info": 2}
        for lvl, text, sess, title, tm in sorted(model, key=lambda x: order.get(x[0], 3)):
            lvl = lvl if lvl in order else "info"
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            add_class(box, "audit-item", lvl)
            who = title or titles.get(str(sess)[:8]) or (str(sess)[:8] if sess else "общее")
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            head.pack_start(label({"high": "ВАЖНО", "warn": "ВНИМАНИЕ", "info": "ИНФО"}[lvl], [f"lvl-{lvl}", "tiny"]),
                            False, False, 0)
            head.pack_start(label(who, ["dim", "tiny"], ellipsize=True), True, True, 0)
            if tm:
                head.pack_end(label(str(tm), ["dimmer", "tiny"]), False, False, 0)
            box.pack_start(head, False, False, 0)
            box.pack_start(label(text, ["small"], wrap=True, selectable=False), False, False, 0)
            self.audit_box.pack_start(box, False, False, 0)
        self.audit_box.show_all()

    def load_audit(self):
        try:
            mt = os.stat(C.AUDIT_JSON).st_mtime
        except OSError:
            return
        if mt == self.audit_mtime:
            return
        try:
            with open(C.AUDIT_JSON, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                data = {"items": data, "updated": datetime.fromtimestamp(mt).isoformat()}
            if isinstance(data, dict):
                data.setdefault("updated", datetime.fromtimestamp(mt).isoformat())
                self.audit = data
                self.audit_mtime = mt
                self.take_facts(data)
        except (OSError, ValueError):
            pass  # файл дописывается — возьмём на следующем тике

    def take_facts(self, data):
        """Факты аудитора → летопись (пульт пишет сам: только в конец, без секретов и повторов); carried → [x]."""
        now = time.time()
        changed = False
        for it in data.get("items") or []:
            if isinstance(it, dict):
                k = J.audit_key(it)
                if k not in self.audit_first:
                    self.audit_first[k] = J.item_first_ts(it, data.get("updated"), now)
                    changed = True
        live_keys = {J.audit_key(i) for i in data.get("items") or [] if isinstance(i, dict)}
        for k in [k for k, ts in self.audit_first.items() if k not in live_keys and now - ts > 6 * 3600]:
            self.audit_first.pop(k, None)
            changed = True
        if changed:
            try:
                C.write_atomic(AUDIT_SEEN, json.dumps(self.audit_first))
            except OSError:
                pass
        if not self.jcfg.get("enabled", True):
            return
        try:
            titles = {s["short"]: s["title"] for s in (self.board or {}).get("sessions", [])}
            added = J.append_facts(self.cfg, data.get("facts"), titles, now)
            if added:
                log(f"летопись: +{len(added)} факт(ов)")
            n = J.mark_carried(self.cfg, data.get("carried"))
            if n:
                log(f"летопись: {n} факт(ов) уже в памяти — [x]")
        except Exception:
            log("летопись:", traceback.format_exc())

    def visible_items(self):
        """Замечания без устаревших: одно и то же не показываем дольше audit_max_age_min.
        Аудитор выключен — замечаний нет (старые из audit.json не показываем)."""
        if not C.auditor_enabled(self.cfg):
            return []
        now = time.time()
        lim = self.cfg["hook"]["audit_max_age_min"] * 60
        out = []
        for it in self.audit.get("items") or []:
            if not isinstance(it, dict):
                continue
            k = J.audit_key(it)
            if now - self.audit_first.get(k, now) > lim:
                if it.get("id") and it["id"] not in self.dropped_told and it["id"] not in self.dropped_new:
                    self.dropped_new.append(it["id"])
                continue
            out.append(it)
        return out

    # ================================================================ сбор данных
    def collect_loop(self):
        last_write, last_sig = 0, None
        while not self.stopping:
            t0 = time.time()
            try:
                board = self.collector.collect()
                sig = board["digest"] + str([(s["pid"], s["status"], s["status_since"]) for s in board["sessions"]])
                if sig != last_sig or t0 - last_write > 10:
                    write_board(board)
                    last_write, last_sig = t0, sig
                if self.jcfg.get("enabled", True):
                    self.journal_step(board, t0)
                GLib.idle_add(self.on_board, board)
            except Exception:
                log("collect error:", traceback.format_exc())
            time.sleep(max(0.3, self.pcfg["refresh_s"] - (time.time() - t0)))

    def journal_step(self, board, now):
        """Поток сборщика: маркеры живых сессий; раз в reap_s — сессии, умершие без SessionEnd, и сверка летописи."""
        try:
            live = set()
            for s in board["sessions"]:
                if not is_human_session(s):
                    continue
                live.add(s["sid"])
                if self.live_marked.get(s["sid"]) == s["pid"] and os.path.exists(J.marker_path(s["sid"])):
                    continue
                if J.ended_by_hook(s["sid"], s["pid"]):
                    continue  # SessionEnd уже записал «закрыта», процесс вот-вот выйдет — маркер не возвращаем
                tp = self.collector.transcript_path(s["cwd"], s["sid"])
                J.mark_live(s["sid"], s["pid"], J.proc_start(s["pid"]), s["cwd"], tp,
                            s["title"] if s.get("title_src") in ("custom", "name") else "")
                self.live_marked[s["sid"]] = s["pid"]
            for sid in list(self.live_marked):
                if sid not in live:
                    self.live_marked.pop(sid, None)
            if now - self.last_reap >= self.jcfg["reap_s"]:
                self.last_reap = now
                reaped = J.reap(self.cfg, skip_sids=live)
                if reaped:
                    log("журнал: оборвались без SessionEnd:", len(reaped))
                    GLib.idle_add(self.on_reaped, len(reaped))
                due = [(i, b) for i, _, b in J.unsynced(self.cfg, self.jcfg["unsynced_min"], now=now)
                       if i not in self.unsynced_asked]
                GLib.idle_add(self.set_unsynced, due)
        except Exception:
            log("journal error:", traceback.format_exc())

    def on_reaped(self, n):
        self.reaped_new += n
        return False

    def set_unsynced(self, due):
        self.unsynced_due = due
        return False

    def on_board(self, board):
        prev, self.board = self.board, board
        now = time.time()
        on = C.auditor_enabled(self.cfg)
        if prev and on:
            cur = {s["sid"] for s in board["sessions"]}
            self.closed_buf += [s for s in prev["sessions"] if s["sid"] not in cur and is_human_session(s)
                                and not any(c["sid"] == s["sid"] for c in self.closed_buf)]
        ev = diff_events(prev, board) if on else []  # выключенному аудитору события и реплики не копим
        if ev:
            self.pending_events += ev
            self.first_event_at = self.first_event_at or now
            log("events:", ev)
        # автозакрытие: пользовательских сессий нет дольше grace
        if board["sessions"]:
            self.seen_session = True
            self.no_sessions_since = None
        else:
            self.no_sessions_since = self.no_sessions_since or now
            if (self.started_by_hook or self.seen_session) and now - self.no_sessions_since > self.pcfg["autoclose_grace_s"]:
                log("сессий Claude не осталось — закрываюсь")
                self.quit()
                return False
        if on:
            self.load_audit()
        try:
            self.render(board)
            self.render_audit()
            self.auditor_step(board, now)
        except Exception:
            log("render error:", traceback.format_exc())
        return False

    # ================================================================ аудитор
    def on_aud_switch(self, sw, _param):
        if sw.get_active() != C.auditor_enabled(self.cfg):  # не наша же установка из sync_auditor_ui
            self.set_auditor_enabled(sw.get_active())

    def set_auditor_enabled(self, value, save=True):
        """Выключатель аудитора. save=True — нажали в пульте, пишем флаг в config.json;
        save=False — флаг уже записал `claude-panel auditor on|off`."""
        value = bool(value)
        if save:
            try:
                C.set_option("auditor", "enabled", value)
            except (OSError, ValueError) as e:
                self.show_toast(f"не удалось записать в конфиг: {e}")
                self.sync_auditor_ui()
                return
        was = C.auditor_enabled(self.cfg)
        self.acfg["enabled"] = value
        if value != was:
            log("аудитор включён" if value else "аудитор выключен")
        if value:
            if not self.auditor_alive:
                self.start_auditor()
            elif not was:  # процесс ещё завершается после «выключить» — стартуем с паузой, как при перезапуске
                GLib.timeout_add(1500, self.start_auditor)
        else:
            pid = self.auditor_pid
            if self.stop_auditor():
                GLib.timeout_add_seconds(5, self.kill_auditor, pid)
            self.pending_events = []
            self.first_event_at = None
            self.manual_tick = False
            self.closed_buf = []
        self.sync_auditor_ui()

    def set_window_visible(self, value, save=True):
        """Показать / скрыть окно (терминал) аудитора; сам аудитор при этом работает как работал."""
        value = bool(value)
        if save:
            try:
                C.set_option("auditor", "window_visible", value)
            except (OSError, ValueError) as e:
                self.show_toast(f"не удалось записать в конфиг: {e}")
                return
        if value != bool(self.acfg.get("window_visible", True)):
            log("окно аудитора показано" if value else "окно аудитора скрыто")
        self.acfg["window_visible"] = value
        self.sync_auditor_ui()

    def sync_auditor_ui(self):
        on = C.auditor_enabled(self.cfg)
        self.aud_switch.set_active(on)
        self.aud_switch.set_tooltip_text("Аудитор включён — нажмите, чтобы выключить" if on
                                         else "Аудитор выключен — нажмите, чтобы включить")
        for b in self.aud_buttons:
            b.set_sensitive(on)
        if not on:
            self.aud_status.set_text("выключен")
        shown = C.auditor_window_shown(self.cfg)
        hidden_by_btn = not self.acfg.get("window_visible", True)
        self.win_btn.set_label("Показать окно" if hidden_by_btn else "Скрыть окно")
        self.win_btn.set_tooltip_text("Окно аудитора скрыто, сам аудитор работает — нажмите, чтобы показать" if hidden_by_btn
                                      else "Убрать окно аудитора; сам аудитор продолжит работать")
        if shown != self.term_stack.get_visible():
            self.term_stack.set_visible(shown)
            if shown and self.paned_alloc >= 3 * self.pcfg["terminal_min_px"]:
                GLib.idle_add(self.apply_term_h)  # вернуть окну его высоту
        self.audit_sig = self.strip_sig = None
        self.render_audit()
        if self.collapsed and self.board:
            self.render_strip(self.board)

    def stop_auditor(self):
        """SIGTERM процессу аудитора. True — было что останавливать."""
        if not (self.auditor_pid and self.auditor_alive):
            return False
        try:
            os.kill(self.auditor_pid, signal.SIGTERM)
        except OSError:
            pass
        return True

    def kill_auditor(self, pid):
        if self.auditor_alive and self.auditor_pid == pid:
            log("аудитор не вышел по SIGTERM за 5 с — SIGKILL")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return False

    def start_auditor(self):
        if not C.auditor_enabled(self.cfg):
            return False
        if self.term is not None:
            self.term_holder.remove(self.term)
            self.term = None
        try:
            A.write_files()
        except OSError as e:
            log("auditor files:", e)
        t = self.term = Vte.Terminal()
        t.set_font(Pango.FontDescription.from_string(self.pcfg["font_mono"]))
        t.set_colors(rgba("#d8dbe0"), rgba("#111317"), [rgba(c) for c in VTE_PALETTE])
        t.set_cursor_blink_mode(Vte.CursorBlinkMode.OFF)
        t.set_scrollback_lines(5000)
        t.set_size(60, 8)  # иначе Vte просит 80×24 и не даёт опустить разделитель
        t.set_audible_bell(False)
        t.set_mouse_autohide(True)
        t.connect("commit", self.on_user_typed)
        t.connect("child-exited", self.on_auditor_exit)
        self.term_holder.pack_start(t, True, True, 0)
        t.show()
        self.term_stack.set_visible_child_name("term")
        argv = A.argv(self.cfg)
        log("старт аудитора:", " ".join(argv))
        self.auditor_started = time.time()
        self.auditor_alive = True
        self.trust_seen_at = None
        self.trust_answered = False
        cwd = os.path.expanduser(self.acfg.get("cwd") or "~")
        t.spawn_async(Vte.PtyFlags.DEFAULT, cwd, argv, A.env(), GLib.SpawnFlags.DEFAULT,
                      None, None, -1, None, self.on_spawned, None)
        if self.acfg.get("auto_trust_home", True):
            GLib.timeout_add(700, self.watch_trust_dialog, t)
        return False

    def term_text(self, t, rows_up=80, rows_down=12):
        """Текст буфера Vte вокруг курсора (TUI после отрисовки часто поднимает курсор выше низа диалога).
        get_text_format() отдаёт только «показанные» строки, а пока пульт свёрнут, терминал аудитора
        создаётся в скрытом блоке, ни разу не получает размер — и там пусто. Поэтому читаем строки
        буфера по номерам: они есть и у невидимого терминала."""
        try:
            row = t.get_cursor_position()[1]
            res = t.get_text_range_format(Vte.Format.TEXT, max(0, row - rows_up), 0,
                                          row + rows_down, t.get_column_count())
            txt = res[0] if isinstance(res, tuple) else res
            if txt:
                return txt
        except Exception:
            pass
        try:
            return t.get_text_format(Vte.Format.TEXT) or ""
        except Exception:
            try:
                return (t.get_text(None, None) or ("",))[0] or ""
            except Exception:
                return ""

    def watch_trust_dialog(self, t):
        """Claude Code никогда не сохраняет доверие к домашней папке и на каждом старте в ~ спрашивает
        «Yes, I trust this folder». В СВОЁМ терминале аудитора отвечаем «да» сами (стрелка вниз + Enter)."""
        if t is not self.term or not self.auditor_alive or self.trust_answered:
            return False
        if time.time() - self.auditor_started > 120:  # первый старт после обновления Claude Code бывает долгим
            log("аудитор: диалог доверия за 120 с не распознан — отвечать не стал")
            return False
        txt = self.term_text(t)
        # пробелы не сравниваем: TUI ставит слова курсором, а не пробелами, и это может меняться между версиями
        flat = re.sub(r"\s+", "", txt)
        if ("trustthisfolder" in flat or "Accessingworkspace" in flat
                or ("No,exit" in flat and "Entertoconfirm" in flat)):
            now = time.time()
            if self.trust_seen_at is None:
                self.trust_seen_at = now
            elif now - self.trust_seen_at > 1.5:  # диалог первые мгновения игнорирует ввод
                self.trust_answered = True
                log("аудитор: подтверждаю доверие к домашней папке в своём терминале")
                self.feed(b"\x1b[B")
                GLib.timeout_add(500, lambda: (self.feed(b"\r"), False)[1])
                return False
        return True

    def feed(self, data):
        if self.term is None:
            return
        self._feeding = True  # «commit» от нашей же вставки не считаем набором пользователя
        try:
            self.term.feed_child(data)
        except TypeError:
            self.term.feed_child_binary(data)
        finally:
            self._feeding = False

    def on_spawned(self, term, pid, error, *_):
        if error or pid is None or pid < 0:
            log("аудитор не запустился:", error)
            self.auditor_alive = False
            self.term_stack.set_visible_child_name("dead")
            return
        self.auditor_pid = pid
        self.collector.auditor_pids.add(pid)
        log("аудитор pid", pid)

    def on_auditor_exit(self, term, status):
        log("аудитор завершился, код", status)
        if term is not self.term:
            return  # прежний терминал, новый аудитор уже запущен
        self.auditor_alive = False
        self.auditor_pid = None
        if not self.stopping and C.auditor_enabled(self.cfg):
            self.term_stack.set_visible_child_name("dead")
            self.aud_status.set_text("закрыт")

    def on_user_typed(self, *_):
        if not getattr(self, "_feeding", False):
            self.last_typed = time.time()

    def restart_auditor(self):
        if not C.auditor_enabled(self.cfg):
            return
        if self.stop_auditor():
            GLib.timeout_add(1500, self.start_auditor)
        else:
            self.start_auditor()

    def request_tick(self):
        if not C.auditor_enabled(self.cfg):
            return
        self.manual_tick = True
        if self.board:
            self.auditor_step(self.board, time.time())

    def auditor_step(self, board, now):
        a = board.get("auditor")
        if not self.auditor_alive or not C.auditor_enabled(self.cfg):
            return
        st = (a or {}).get("status") if a and a.get("pid") == self.auditor_pid else None
        tick_s = self.acfg["tick_min"] * 60
        text = {"idle": "ждёт", "busy": "проверяет…", "waiting": "ждёт ответа в диалоге",
                None: "запускается…"}.get(st, st)
        extra = ""
        if self.last_tick_at:
            extra = f" · проверка {datetime.fromtimestamp(self.last_tick_at):%H:%M}"
        if self.pending_events:
            extra += f" · событий в очереди: {len(self.pending_events)}"
        self.aud_status.set_text(text + extra)
        if st != "idle" or now - self.sent_at < 15:
            return
        if now - self.last_typed < self.acfg["typing_guard_s"]:
            return  # пользователь сам что-то пишет аудитору — не мешаем
        reason = None
        queued = J.queue_size() if self.jcfg.get("enabled", True) else 0
        if self.manual_tick:
            reason = "ручная проверка"
        elif self.pending_events and now - (self.first_event_at or now) >= self.acfg["event_debounce_s"]:
            reason = "события"
        elif queued:
            reason = "разбор оборвавшихся сессий"
        elif self.unsynced_due:
            reason = "сверка летописи с памятью"
        elif board["sessions"] and board["digest"] != self.last_digest and (
                now - self.last_tick_at >= tick_s or not self.last_tick_at):
            reason = "плановая проверка" if self.last_tick_at else "стартовая проверка"
        if not reason:
            return
        since = self.last_tick_at or (now - 3600)
        extra = self.tick_extra(board, since, now, queued)
        msg = A.compose_tick(board, reason, self.pending_events, since, now, extra)
        self.send_to_auditor(msg)
        self.pending_events = []
        self.first_event_at = None
        self.manual_tick = False
        self.closed_buf = []
        self.last_tick_at = now
        self.last_digest = board["digest"]
        self.state["last_tick_at"] = now
        self.state["last_digest"] = self.last_digest
        self.state["unsynced_asked"] = sorted(self.unsynced_asked)[-300:]
        C.save_state(self.state)

    def tick_extra(self, board, since, now, queued):
        """Летопись в тике: дельта реплик, оборвавшиеся сессии, факты без [x], снятые и уже показанные замечания."""
        extra = {}
        if not self.jcfg.get("enabled", True):
            return extra
        try:
            extra["delta_n"] = A.build_delta(board, since, now, self.closed_buf, J.last_facts(self.cfg, 8),
                                             self.jcfg["delta_kb"])
            if queued:
                extra["reviews"] = J.take_reviews(self.cfg, self.jcfg["review_per_tick"])
            if self.unsynced_due:
                extra["unsynced"] = self.unsynced_due[:6]
                self.unsynced_asked.update(i for i, _ in self.unsynced_due[:6])
                self.unsynced_due = self.unsynced_due[6:]
            if self.dropped_new:
                extra["dropped"] = list(self.dropped_new)
                self.dropped_told.update(self.dropped_new)
                self.dropped_new = []
            seen = J.load_json(C.HOOK_SEEN, {}).get("audit") or {}
            replied = {s["short"]: max(s.get("last_reply_ts") or 0, s.get("last_user_ts") or 0)
                       for s in board["sessions"]}
            told = []
            for it in self.visible_items():
                sess = str(it.get("session") or "")[:8]
                ts = (seen.get(sess) or {}).get(J.audit_key(it))
                if ts and replied.get(sess, 0) > ts and it.get("id") not in self.delivered_told:
                    told.append(it.get("id"))
                    self.delivered_told.add(it.get("id"))
            if told:
                extra["delivered"] = told
            self.reaped_new = 0
        except Exception:
            log("tick extra:", traceback.format_exc())
        return extra

    def send_to_auditor(self, msg):
        log("тик аудитору:", msg[:200], "…" if len(msg) > 200 else "")
        self.sent_at = time.time()
        self.feed(msg.encode("utf-8"))
        # Enter отдельно и с паузой — иначе TUI примет его за часть вставки
        GLib.timeout_add(450, lambda: (self.feed(b"\r"), False)[1])

    # ================================================================ сигналы
    def expand_cb(self):
        """SIGHUP: команда из файла cmd (collapse/expand/toggle/hide/show/toggle-hidden/auditor),
        по умолчанию — развернуть."""
        path = os.path.join(C.RUN_DIR, "cmd")
        cmd = "expand"
        try:
            with open(path) as f:
                cmd = f.read().strip() or "expand"
            os.unlink(path)
        except OSError:
            pass
        if cmd == "auditor":  # `claude-panel auditor on|off|show|hide` записал флаг в config.json — перечитываем
            fresh = C.load_config()["auditor"]
            self.set_window_visible(fresh.get("window_visible", True), save=False)
            self.set_auditor_enabled(fresh.get("enabled", True), save=False)
        elif cmd in ("hide", "show", "toggle-hidden"):
            self.set_hidden({"hide": True, "show": False}.get(cmd, not self.hidden))
        elif cmd == "collapse":
            self.set_collapsed(True)
        elif cmd == "toggle":
            self.set_collapsed(not self.collapsed)
        else:
            self.set_collapsed(False)
        return True

    def restart_auditor_cb(self):
        self.restart_auditor()
        return True

    def tick_now_cb(self):
        self.request_tick()
        return True

    def quit(self, *_):
        if self.stopping:
            return False
        self.stopping = True
        self.stop_auditor()
        C.save_state(self.state)
        GLib.timeout_add(800, Gtk.main_quit)
        return False


def main():
    C.ensure_run_dir()
    lockf = open(os.path.join(C.RUN_DIR, "panel.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("пульт уже запущен", file=sys.stderr)
        return 0
    with open(C.PANEL_PID, "w") as f:
        f.write(str(os.getpid()))
    # переменные родительской сессии Claude пульту и аудитору не нужны
    for k in list(os.environ):
        if k.startswith("CLAUDE") and k != "CLAUDE_PANEL_BY_HOOK":
            os.environ.pop(k, None)
    by_hook = os.environ.pop("CLAUDE_PANEL_BY_HOOK", "") == "1"
    GLib.set_prgname("claude-panel")
    GLib.set_application_name("Пульт Claude")
    Gtk.init(sys.argv)
    Panel(started_by_hook=by_hook)
    log("пульт запущен, pid", os.getpid())
    Gtk.main()
    try:
        os.unlink(C.PANEL_PID)
    except OSError:
        pass
    log("пульт закрыт")
    return 0


if __name__ == "__main__":
    sys.exit(main())
