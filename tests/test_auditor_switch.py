"""Выключатель аудитора (auditor.enabled): флаг пишется в config.json; при выключенном хук не подкладывает
замечания аудитора (и старые из audit.json тоже), сводка о других сессиях остаётся; пульт аудитора не запускает.

Запуск: python3 -m unittest discover -s tests   (из корня репо; без сети, всё во временной папке)
"""
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_panel import config as C  # noqa: E402
from claude_panel import hookprompt as H  # noqa: E402

ME = "a11ce000-0000-4000-8000-000000000002"
OTHER = "c0ffee00-0000-4000-8000-000000000003"
REMARK = "деплой ушёл без проверки"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        run, conf = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "config")
        os.makedirs(run)
        for name, value in (("RUN_DIR", run), ("BOARD_JSON", os.path.join(run, "board.json")),
                            ("AUDIT_JSON", os.path.join(run, "audit.json")),
                            ("HOOK_SEEN", os.path.join(run, "hook-delivered.json")),
                            ("CONFIG_DIR", conf), ("CONFIG_FILE", os.path.join(conf, "config.json"))):
            p = mock.patch.object(C, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(C.AUDITOR_ENV, None)
        C.set_option("journal", "enabled", False)  # летопись в настоящем vault не читаем

    def card(self, sid, title):
        return {"sid": sid, "short": sid[:8], "pid": 1, "kind": "interactive", "entrypoint": "cli", "cwd": self.tmp,
                "title": title, "last_prompt": "", "agents": [], "warnings": [], "status": "idle", "repo": None}

    def hook(self):
        with open(C.BOARD_JSON, "w", encoding="utf-8") as f:
            json.dump({"sessions": [self.card(ME, "моя"), self.card(OTHER, "соседняя")], "warnings": []}, f)
        with open(C.AUDIT_JSON, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "a1", "level": "warn", "session": ME[:8], "text": REMARK}]}, f)
        try:
            os.unlink(C.HOOK_SEEN)  # «один раз на сессию» здесь не проверяем
        except OSError:
            pass
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": ME}))), \
                mock.patch.object(sys, "stdout", out):
            H.main()
        return out.getvalue()


class Config(Base):
    def test_default_is_enabled(self):
        self.assertTrue(C.auditor_enabled(C.load_config()))

    def test_set_option_keeps_other_values(self):
        C.set_option("auditor", "effort", "medium")
        C.set_option("auditor", "enabled", False)
        cfg = C.load_config()
        self.assertFalse(C.auditor_enabled(cfg))
        self.assertEqual(cfg["auditor"]["effort"], "medium")
        self.assertFalse(cfg["journal"]["enabled"])
        self.assertEqual(cfg["auditor"]["tick_min"], C.DEFAULTS["auditor"]["tick_min"])
        C.set_option("auditor", "enabled", True)
        self.assertTrue(C.auditor_enabled(C.load_config()))

    def test_broken_config_is_not_overwritten(self):
        with open(C.CONFIG_FILE, "w") as f:
            f.write("{не json")
        with self.assertRaises(ValueError):
            C.set_option("auditor", "enabled", False)
        with open(C.CONFIG_FILE) as f:
            self.assertEqual(f.read(), "{не json")


class Window(Base):
    """Окно аудитора: свой флаг; скрытое окно аудитора не выключает, выключенный аудитор окна не показывает."""

    def test_default_shown(self):
        self.assertTrue(C.auditor_window_shown(C.load_config()))

    def test_hidden_keeps_auditor_enabled(self):
        C.set_option("auditor", "window_visible", False)
        cfg = C.load_config()
        self.assertFalse(C.auditor_window_shown(cfg))
        self.assertTrue(C.auditor_enabled(cfg))
        self.assertIn(REMARK, self.hook())  # замечания идут и при скрытом окне

    def test_disabled_auditor_has_no_window(self):
        C.set_option("auditor", "enabled", False)
        self.assertFalse(C.auditor_window_shown(C.load_config()))
        C.set_option("auditor", "enabled", True)
        self.assertTrue(C.auditor_window_shown(C.load_config()))


class Hook(Base):
    def test_enabled_shows_remark(self):
        text = self.hook()
        self.assertIn(f"Аудитор (warn): {REMARK}", text)
        self.assertIn("«соседняя»", text)

    def test_disabled_hides_remark_keeps_sessions(self):
        C.set_option("auditor", "enabled", False)
        text = self.hook()
        self.assertNotIn("Аудитор", text)
        self.assertNotIn(REMARK, text)
        self.assertIn("другие открытые сессии", text)
        self.assertIn("«соседняя»", text)

    def test_enabled_again_shows_remark(self):
        C.set_option("auditor", "enabled", False)
        self.hook()
        C.set_option("auditor", "enabled", True)
        self.assertIn(REMARK, self.hook())


class PanelStart(Base):
    """Пульт: при выключенном флаге процесс аудитора не запускается, тик не уходит."""

    def setUp(self):
        super().setUp()
        try:
            from claude_panel import panel as P
        except (ImportError, ValueError) as e:  # нет GTK/Vte
            self.skipTest(f"GTK недоступен: {e}")
        self.P = P
        C.set_option("auditor", "enabled", False)
        cfg = C.load_config()
        self.fake = types.SimpleNamespace(cfg=cfg, acfg=cfg["auditor"], term=None, auditor_alive=False,
                                          auditor_pid=None, manual_tick=False, board={"sessions": []})

    def test_start_does_not_spawn(self):
        with mock.patch.object(self.P.A, "write_files") as wf, mock.patch.object(self.P.Vte, "Terminal") as term:
            self.assertFalse(self.P.Panel.start_auditor(self.fake))
        wf.assert_not_called()
        term.assert_not_called()
        self.assertIsNone(self.fake.term)
        self.assertFalse(self.fake.auditor_alive)

    def test_tick_and_restart_are_ignored(self):
        self.fake.start_auditor = mock.Mock()
        self.fake.stop_auditor = mock.Mock()
        self.fake.auditor_step = mock.Mock()
        self.P.Panel.request_tick(self.fake)
        self.P.Panel.restart_auditor(self.fake)
        self.assertFalse(self.fake.manual_tick)
        for m in (self.fake.start_auditor, self.fake.stop_auditor, self.fake.auditor_step):
            m.assert_not_called()

    def test_no_remarks_shown(self):
        self.fake.audit = {"items": [{"id": "a1", "level": "high", "text": REMARK}]}
        self.assertEqual(self.P.Panel.visible_items(self.fake), [])


if __name__ == "__main__":
    unittest.main()
