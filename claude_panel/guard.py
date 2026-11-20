"""PreToolUse-страж сессии аудитора. Срабатывает ДО правил разрешений (exit 2 = запрет, даже если что-то разрешено).

Аудитору можно:
  * Write/Edit/MultiEdit — только audit.json;
  * Bash — только чтение: ls/cat/head/tail/grep/wc/stat/find/du/date/git (log|status|show|diff|…), без записи и сети;
  * Read/Grep/Glob — всё, кроме секретов.
"""
import json
import os
import re
import shlex
import sys

from . import config as C

SECRET_RE = re.compile(r"(^|/)(\.env(\.[^/]*)?|\.secrets|\.ssh|\.credentials\.json|credentials|vault\.age|id_rsa|id_ed25519|"
                       r"\.netrc|\.pgpass|settings\.local\.json)(/|$|\s|\"|')", re.I)
READ_CMDS = {"ls", "cat", "head", "tail", "grep", "egrep", "rg", "wc", "stat", "find", "du", "date", "file", "sort",
             "cut", "tr", "jq", "basename", "dirname", "realpath", "readlink", "pwd", "echo", "true", "diff",
             "column", "nl", "tac", "zcat"}
GIT_READ = {"log", "status", "show", "diff", "rev-parse", "branch", "ls-files", "blame", "shortlog", "describe", "tag",
            "remote", "reflog", "cat-file", "ls-tree", "whatchanged"}
FIND_BAD = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


def _real(p):
    try:
        return os.path.realpath(os.path.expanduser(p))
    except (TypeError, ValueError):
        return ""


def _allowed_edit_targets():
    return {_real(C.AUDIT_JSON)}


def check_bash(cmd):
    """None — можно; строка — причина запрета."""
    if not cmd or len(cmd) > 2000:
        return "слишком длинная или пустая команда"
    if SECRET_RE.search(cmd):
        return "секреты не читаем"
    if re.search(r"`|\$\(|<\(|>\(", cmd):
        return "подстановки команд запрещены"
    # редиректы разрешены только в /dev/null
    for m in re.finditer(r"(\d*)(>>?|&>)\s*([^\s;|&]+)", cmd):
        if m.group(3) not in ("/dev/null", "&1", "&2"):
            return "запись в файлы через > запрещена"
    parts = re.split(r"\|\||&&|;|\||\n", cmd)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            words = shlex.split(part)
        except ValueError:
            return "не разобрал команду"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            return "переменные окружения в команде запрещены"
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog == "git":
            args = words[1:]
            while args and args[0] in ("-C", "-c", "--no-pager", "--git-dir", "--work-tree"):
                if args[0] == "-c":
                    return "git -c запрещён"
                args = args[2:] if args[0] in ("-C", "--git-dir", "--work-tree") else args[1:]
            if not args or args[0] not in GIT_READ:
                return f"git {args[0] if args else ''} — только чтение (log/status/show/diff/…)"
            if args[0] in ("branch", "tag", "remote") and any(a.startswith("-") and a not in ("-a", "-v", "-vv", "-r", "--list", "-l") for a in args[1:]):
                return "git branch/tag/remote — только просмотр"
            continue
        if prog not in READ_CMDS:
            return f"«{prog}» не входит в список команд только для чтения"
        if prog == "find" and any(w in FIND_BAD for w in words):
            return "find только для поиска, без -exec/-delete"
        if prog == "sort" and any(w == "-o" or w.startswith(("--output", "-o")) for w in words[1:]):
            return "sort -o пишет в файл"
    return None


def decide(data):
    tool = data.get("tool_name") or ""
    inp = data.get("tool_input") or {}
    if tool == "Write":
        if _real(inp.get("file_path", "")) == _real(C.AUDIT_JSON):
            return None
        return f"Write разрешён только для {C.AUDIT_JSON}"
    if tool in ("Edit", "MultiEdit"):
        if _real(inp.get("file_path", "")) in _allowed_edit_targets():
            return None
        return f"Edit разрешён только для {C.AUDIT_JSON}"
    if tool == "NotebookEdit":
        return "NotebookEdit запрещён"
    if tool == "Bash":
        return check_bash(str(inp.get("command", "")))
    if tool in ("Read", "Grep", "Glob"):
        p = str(inp.get("file_path") or inp.get("path") or "") + " " + str(inp.get("pattern") or "")
        if SECRET_RE.search(p):
            return "секреты не читаем"
        return None
    if tool.startswith("mcp__") or tool in ("WebFetch", "WebSearch", "Agent", "Task", "SendMessage", "Skill"):
        return f"{tool} аудитору запрещён"
    return None


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        sys.stderr.write("Пульт: не разобрал запрос инструмента — запрещено.\n")
        sys.exit(2)
    reason = decide(data)
    if reason:
        sys.stderr.write(f"Пульт: запрещено аудитору — {reason}.\n")
        sys.exit(2)
    sys.exit(0)
