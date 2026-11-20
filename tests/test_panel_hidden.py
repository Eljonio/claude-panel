"""Полное скрытие окна пульта (state.json: hidden): запоминается, переживает перезапуск, скрытый пульт
не показывают ни хук SessionStart, ни `open`/развернуть — только hide/show/toggle-hidden.

Запуск: python3 -m unittest discover -s tests   (из корня репо; без сети, всё во временной папке)
"""
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from claude_panel import config as C  # noqa: E402
from claude_panel import journal as J  # noqa: E402


def load_cli():
    loader = importlib.machinery.SourceFileLoader("claude_panel_cli", os.path.join(ROOT, "bin", "claude-panel"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        run, conf = os.path.join(self.tmp, "run"), os.path.join(self.tmp, "config")
        os.makedirs(run)
        for name, value in (("RUN_DIR", run), ("PANEL_LOG", os.path.join(run, "panel.log")),
                            ("CONFIG_DIR", conf), ("CONFIG_FILE", os.path.join(conf, "config.json")),
                            ("STATE_FILE", os.path.join(conf, "state.json"))):
            p = mock.patch.object(C, name, value)
            p.start()
            self.addCleanup(p.stop)


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.start = mock.Mock(return_value=4242)
        self.kill = mock.Mock()
        for target, name, value in ((self.cli, "start_panel", self.start), (self.cli, "panel_pid", lambda: self.pid),
                                    (os, "kill", self.kill), (J, "on_session_start", mock.Mock())):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.pid = None

    def run_cli(self, *args):
        with mock.patch.object(sys, "argv", ["claude-panel", *args]), \
                mock.patch.object(sys, "stdin", io.StringIO("{}")), mock.patch.object(sys, "stdout", io.StringIO()):
            try:
                self.cli.main()
            except SystemExit:
                pass

    def cmd_file(self):
        try:
            with open(os.path.join(C.RUN_DIR, "cmd")) as f:
                return f.read()
        except OSError:
            return None

    def test_hide_without_panel_is_remembered(self):
        C.save_state({"collapsed": True})
        self.run_cli("hide")
        self.assertEqual(C.load_state(), {"collapsed": True, "hidden": True})
        self.start.assert_not_called()

    def test_show_without_panel_starts_it(self):
        C.save_state({"hidden": True})
        self.run_cli("show")
        self.assertFalse(C.load_state()["hidden"])
        self.start.assert_called_once()

    def test_toggle_hidden_flips(self):
        self.run_cli("toggle-hidden")
        self.assertTrue(C.load_state()["hidden"])
        self.run_cli("toggle-hidden")
        self.assertFalse(C.load_state()["hidden"])

    def test_running_panel_gets_command(self):
        self.pid = 4242
        C.save_state({"hidden": True})
        self.run_cli("toggle-hidden")
        self.assertEqual(self.cmd_file(), "show")
        self.kill.assert_called_once()
        self.start.assert_not_called()

    def test_hook_start_does_not_show_hidden_panel(self):
        """SessionStart: процесс пульта поднимается, но скрытому окну ничего не шлётся и флаг не трогается."""
        C.save_state({"hidden": True})
        for self.pid in (None, 4242):
            self.run_cli("hook-start")
        self.assertTrue(C.load_state()["hidden"])
        self.assertIsNone(self.cmd_file())
        self.kill.assert_not_called()

    def test_open_does_not_show_hidden_panel(self):
        self.pid = 4242
        C.save_state({"hidden": True})
        self.run_cli("open")
        self.kill.assert_not_called()
        self.assertTrue(C.load_state()["hidden"])


class Window(Base):
    def setUp(self):
        super().setUp()
        try:
            from claude_panel import panel as P
        except (ImportError, ValueError) as e:  # нет GTK/Vte
            self.skipTest(f"GTK недоступен: {e}")
        self.P = P
        self.fake = types.SimpleNamespace(hidden=False, collapsed=True, state={"collapsed": True}, board=None,
                                          win=mock.Mock(), apply_collapsed=mock.Mock(), set_collapsed=mock.Mock())
        self.fake.set_hidden = lambda v: P.Panel.set_hidden(self.fake, v)

    def test_hide_and_show_keep_collapsed(self):
        self.P.Panel.set_hidden(self.fake, True)
        self.fake.win.hide.assert_called_once()
        self.fake.win.show.assert_not_called()
        self.assertEqual(C.load_state(), {"collapsed": True, "hidden": True})
        self.P.Panel.set_hidden(self.fake, False)
        self.fake.win.show.assert_called_once()
        self.assertEqual(C.load_state(), {"collapsed": True, "hidden": False})
        self.assertTrue(self.fake.collapsed)

    def test_signal_commands(self):
        for cmd, hidden in (("hide", True), ("toggle-hidden", False), ("toggle-hidden", True), ("show", False)):
            with open(os.path.join(C.RUN_DIR, "cmd"), "w") as f:
                f.write(cmd)
            self.P.Panel.expand_cb(self.fake)
            self.assertEqual(self.fake.hidden, hidden, cmd)
        self.fake.set_collapsed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
