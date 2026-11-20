"""Пути и настройки пульта. Дефолты — здесь, свои значения — в ~/.config/claude-panel/config.json
(кладутся поверх дефолтов по разделам)."""
import copy
import json
import os

HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_DIR = os.path.join(HOME, ".claude")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

RUN_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "claude-panel")
BOARD_JSON = os.path.join(RUN_DIR, "board.json")
BOARD_TXT = os.path.join(RUN_DIR, "board.txt")
AUDIT_JSON = os.path.join(RUN_DIR, "audit.json")
PANEL_PID = os.path.join(RUN_DIR, "panel.pid")
PANEL_LOG = os.path.join(RUN_DIR, "panel.log")
START_LOCK = os.path.join(RUN_DIR, "start.lock")
AUDITOR_PROMPT = os.path.join(RUN_DIR, "auditor-prompt.md")
AUDITOR_SETTINGS = os.path.join(RUN_DIR, "auditor-settings.json")
DELTA_TXT = os.path.join(RUN_DIR, "delta.txt")          # новые реплики с прошлого тика — для летописи
REVIEW_DIR = os.path.join(RUN_DIR, "review")            # выжимки оборвавшихся сессий к разбору аудитором
HOOK_SEEN = os.path.join(RUN_DIR, "hook-delivered.json")  # что хук уже показал сессиям (без повторов)

# Состояние, которое должно пережить перезагрузку (/run — tmpfs, чистится): маркеры живых сессий и очередь разбора
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "claude-panel")
LIVE_DIR = os.path.join(STATE_DIR, "live")              # <sid>.json — сессия открыта (SessionStart / пульт видел живой)
QUEUE_DIR = os.path.join(STATE_DIR, "review-queue")     # <sid>.json — оборвалась, аудитор ещё не разобрал

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "claude-panel")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

AUDITOR_ENV = "CLAUDE_PANEL_AUDITOR"
AUDITOR_NAME = "auditor"
# как подписывать реплики человека в выжимках для аудитора («12:03 Пользователь: …»)
USER_LABEL = os.environ.get("CLAUDE_PANEL_USER") or "Пользователь"
# папка заметок (та же, что у vault-app): журнал сессий и летопись пульт пишет в неё
VAULT_DIR = os.environ.get("VAULT_DIR") or "~/claude-vault"

DEFAULTS = {
    "panel": {
        "width": 480,               # ширина развёрнутой панели, px
        "collapsed_width": 24,      # полоска в свёрнутом виде
        "refresh_s": 1.5,           # период опроса файлов
        "terminal_fraction": 0.42,  # доля высоты под терминал аудитора
        "terminal_min_px": 150,     # ниже терминал аудитора не опускается (и столько же — список сверху)
        "autoclose_grace_s": 60,    # сколько ждать после закрытия последней сессии
        "font_mono": "Ubuntu Sans Mono 10",
    },
    "board": {
        "tail_kb": 512,             # сколько хвоста переписки читать при первом взгляде
        "agents_recent_h": 2,       # завершённых агентов показывать за последние N часов
        "agent_stale_min": 15,      # агент «молчит» дольше — помечаем
        "repo_window_min": 20,      # правки в один репо за это окно = конфликт
        "unaccepted_min": 10,       # агент готов, а пользователь не ответил дольше N мин
        "conflict_ignore": [],      # репо, по которым не поднимать конфликт (пути)
    },
    "auditor": {
        "enabled": True,            # выключатель: кнопка в пульте и `claude-panel auditor on|off` пишут его в config.json
        "window_visible": True,     # показывать ли окно (терминал) аудитора; скрытое окно аудитора не останавливает
        "claude_bin": "~/.local/bin/claude",
        "cwd": "~",                 # рабочая папка аудитора (в ~ видит память и CLAUDE.md, как обычные сессии)
        "auto_trust_home": True,    # сам подтверждать «trust this folder» в СВОЁМ терминале (доверие к ~ не сохраняется)
        "model": None,              # None = модель по умолчанию из настроек Claude Code
        "effort": None,             # None = как в настройках Claude Code
        "tick_min": 15,             # плановая проверка (пропускается, если board не менялся)
        "event_debounce_s": 20,     # события копятся столько секунд, потом одной пачкой
        "typing_guard_s": 45,       # не вставлять тик, если пользователь недавно печатал в терминал аудитора
        "extra_args": [],
        "risky_extra": [],          # свои регулярки «рискованных шагов» поверх встроенных (auditor.RISKY)
    },
    "hook": {
        "max_lines": 10,
        "max_chars": 1000,
        "board_stale_s": 20,        # board старше — считаем сами (облегчённо)
        "facts_max": 3,             # сколько свежих неперенесённых фактов летописи подкладывать сессии
        "audit_max_age_min": 60,    # замечание аудитора старше — сессии больше не показываем
    },
    "journal": {
        "enabled": True,
        "sessions": os.path.join(VAULT_DIR, "project", "journal_sessions.md"),  # журнал сессий (пишет пульт, без ИИ)
        "auto": os.path.join(VAULT_DIR, "project", "journal_auto.md"),          # летопись фактов (факты даёт аудитор, пишет пульт)
        "archive_dir": os.path.join(VAULT_DIR, ".archive", "journals"),       # старое — сюда, по месяцам
        "max_kb": 40,               # больше — старые строки уезжают в архив
        "prompt_chars": 200,        # последний запрос пользователя в строке журнала
        "tail_kb": 512,             # сколько хвоста переписки смотреть в хуке (дальше — mmap.rfind)
        "review_kb": 150,           # хвост оборвавшейся сессии для выжимки аудитору
        "review_per_tick": 3,       # сколько оборвавшихся сессий отдавать аудитору за один тик
        "delta_kb": 8,              # предел файла «новые реплики» для тика
        "unsynced_min": 30,         # факт без [x] дольше — аудитор сверяет со столами и open_tasks
        "reap_s": 60,               # как часто пульт ищет сессии, умершие без SessionEnd
    },
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            _merge(cfg, json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def set_option(section, key, value):
    """Записать одно своё значение в config.json; остальное в файле не трогаем.
    Битый файл не перезаписываем — ValueError уйдёт наверх."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.setdefault(section, {}), dict):
        raise ValueError(f"{CONFIG_FILE}: раздел {section} — не объект")
    data[section][key] = value
    os.makedirs(CONFIG_DIR, exist_ok=True)
    write_atomic(CONFIG_FILE, json.dumps(data, ensure_ascii=False, indent=1) + "\n", mode=0o644)


def auditor_enabled(cfg):
    return bool(cfg["auditor"].get("enabled", True))


def auditor_window_shown(cfg):
    """Окно (терминал) аудитора на экране: аудитор включён и окно не скрыто кнопкой."""
    return auditor_enabled(cfg) and bool(cfg["auditor"].get("window_visible", True))


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def ensure_run_dir():
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)


def slug_for_cwd(cwd):
    # так Claude Code именует папку проекта: всё, кроме [A-Za-z0-9], → «-»
    return "".join(c if (c.isascii() and c.isalnum()) else "-" for c in cwd)


def jpath(cfg, key):
    """Путь из раздела journal (с ~)."""
    return os.path.expanduser(cfg["journal"][key])


def write_atomic(path, data, mode=0o600):
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
