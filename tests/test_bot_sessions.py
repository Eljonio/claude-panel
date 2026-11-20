"""Бот-сессии (`claude -p` из скриптов: боты и стенды, cwd /tmp, entrypoint sdk-cli) не попадают
в журнал сессий и в очередь аудитору; интерактивные терминалы человека — попадают.

Запуск: python3 -m unittest discover -s tests   (из корня репо; без сети, всё во временной папке)
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_panel import collector as K  # noqa: E402
from claude_panel import config as C  # noqa: E402
from claude_panel import journal as J  # noqa: E402

DEAD_PID = 99999999  # больше pid_max — процесса нет
BOT_SID = "b0b0b0b0-0000-4000-8000-000000000001"
HUMAN_SID = "a11ce000-0000-4000-8000-000000000002"
BOT_PROMPT = "CUSTOMER: demo message from a test script, no reply for 1 h 45 min."
HUMAN_PROMPT = "fix the panel journal"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="panel-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        state = os.path.join(self.tmp, "state")
        self.cfg = copy.deepcopy(C.DEFAULTS)
        self.cfg["journal"].update(sessions=os.path.join(self.tmp, "vault", "journal_sessions.md"),
                                   auto=os.path.join(self.tmp, "vault", "journal_auto.md"),
                                   archive_dir=os.path.join(self.tmp, "vault", "archive"))
        patches = [
            mock.patch.object(C, "HOME", self.home),
            mock.patch.object(C, "SESSIONS_DIR", os.path.join(self.tmp, "sessions")),
            mock.patch.object(C, "PROJECTS_DIR", os.path.join(self.tmp, "projects")),
            mock.patch.object(C, "STATE_DIR", state),
            mock.patch.object(C, "LIVE_DIR", os.path.join(state, "live")),
            mock.patch.object(C, "QUEUE_DIR", os.path.join(state, "review-queue")),
            mock.patch.object(C, "load_config", lambda: copy.deepcopy(self.cfg)),
            mock.patch.object(J, "claude_pid", lambda: self.pid),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        os.environ.pop("CLAUDE_PID", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pid = None
        self.bot_tp = self.transcript(BOT_SID, "/tmp", "sdk-cli", BOT_PROMPT, human=False)
        self.human_tp = self.transcript(HUMAN_SID, self.home, "cli", HUMAN_PROMPT, human=True)

    # --- помощники
    def transcript(self, sid, cwd, entrypoint, prompt, human):
        d = os.path.join(C.PROJECTS_DIR, C.slug_for_cwd(cwd))
        os.makedirs(d, exist_ok=True)
        base = {"entrypoint": entrypoint, "cwd": cwd, "sessionId": sid, "userType": "external"}
        rows = [
            dict(base, type="user", timestamp="2026-10-23T08:39:25.621Z", promptSource="typed" if human else "sdk",
                 origin={"kind": "human"} if human else None, message={"role": "user", "content": prompt}),
            dict(base, type="assistant", timestamp="2026-10-23T08:39:40.000Z",
                 message={"role": "assistant", "stop_reason": "end_turn",
                          "content": [{"type": "text", "text": "Готово, сделал."}]}),
            {"type": "last-prompt", "lastPrompt": prompt, "sessionId": sid},
        ]
        p = os.path.join(d, sid + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        return p

    def registry(self, pid, entrypoint, cwd):
        """~/.claude/sessions/<pid>.json как его пишет Claude Code 2.1.309 (у `claude -p` kind тоже interactive)."""
        os.makedirs(C.SESSIONS_DIR, exist_ok=True)
        with open(os.path.join(C.SESSIONS_DIR, f"{pid}.json"), "w") as f:
            json.dump({"pid": pid, "kind": "interactive", "entrypoint": entrypoint, "cwd": cwd}, f)

    def journal(self):
        return J._read(C.jpath(self.cfg, "sessions")) or ""

    def queued(self):
        try:
            return sorted(n[:-5] for n in os.listdir(C.QUEUE_DIR))
        except OSError:
            return []


class IsBotSession(unittest.TestCase):
    def test_entrypoint_decides(self):
        self.assertTrue(J.is_bot_session(rec={"entrypoint": "sdk-cli", "cwd": "/home/alice"}))
        self.assertTrue(J.is_bot_session(info={"entrypoint": "sdk-ts"}))
        self.assertTrue(J.is_bot_session(env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertFalse(J.is_bot_session(rec={"entrypoint": "cli", "cwd": "/tmp"}))  # человек в /tmp — человек
        self.assertFalse(J.is_bot_session(env={"CLAUDE_CODE_ENTRYPOINT": "cli"}, info={"entrypoint": "sdk-cli"}))
        self.assertFalse(J.is_bot_session(rec={"entrypoint": "claude-vscode"}))

    def test_tmp_only_when_entrypoint_unknown(self):
        self.assertTrue(J.is_bot_session(cwd="/tmp"))
        self.assertTrue(J.is_bot_session(rec={"cwd": "/tmp/claude-1000/x/scratchpad"}))
        self.assertFalse(J.is_bot_session(rec={"cwd": "/tmpfoo"}))
        self.assertFalse(J.is_bot_session(rec={"cwd": "/home/alice"}))
        self.assertFalse(J.is_bot_session())


class Reap(Base):
    """Главный путь шума: пульт ставил маркер бот-сессии, она умирала → «оборвалась» + очередь аудитору."""

    def test_bot_marker_not_journaled_nor_queued(self):
        J.mark_live(BOT_SID, DEAD_PID, "1", "/tmp", self.bot_tp, "")
        self.assertEqual(J.reap(self.cfg), [])
        self.assertNotIn(BOT_SID, self.journal())
        self.assertEqual(self.queued(), [])
        self.assertFalse(os.path.exists(J.marker_path(BOT_SID)))

    def test_human_marker_journaled_and_queued(self):
        J.mark_live(HUMAN_SID, DEAD_PID, "1", self.home, self.human_tp, "")
        lines = J.reap(self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertIn("оборвалась", lines[0])
        self.assertIn(f"`{HUMAN_SID}`", self.journal())
        self.assertEqual(self.queued(), [HUMAN_SID])


class Hooks(Base):
    def test_start_bot_sets_no_marker(self):
        self.pid = os.getpid()
        self.registry(self.pid, "sdk-cli", "/tmp")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        J.on_session_start({"session_id": BOT_SID, "cwd": "/tmp", "transcript_path": self.bot_tp})
        self.assertIsNone(J.read_marker(BOT_SID))

    def test_start_human_sets_marker(self):
        self.pid = os.getpid()
        self.registry(self.pid, "cli", self.home)
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        J.on_session_start({"session_id": HUMAN_SID, "cwd": self.home, "transcript_path": self.human_tp})
        self.assertEqual((J.read_marker(HUMAN_SID) or {}).get("pid"), self.pid)

    def test_end_bot_with_stale_marker(self):
        """Маркер поставил пульт старой версии, SessionEnd «other» — всё равно ни строки, ни очереди."""
        self.pid = os.getpid()
        J.mark_live(BOT_SID, self.pid, J.proc_start(self.pid), "/tmp", self.bot_tp, "")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        J.on_session_end({"session_id": BOT_SID, "cwd": "/tmp", "transcript_path": self.bot_tp, "reason": "other"})
        self.assertNotIn(BOT_SID, self.journal())
        self.assertEqual(self.queued(), [])
        self.assertIsNone(J.read_marker(BOT_SID))

    def test_end_bot_without_env_and_registry(self):
        """Ни окружения, ни ~/.claude/sessions — решает переписка (entrypoint sdk-cli)."""
        J.on_session_end({"session_id": BOT_SID, "cwd": "/tmp", "transcript_path": self.bot_tp, "reason": "other"})
        self.assertNotIn(BOT_SID, self.journal())
        self.assertEqual(self.queued(), [])

    def test_end_human_dropped_goes_to_auditor(self):
        self.pid = os.getpid()
        self.registry(self.pid, "cli", self.home)
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        J.on_session_end({"session_id": HUMAN_SID, "cwd": self.home, "transcript_path": self.human_tp,
                          "reason": "other"})
        self.assertIn("оборвалась (окно закрыто", self.journal())
        self.assertEqual(self.queued(), [HUMAN_SID])

    def test_end_human_closed_not_queued(self):
        self.pid = os.getpid()
        self.registry(self.pid, "cli", self.home)
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        J.on_session_end({"session_id": HUMAN_SID, "cwd": self.home, "transcript_path": self.human_tp,
                          "reason": "prompt_input_exit"})
        self.assertIn(f"закрыта · «", self.journal())
        self.assertIn(f"`{HUMAN_SID}`", self.journal())
        self.assertEqual(self.queued(), [])


class Panel(Base):
    """Пульт: маркеры ставит и события «сессия закрыта» шлёт аудитору только по is_human_session."""

    @staticmethod
    def card(sid, entrypoint, cwd, title):
        return {"sid": sid, "pid": 1, "kind": "interactive", "entrypoint": entrypoint, "cwd": cwd, "title": title,
                "last_prompt": "", "agents": [], "status": "idle", "repo": None, "away": None}

    def test_is_human_session(self):
        self.assertFalse(K.is_human_session(self.card(BOT_SID, "sdk-cli", "/tmp", "tmp-fd")))
        self.assertTrue(K.is_human_session(self.card(HUMAN_SID, "cli", self.home, "журнал")))
        old = self.card(BOT_SID, "", "/tmp", "tmp-fd")  # board.json пульта старой версии — без entrypoint
        del old["entrypoint"]
        self.assertFalse(K.is_human_session(old))

    def test_closed_bot_is_not_an_event(self):
        prev = {"sessions": [self.card(BOT_SID, "sdk-cli", "/tmp", "tmp-fd"),
                             self.card(HUMAN_SID, "cli", self.home, "журнал")], "warnings": []}
        ev = K.diff_events(prev, {"sessions": [], "warnings": []})
        self.assertEqual(len(ev), 1)
        self.assertIn("журнал", ev[0])


class BackfillAndPurge(Base):
    def test_backfill_skips_bot(self):
        lines = J.backfill(self.cfg, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn(HUMAN_SID, lines[0])

    def test_purge_removes_only_bot_lines(self):
        head = J.SESSIONS_HEAD.replace("{max_kb}", "40")
        human = J.session_line(self.cfg, HUMAN_SID, J.scan_transcript(self.human_tp), "закрыта", 1792740000)
        bot = J.session_line(self.cfg, BOT_SID, J.scan_transcript(self.bot_tp), "оборвалась", 1792740060,
                             {"cwd": "/tmp"})
        gone_sid = "0d0d0d0d-1111-4222-8333-944455556666"  # переписки уже нет — решает папка /tmp в строке
        no_tp = f"- 2026-10-23 13:39 · оборвалась · «0d0d0d0d» · `{gone_sid}` · /tmp · » {BOT_PROMPT}"
        path = C.jpath(self.cfg, "sessions")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = head + "\n".join([human, bot, no_tp]) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        dry = J.purge_bot_lines(self.cfg, dry=True)
        self.assertEqual(len(dry[path]), 2)
        self.assertEqual(self.journal(), text)  # --dry ничего не трогает
        J.purge_bot_lines(self.cfg)
        self.assertEqual(self.journal(), head + human + "\n")  # шапка и строка человека — байт в байт


if __name__ == "__main__":
    unittest.main()
