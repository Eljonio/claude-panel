"""Инкрементальное чтение переписок Claude Code (*.jsonl).

Файлы бывают по 15–25 МБ, поэтому:
  * при первом взгляде читаем только хвост (tail_kb), обрезая первую неполную строку;
  * дальше — только новые байты по сохранённому смещению;
  * если в хвосте нет названия чата (ai-title / custom-title) — один раз ищем последнее
    вхождение по всему файлу через mmap.rfind (это проход в C, 15 МБ ~ единицы мс) и кешируем.
"""
import json
import mmap
import os
import re
from collections import deque
from datetime import datetime

from .config import USER_LABEL

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
TASK_RE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>([a-z_]+)</status>", re.S)
SECRET_RE = re.compile(
    r"(EAA[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}|"
    r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}|(?i:bearer)\s+[A-Za-z0-9._\-]{16,}|"
    r"(?i:(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*)['\"]?[^\s'\"&]{6,})"
)


def mask_secrets(s):
    return SECRET_RE.sub("***", s)


def iso_to_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _short(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return name
    if name in EDIT_TOOLS:
        return f"{name} {inp.get('file_path') or inp.get('notebook_path') or ''}"
    if name == "Bash":
        return "Bash: " + mask_secrets(_short(inp.get("command", ""), 220))
    if name in ("Agent", "Task"):
        return f"Agent[{inp.get('subagent_type', '')}]: {_short(inp.get('description', ''), 80)}"
    if name == "SendMessage":
        return f"SendMessage → {inp.get('to', '')}"
    if name in ("WebFetch",):
        return f"WebFetch {_short(inp.get('url', ''), 120)}"
    if name == "Read":
        return f"Read {inp.get('file_path', '')}"
    if name.startswith("mcp__"):
        return name
    if name == "Artifact":
        return f"Artifact {inp.get('action', 'publish')}"
    return name


class Tail:
    """Состояние одного jsonl-файла. update() дочитывает новое и обновляет поля."""

    def __init__(self, path, tail_kb=512, is_agent=False):
        self.path = path
        self.tail_bytes = tail_kb * 1024
        self.is_agent = is_agent
        self.ino = None
        self.offset = 0
        self.buf = b""
        self.size = 0
        self.mtime = 0.0
        self.changed_at = 0.0
        # метаданные чата
        self.ai_title = None
        self.custom_title = None
        self.agent_name = None
        self.last_prompt = None
        self.away = None          # (text, ts)
        self.title_scanned = False
        # активность
        self.last_user_ts = None  # последнее сообщение, набранное человеком
        self.last_ts = None       # последняя запись с временем
        self.first_ts = None
        self.edits = {}           # path -> ts
        self.actions = deque(maxlen=12)  # (ts, summary)
        self.notifications = {}   # task_id -> (status, ts)
        self.handbacks = {}       # agent_id -> ts (отчёт агента пришёл в родителя)
        # последнее сообщение (для агентов): ('assistant'|'user'|'handback', stop_reason, has_tool_use, ts)
        # 'handback' — агент сдал отчёт через SubagentHandback и получил tool_result: работа закончена
        self.last_msg = None
        self.handback_ids = set()  # id вызовов SubagentHandback, ждущих tool_result
        self.run_start = None
        self.last_reply_ts = None  # последнее сообщение Claude с текстом (не субагент) — «ответил в чат»
        # реплики для летописи аудитора: (ts, USER_LABEL|'Claude', текст ≤1500); у агента — его отчёт
        self.msgs = deque(maxlen=12)
        self.report = None        # (ts, текст) — SubagentHandback или последний текст агента
        self.last_text = None

    # ---------- чтение ----------
    def update(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if self.ino is not None and (st.st_ino != self.ino or st.st_size < self.offset):
            self.__init__(self.path, self.tail_bytes // 1024, self.is_agent)  # файл переписан — заново
        if st.st_size == self.offset and self.ino is not None:
            return False
        first = self.ino is None
        self.ino = st.st_ino
        self.mtime = st.st_mtime
        start = self.offset
        skip_partial = False
        if first:
            if self.is_agent:
                self._read_first_ts()
            if st.st_size > self.tail_bytes:
                start = st.st_size - self.tail_bytes
                skip_partial = True
        elif st.st_size - self.offset > self.tail_bytes * 4:
            # за один тик прилетело слишком много — перескакиваем в хвост
            start = st.st_size - self.tail_bytes
            skip_partial = True
            self.buf = b""
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                data = f.read(st.st_size - start)
        except OSError:
            return False
        self.offset = start + len(data)
        self.size = self.offset
        if skip_partial:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        data = self.buf + data
        lines = data.split(b"\n")
        self.buf = lines.pop()  # неполная последняя строка
        if len(self.buf) > 64 * 1024 * 1024:
            self.buf = b""
        for raw in lines:
            if raw:
                self._line(raw)
        if first and not self.is_agent and not (self.ai_title or self.custom_title) and start > 0:
            self._scan_whole_file()
        self.changed_at = st.st_mtime
        return True

    def _read_first_ts(self):
        try:
            with open(self.path, "rb") as f:
                head = f.read(256 * 1024)
            m = re.search(rb'"timestamp":"([^"]+)"', head)
            if m:
                self.first_ts = iso_to_ts(m.group(1).decode())
        except OSError:
            pass

    def _scan_whole_file(self):
        """Один полный проход (в C) за последними метаданными, которых не было в хвосте."""
        self.title_scanned = True
        try:
            with open(self.path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for pat in (b'{"type":"custom-title"', b'{"type":"ai-title"', b'{"type":"last-prompt"',
                            b'{"type":"agent-name"', b'"subtype":"away_summary"'):
                    i = mm.rfind(pat)
                    if i < 0:
                        continue
                    a = mm.rfind(b"\n", 0, i) + 1
                    b = mm.find(b"\n", i)
                    if b < 0:
                        b = len(mm)
                    if b - a > 2 * 1024 * 1024:
                        continue
                    self._line(mm[a:b], meta_only=True)
        except (OSError, ValueError):
            pass

    # ---------- разбор строки ----------
    def _line(self, raw, meta_only=False):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        t = d.get("type")
        if t == "ai-title":
            if not (meta_only and self.ai_title):
                self.ai_title = d.get("aiTitle") or self.ai_title
            return
        if t == "custom-title":
            if not (meta_only and self.custom_title):
                self.custom_title = d.get("customTitle") or self.custom_title
            return
        if t == "agent-name":
            if not (meta_only and self.agent_name):
                self.agent_name = d.get("agentName") or self.agent_name
            return
        if t == "last-prompt":
            if d.get("lastPrompt") and not (meta_only and self.last_prompt):
                self.last_prompt = d["lastPrompt"]
            return
        ts = iso_to_ts(d.get("timestamp"))
        if t == "system" and d.get("subtype") == "away_summary":
            if not (meta_only and self.away):
                txt = str(d.get("content") or "")
                txt = re.sub(r"\s*\(disable recaps in /config\)\s*$", "", txt)
                self.away = (txt, ts)
            return
        if meta_only:
            return
        if ts:
            self.last_ts = ts if not self.last_ts or ts > self.last_ts else self.last_ts
            if self.first_ts is None:
                self.first_ts = ts
        if t == "attachment":
            att = d.get("attachment") or {}
            # сообщение, набранное человеком, пока Claude был занят, — приходит вложением queued_command
            if (not d.get("isSidechain") and att.get("type") == "queued_command"
                    and (att.get("origin") or {}).get("kind") == "human" and att.get("prompt")):
                self.msgs.append((ts, USER_LABEL, str(att["prompt"])[:1500]))
            return
        if t == "queue-operation":
            content = d.get("content")
            if isinstance(content, str) and "<task-notification>" in content:
                self._notif(content, ts)
            return
        if t == "user":
            self._user(d, ts)
        elif t == "assistant":
            self._assistant(d, ts)

    def _notif(self, text, ts):
        for m in TASK_RE.finditer(text):
            tid, status = m.group(1).strip(), m.group(2)
            prev = self.notifications.get(tid)
            if not prev or (ts or 0) >= (prev[1] or 0):
                self.notifications[tid] = (status, ts)

    def _user(self, d, ts):
        msg = d.get("message") or {}
        content = msg.get("content")
        origin = d.get("origin") or {}
        has_result = isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
        if isinstance(content, str) and "<task-notification>" in content:
            self._notif(content, ts)
        if origin.get("kind") == "peer" and origin.get("handback") and origin.get("from"):
            self.handbacks[origin["from"]] = ts
        if not d.get("isSidechain") and not d.get("isMeta") and origin.get("kind") == "human" and not has_result:
            self.last_user_ts = ts
            txt = content if isinstance(content, str) else " ".join(
                str(x.get("text", "")) for x in content or [] if isinstance(x, dict) and x.get("type") == "text")
            if txt.strip():
                self.msgs.append((ts, USER_LABEL, txt[:1500]))
        if self.is_agent:
            handed = has_result and self.handback_ids and any(
                isinstance(x, dict) and x.get("type") == "tool_result" and not x.get("is_error")
                and x.get("tool_use_id") in self.handback_ids for x in content)
            self.last_msg = ("handback" if handed else "user", None, False, ts)
            if handed:
                self.handback_ids.clear()
            if not has_result:
                self.run_start = ts  # первое задание или продолжение через SendMessage

    def _assistant(self, d, ts):
        msg = d.get("message") or {}
        content = msg.get("content") or []
        has_tool = False
        has_text = isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "text" and str(x.get("text", "")).strip() for x in content)
        if has_text:
            txt = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") == "text")
            if self.is_agent:
                self.last_text = (ts, txt[:1500])  # у агента все записи — sidechain
            elif not d.get("isSidechain"):
                self.msgs.append((ts, "Claude", txt[:1500]))
        if not d.get("isSidechain") and has_text:
            if ts and (not self.last_reply_ts or ts > self.last_reply_ts):
                self.last_reply_ts = ts
        if isinstance(content, list):
            for x in content:
                if not isinstance(x, dict) or x.get("type") != "tool_use":
                    continue
                has_tool = True
                name, inp = x.get("name", ""), x.get("input") or {}
                if name == "SubagentHandback" and self.is_agent and x.get("id"):
                    self.handback_ids.add(x["id"])
                    if isinstance(inp, dict) and inp.get("message"):
                        self.report = (ts, str(inp["message"])[:1500])
                if name in EDIT_TOOLS and isinstance(inp, dict):
                    p = inp.get("file_path") or inp.get("notebook_path")
                    if p:
                        self.edits[p] = ts
                        if len(self.edits) > 200:
                            for k in sorted(self.edits, key=lambda k: self.edits[k] or 0)[:50]:
                                self.edits.pop(k, None)
                self.actions.append((ts, _tool_summary(name, inp)))
        if self.is_agent:
            self.last_msg = ("assistant", msg.get("stop_reason"), has_tool, ts)

    # ---------- итог ----------
    @property
    def title(self):
        return self.custom_title or self.ai_title
