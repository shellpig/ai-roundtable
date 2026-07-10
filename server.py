# ai-roundtable: 多 AI 圓桌討論室（localhost 單機工具）
# 參與者 = CLI 呼叫配方；逐字稿(jsonl + md 鏡像)是唯一資料層。
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
TRANSCRIPT = DATA / "transcript.jsonl"
MD_MIRROR = DATA / "roundtable.md"
SETTINGS = DATA / "settings.json"
SESSIONS = DATA / "sessions.json"

DEFAULT_PROJECT_DIR = os.environ.get("AI_ROUNDTABLE_PROJECT_DIR", str(ROOT))
UNNAMED_TITLE = "未命名會議"
AGY_EXE = os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe")
CODEX_CMD = os.path.expandvars(r"%APPDATA%\npm\codex.cmd")
DS_CODEX_HOME = os.environ.get("AI_ROUNDTABLE_DS_CODEX_HOME", str(ROOT / ".codex-deepseek-home"))

PORT = 8787
CALL_TIMEOUT = 600  # seconds per AI call
MAX_AUTO_ROUNDS = 2
INVITE_TTL_SECONDS = 180
SESSION_COOKIE = "ai_roundtable_session"

# 公開 demo 模式：對外經 Tailscale Funnel 曝露時開啟。
# 開啟後不再因來源是 loopback 就自動給 HOST（Funnel 轉發的公網流量在本機也是 127.0.0.1）。
PUBLIC_MODE = os.environ.get("AI_ROUNDTABLE_PUBLIC") == "1"
PUBLIC_URL = (os.environ.get("AI_ROUNDTABLE_PUBLIC_URL") or "").strip().rstrip("/")

# DNS-rebinding 防護：只放行白名單內的 HTTP Host 主機名（忽略 port）。
# 伺服器綁 127.0.0.1（及選擇性的 Tailscale IP），且非 PUBLIC 模式會對 loopback 來源
# 自動給 HOST。惡意網站可用 DNS rebinding（evil.com → 127.0.0.1）從瀏覽器對
# 127.0.0.1:PORT 發出帶 cookie 的請求，這類請求來源仍是 loopback、會被自動當 HOST；
# 但 rebinding 請求的 Host header 仍是 evil.com，故驗證主機名即可擋下。
# main() 啟動時會依實際 runtime 值（Tailscale IP、公網 Funnel 名稱）擴充此集合。
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 開發模式：只能由啟動層進入（start_devmode.cmd 設 AI_ROUNDTABLE_DEVMODE=1）。
# 執行期沒有任何切換模式的 API 或 UI——可寫權限烙在啟動時組出的 CLI 參數裡，
# 避免執行期請求（含 prompt injection）翻轉唯讀圓桌。與 PUBLIC_MODE 互斥（見 main()）。
DEVMODE = os.environ.get("AI_ROUNDTABLE_DEVMODE") == "1"
DEV_CALL_TIMEOUT = int(os.environ.get("AI_ROUNDTABLE_DEV_TIMEOUT", "3600"))   # 開發模式單棒上限（秒）
DEV_MAX_TURNS = int(os.environ.get("AI_ROUNDTABLE_DEV_MAX_TURNS", "40"))      # 整場總棒數上限
DEV_MAX_ATTEMPTS = 3            # 單任務重試上限（規格 §5）
DEV_BRANCH_PREFIX = "roundtable/dev-"
TASKS_FILE = DATA / "tasks.json"
MEETING_SUMMARY_FILE = DATA / "meeting_summary.json"
DEVLOG_DIR = DATA / "devlogs"
RATE_LIMIT_MARKERS = ("rate limit", "usage limit", "429", "quota", "usage_limit")  # 小寫比對

# 開發模式輸出協議（規格書 §6）：prompt 模板與解析器共用同一份常數，不得各寫一份字面值。
JSON_FENCE_OPEN = "```json"
JSON_FENCE_CLOSE = "```"
VERDICT_PASS = "驗證結果：通過"
VERDICT_FAIL_PREFIX = "驗證結果：不通過—"
ARBITRATION_REASSIGN_PREFIX = "仲裁：重派—"
ARBITRATION_SKIP_PREFIX = "仲裁：跳過—"
ARBITRATION_ASK_PREFIX = "仲裁：詢問—"
PROTOCOL_RETRY_NOTE = "上次輸出不符格式，請重新輸出。"

# 每席位的可選模型；label 顯示在 UI 與訊息氣泡，其餘欄位由各 adapter 解讀。
PARTICIPANTS = {
    "codex": {
        "display": "Codex", "color": "#10a37f",
        "models": [
            {"label": "GPT-5.5 (High)", "model": "gpt-5.5", "effort": "high"},
            {"label": "GPT-5.5 (Medium)", "model": "gpt-5.5", "effort": "medium"},
            {"label": "GPT-5.5 (Low)", "model": "gpt-5.5", "effort": "low"},
        ],
    },
    "agy": {
        "display": "agy", "color": "#4c8bf5",
        "models": [{"label": m, "model": m} for m in [
            "Gemini 3.5 Flash (High)", "Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash (Low)",
            "Gemini 3.1 Pro (High)", "Gemini 3.1 Pro (Low)",
            "Claude Sonnet 4.6 (Thinking)", "Claude Opus 4.6 (Thinking)",
            "GPT-OSS 120B (Medium)",
        ]],
    },
    "ds": {
        "display": "DS", "color": "#9b6bff",
        "models": [
            {"label": "DeepSeek V4 Pro", "model": "deepseek-v4-pro"},
            {"label": "DeepSeek V4 Flash", "model": "deepseek-v4-flash"},
        ],
    },
    "claude": {
        "display": "Claude", "color": "#d97757",
        # effort 值域（由 CLI 驗證）：low / medium / high / xhigh / max
        "models": [
            {"label": "Opus 4.8 (High)", "model": "opus", "effort": "high"},
            {"label": "Opus 4.8 (Max)", "model": "opus", "effort": "max"},
            {"label": "Opus 4.8 (xHigh)", "model": "opus", "effort": "xhigh"},
            {"label": "Opus 4.8 (Medium)", "model": "opus", "effort": "medium"},
            {"label": "Opus 4.8 (Low)", "model": "opus", "effort": "low"},
            {"label": "Sonnet 5 (High)", "model": "sonnet", "effort": "high"},
            {"label": "Sonnet 5 (Medium)", "model": "sonnet", "effort": "medium"},
            {"label": "Sonnet 5 (Low)", "model": "sonnet", "effort": "low"},
            {"label": "Haiku 4.5 (High)", "model": "haiku", "effort": "high"},
            {"label": "Haiku 4.5 (Low)", "model": "haiku", "effort": "low"},
        ],
    },
}

DEFAULT_SELECTIONS = {
    "codex": "GPT-5.5 (High)",
    "agy": "Gemini 3.5 Flash (High)",
    "ds": "DeepSeek V4 Pro",
    "claude": "Opus 4.8 (High)",
}

# 開發模式角色→席位對應；v1 只由設定檔調整，UI 只顯示不提供編輯。
DEFAULT_DEV_ROLES = {"controller": "claude", "implementer": "agy", "verifier": "codex"}

_lock = threading.Lock()
_messages = []
_busy = {}  # name -> started_ts
_processes = {}  # name -> subprocess.Popen
_cancel_requested = set()
_selected = {}  # name -> label
_project_dir = DEFAULT_PROJECT_DIR
_active_session = {"title": UNNAMED_TITLE, "created_at": None}
_discussion = {"active": False, "round": 0, "max_rounds": 0}  # 共識討論模式的即時狀態（不持久化）
_auth_sessions = {}  # session_id -> {"role": "host"|"guest", "name": str, ...}
_invites = {}  # token -> {"role", "expires_at"}
_host_bootstrap_token = None  # PUBLIC 模式下印在 console 的長效 HOST 進場碼（僅本機使用）
_batch_watermark = {}  # seat name -> latest human message number successfully handled
_enabled_seats = []  # persisted AI seats that auto-answer any human message (host or guest)
_batch_auto_rounds = 0
_batch_blocked = False
_dev_roles = DEFAULT_DEV_ROLES.copy()  # devmode 角色→席位對應（persisted；管線由 D2 消費）

# 開發管線狀態（模組級，比照 _discussion；不持久化——斷點續作靠 tasks.json）。
# pause_reason: manual/interject/rate_limit/tamper/parse_fail/seat_error/turn_cap/crash/ask_host
_dev = {
    "active": False, "paused": False, "pause_reason": "",
    "stage": "", "current_task": None, "turn_count": 0, "branch": "",
}
_dev_pause_requested = False  # HOST 按「⏸」的請求旗標；當前棒跑完後生效
_last_run = {}  # seat name -> 最近一次 _run_process 呼叫的 args/stdout/stderr/returncode/elapsed（管線稽核用）
_data_hashes = {}  # str(path) -> sha256；伺服器自身寫入 TRANSCRIPT/MD_MIRROR/TASKS_FILE 後更新，供竄改偵測比對
_trusted_tasks = None  # 最近一次伺服器成功落盤的 tasks.json 快照；竄改時不得信任磁碟上的內容
_trusted_meeting_summary = None


class CallCancelled(Exception):
    pass


class _RateLimited(Exception):
    """席位呼叫命中限流特徵；管線層須據此暫停而不計入任務重試次數。"""


class _PipelinePaused(Exception):
    """安全閘門已落盤暫停；上層只需停止 pipeline，不得改寫 pause_reason。"""


def _is_loopback(ip):
    return ip == "::1" or ip.startswith("127.")


def _host_hostname(host):
    # 從 HTTP Host header 取出主機名（小寫），忽略 port。回傳 None 代表無法解析／缺 Host。
    # 需正確處理：bare hostname、host:port、IPv6 loopback 的 ::1 與 [::1]:port。
    host = (host or "").strip()
    if not host:
        return None
    if host.startswith("["):  # bracketed IPv6, e.g. [::1] 或 [::1]:8787
        end = host.find("]")
        if end == -1:
            return None
        return host[1:end].lower()
    if host.count(":") > 1:  # 不含中括號但有多個冒號 → bare IPv6 literal（如 ::1），無 port
        return host.lower()
    return host.split(":", 1)[0].lower()  # bare hostname 或 host:port


def _host_allowed(host):
    name = _host_hostname(host)
    return name is not None and name in _ALLOWED_HOSTS


def _clean_name(name, fallback):
    name = (name or "").strip()
    return (name or fallback)[:40]


def _new_auth_session(role, name):
    sid = secrets.token_urlsafe(32)
    now = time.time()
    _auth_sessions[sid] = {
        "role": role,
        "name": _clean_name(name, "HOST" if role == "host" else "Guest"),
        "created_at": now,
        "last_seen": now,
    }
    return sid, _auth_sessions[sid]


def _cookie_header(sid):
    return f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax"


def _read_cookie_sid(headers):
    raw = headers.get("Cookie") or ""
    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def _session_payload(session):
    if not session:
        return None
    return {"role": session["role"], "name": session["name"]}


def _clean_invites(now=None):
    now = now or time.time()
    expired = [token for token, invite in _invites.items() if invite["expires_at"] <= now]
    for token in expired:
        _invites.pop(token, None)


def _create_invite(role):
    if role not in {"host", "guest"}:
        return None
    _clean_invites()
    token = secrets.token_urlsafe(24)
    _invites[token] = {"role": role, "expires_at": time.time() + INVITE_TTL_SECONDS}
    return token, _invites[token]


def _redeem_invite(token, name):
    token = (token or "").strip()
    # HOST 進場碼可重複使用（僅印在本機 console，不經 API 外流），讓操作者掉 cookie 也能重進。
    if _host_bootstrap_token and token == _host_bootstrap_token:
        return _new_auth_session("host", name)
    _clean_invites()
    invite = _invites.pop(token, None)
    if not invite:
        return None
    return _new_auth_session(invite["role"], name)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _norm_path(path):
    return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))))


def _same_path(a, b):
    return _norm_path(a) == _norm_path(b)


def _read_json(path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return fallback


def _write_atomic(path, text):
    # tasks.json 的唯一寫入管道：同目錄暫存檔 + os.replace，任何時點檔案內容都是完整合法 JSON。
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _record_hash(path)


def _hash_file(path):
    path = Path(path)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_hash(path):
    _data_hashes[str(Path(path))] = _hash_file(path)


def _check_tamper():
    # 竄改偵測：伺服器自己管理的檔案，每棒開始前重算雜湊比對上次自身寫入後記錄的值。
    managed = (TRANSCRIPT, MD_MIRROR, TASKS_FILE, MEETING_SUMMARY_FILE)
    return all(_data_hashes.get(str(Path(p))) == _hash_file(p) for p in managed)


def _default_settings():
    return {
        "project_dir": DEFAULT_PROJECT_DIR,
        "participants": DEFAULT_SELECTIONS.copy(),
        "enabled_seats": _default_enabled_seats(),
        "active_session": {"title": UNNAMED_TITLE, "created_at": _now()},
        "dev_roles": DEFAULT_DEV_ROLES.copy(),
    }


def _valid_dev_roles(raw):
    # 驗證值必須是 PARTICIPANTS 的 key 且三角色互異，非法時回落預設。
    if not isinstance(raw, dict):
        return DEFAULT_DEV_ROLES.copy()
    candidate = {role: raw.get(role) for role in DEFAULT_DEV_ROLES}
    seats = list(candidate.values())
    if all(seat in PARTICIPANTS for seat in seats) and len(set(seats)) == len(seats):
        return candidate
    return DEFAULT_DEV_ROLES.copy()


def _coerce_settings(raw):
    settings = _default_settings()
    if not isinstance(raw, dict):
        return settings

    # Legacy schema: {"codex": "...", "agy": "...", ...}
    if "participants" not in raw:
        legacy = {name: raw.get(name) for name in PARTICIPANTS}
        settings["participants"].update({k: v for k, v in legacy.items() if isinstance(v, str)})
        settings["participants"]["ds"] = DEFAULT_SELECTIONS["ds"]
        return settings

    if isinstance(raw.get("project_dir"), str) and raw["project_dir"].strip():
        settings["project_dir"] = raw["project_dir"].strip()
    if isinstance(raw.get("participants"), dict):
        settings["participants"].update(raw["participants"])
    if isinstance(raw.get("enabled_seats"), list):
        settings["enabled_seats"] = _normalize_seats(raw["enabled_seats"])
    if isinstance(raw.get("active_session"), dict):
        settings["active_session"].update(raw["active_session"])
    settings["dev_roles"] = _valid_dev_roles(raw.get("dev_roles"))
    return settings


def _load_settings():
    global _project_dir, _active_session, _enabled_seats, _dev_roles
    settings = _coerce_settings(_read_json(SETTINGS, {}))
    project = settings.get("project_dir") or DEFAULT_PROJECT_DIR
    _project_dir = project if Path(project).is_dir() else DEFAULT_PROJECT_DIR

    for name, p in PARTICIPANTS.items():
        labels = [m["label"] for m in p["models"]]
        saved = settings["participants"].get(name)
        default = DEFAULT_SELECTIONS.get(name, labels[0])
        _selected[name] = saved if saved in labels else default

    _enabled_seats = _normalize_seats(settings.get("enabled_seats"))
    _dev_roles = _valid_dev_roles(settings.get("dev_roles"))

    active = settings.get("active_session") or {}
    _active_session = {
        "title": (active.get("title") or UNNAMED_TITLE).strip() or UNNAMED_TITLE,
        "created_at": active.get("created_at") or _now(),
    }
    _save_settings()


def _save_settings():
    payload = {
        "project_dir": _project_dir,
        "participants": {name: _selected[name] for name in PARTICIPANTS},
        "enabled_seats": list(_enabled_seats),
        "active_session": {
            "title": _active_session.get("title") or UNNAMED_TITLE,
            "created_at": _active_session.get("created_at") or _now(),
        },
        "dev_roles": dict(_dev_roles),
    }
    SETTINGS.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _load_sessions():
    data = _read_json(SESSIONS, [])
    return data if isinstance(data, list) else []


def _save_sessions(sessions):
    SESSIONS.write_text(json.dumps(sessions, ensure_ascii=False, indent=1), encoding="utf-8")


def _session_title():
    return (_active_session.get("title") or UNNAMED_TITLE).strip() or UNNAMED_TITLE


def _set_session_title(title):
    _active_session["title"] = (title or "").strip() or UNNAMED_TITLE
    _save_settings()


def _title_from_text(text):
    title = " ".join((text or "").strip().split())
    if not title:
        return UNNAMED_TITLE
    return title[:34] + "..." if len(title) > 34 else title


def _message_role(msg):
    role = msg.get("role")
    if role in {"host", "guest", "ai", "system"}:
        return role
    speaker = msg.get("speaker")
    if speaker == "你":
        return "host"
    if speaker == "system":
        return "system"
    if speaker in PARTICIPANTS:
        return "ai"
    return "guest"


def _message_name(msg):
    name = (msg.get("name") or "").strip()
    if name:
        return name
    speaker = msg.get("speaker")
    if speaker in PARTICIPANTS:
        return PARTICIPANTS[speaker]["display"]
    if speaker == "你":
        return "你"
    return speaker or "unknown"


def _is_host_message(msg):
    return _message_role(msg) == "host"


def _is_human_message(msg):
    return _message_role(msg) in {"host", "guest"}


def _latest_human_no():
    latest = 0
    for i, msg in enumerate(_messages, 1):
        if _is_human_message(msg):
            latest = i
    return latest


def _reset_batch_watermarks():
    latest = _latest_human_no()
    _batch_watermark.clear()
    for name in ADAPTERS:
        _batch_watermark[name] = latest


def _reset_batch_state():
    # Per-session batch progress only; enabled seats are a persisted preference.
    global _batch_auto_rounds, _batch_blocked
    _batch_auto_rounds = 0
    _batch_blocked = False
    _reset_batch_watermarks()


def _human_range_label(start_no, end_no):
    return f"[{start_no}]" if start_no == end_no else f"[{start_no}-{end_no}]"


def _archive_title():
    title = _session_title()
    if title != UNNAMED_TITLE:
        return title
    for msg in _messages:
        if _is_host_message(msg):
            return _title_from_text(msg.get("text"))
    return UNNAMED_TITLE


def _option(name):
    for m in PARTICIPANTS[name]["models"]:
        if m["label"] == _selected[name]:
            return m
    return PARTICIPANTS[name]["models"][0]


def _load():
    _messages.clear()
    if TRANSCRIPT.exists():
        for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                _messages.append(json.loads(line))
    _reset_batch_state()
    _record_hash(TRANSCRIPT)
    _record_hash(MD_MIRROR)
    _remember_loaded_meeting_summary(_load_meeting_summary())


def _rebuild_md():
    parts = ["# 圓桌會議逐字稿", ""]
    for i, m in enumerate(_messages, 1):
        disp = _message_name(m)
        parts.append(f"## [{i}] {disp}")
        parts.append("")
        parts.append(m["text"])
        parts.append("")
    MD_MIRROR.write_text("\n".join(parts), encoding="utf-8")


def _unique_archive_paths(stamp):
    suffix = stamp
    counter = 2
    while True:
        transcript = DATA / f"transcript-{suffix}.jsonl"
        mirror = DATA / f"roundtable-{suffix}.md"
        tasks = DATA / f"tasks-{suffix}.json"
        summary = DATA / f"meeting-summary-{suffix}.json"
        if not transcript.exists() and not mirror.exists() and not tasks.exists() and not summary.exists():
            return suffix, transcript, mirror, tasks, summary
        suffix = f"{stamp}-{counter}"
        counter += 1


def _archive_active_session(reset_title=True):
    global _active_session, _trusted_tasks, _trusted_meeting_summary
    if not _messages:
        if reset_title:
            _active_session = {"title": UNNAMED_TITLE, "created_at": _now()}
            _save_settings()
        _reset_batch_state()
        return None

    sid, archived_transcript, archived_mirror, archived_tasks, archived_summary = _unique_archive_paths(_stamp())
    if TRANSCRIPT.exists():
        TRANSCRIPT.replace(archived_transcript)
    else:
        with archived_transcript.open("w", encoding="utf-8") as f:
            for msg in _messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    if MD_MIRROR.exists():
        MD_MIRROR.replace(archived_mirror)
    else:
        _rebuild_md()
        MD_MIRROR.replace(archived_mirror)

    tasks_archived = TASKS_FILE.exists()  # 開發模式的管線狀態隨會議一起封存（規格 §7）
    if tasks_archived:
        TASKS_FILE.replace(archived_tasks)
    summary_archived = MEETING_SUMMARY_FILE.exists()
    if summary_archived:
        MEETING_SUMMARY_FILE.replace(archived_summary)

    entry = {
        "id": sid,
        "project_dir": _project_dir,
        "title": _archive_title(),
        "created_at": _active_session.get("created_at") or "",
        "archived_at": _now(),
        "message_count": len(_messages),
        "transcript_path": archived_transcript.name,
        "mirror_path": archived_mirror.name,
        "tasks_path": archived_tasks.name if tasks_archived else None,
        "meeting_summary_path": archived_summary.name if summary_archived else None,
    }
    sessions = _load_sessions()
    sessions.append(entry)
    _save_sessions(sessions)
    _messages.clear()
    _trusted_tasks = None
    _trusted_meeting_summary = None
    for managed in (TRANSCRIPT, MD_MIRROR, TASKS_FILE, MEETING_SUMMARY_FILE):
        _record_hash(managed)
    _reset_batch_state()
    if reset_title:
        _active_session = {"title": UNNAMED_TITLE, "created_at": _now()}
    _save_settings()
    return entry


def _restore_session(entry):
    global _project_dir, _active_session
    _archive_active_session(reset_title=False)
    transcript = DATA / entry["transcript_path"]
    mirror = DATA / entry["mirror_path"]
    if not transcript.exists() or not mirror.exists():
        raise FileNotFoundError("歸檔逐字稿檔案不存在")
    transcript.replace(TRANSCRIPT)
    mirror.replace(MD_MIRROR)
    tasks_name = entry.get("tasks_path")
    if tasks_name:
        tasks_archive = DATA / tasks_name
        if tasks_archive.exists():
            tasks_archive.replace(TASKS_FILE)
    summary_name = entry.get("meeting_summary_path")
    if summary_name:
        summary_archive = DATA / summary_name
        if summary_archive.exists():
            summary_archive.replace(MEETING_SUMMARY_FILE)
    _project_dir = entry.get("project_dir") or _project_dir
    _active_session = {
        "title": entry.get("title") or UNNAMED_TITLE,
        "created_at": entry.get("created_at") or _now(),
    }
    sessions = [s for s in _load_sessions() if s.get("id") != entry.get("id")]
    _save_sessions(sessions)
    _save_settings()
    _load()
    restored_tasks = _read_json(TASKS_FILE, None)
    if restored_tasks is not None:
        _remember_loaded_tasks(restored_tasks)
    _remember_loaded_meeting_summary(_load_meeting_summary())


def append_message(speaker, text, sub=None, role=None, name=None):
    msg = {"speaker": speaker, "text": text.strip(), "ts": _now()}
    msg["role"] = role or _message_role(msg)
    msg["name"] = _clean_name(name, _message_name(msg))
    if sub:
        msg["sub"] = sub
    with _lock:
        if not _messages and _is_host_message(msg) and _session_title() == UNNAMED_TITLE:
            _active_session["title"] = _title_from_text(text)
            _save_settings()
        _messages.append(msg)
        with TRANSCRIPT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        _rebuild_md()
        _record_hash(TRANSCRIPT)
        _record_hash(MD_MIRROR)
    return msg


def _instruction(name, phase=None):
    # phase: None=單則回應（現況）; "first"=討論模式首輪（獨立陳述）; "debate"=討論模式辯論輪
    disp = PARTICIPANTS[name]["display"]
    head = (
        f"你是多 AI 圓桌規格討論會的參與者「{disp}」。\n"
        f"1. 先讀取逐字稿檔案（UTF-8）：{MD_MIRROR} —— 這是到目前為止的完整討論。\n"
        f"2. 討論主題通常圍繞位於 {_project_dir} 的專案，你可以唯讀查閱專案檔案來佐證論點。\n"
    )
    tail = "用繁體中文，發言精煉聚焦。絕對不要建立、修改或刪除任何檔案。\n"
    if phase == "first":
        return head + (
            f"3. 這是「共識討論模式」的第一輪。此刻其他席位尚未針對本題發言、你看不到他們的意見。"
            f"請以「{disp}」的身分，針對逐字稿最後主持人拋出的命題，獨立提出你的初始立場、你看到的風險、"
            f"以及你偏好的方案。不要回應或揣測其他席位的意見，也不要在結尾加任何結論傾向標記。"
            f"直接輸出發言內容本身，不要任何前綴、署名、標題或 markdown 程式碼圍欄。\n"
            f"4. {tail}"
        )
    if phase == "debate":
        return head + (
            f"3. 這是「共識討論模式」的辯論輪。請回顧逐字稿中至今所有發言，以「{disp}」的身分針對彼此意見的"
            f"「分歧點」回應：可反駁、修正、或讓步；不要重述別人已講過的論點，沒有新論點就直接表示同意。"
            f"反附和原則：只有你真心同意目前方案、且它滿足你所代表模型的技術考量時，才標記共識；"
            f"只要還有疑慮，就把「具體還沒解決的點」寫出來，不要為了收斂而附和。\n"
            f"4. 發言最後一行必須是結論標記，二選一（照抄格式，不要多加標點）：\n"
            f"結論傾向：共識\n"
            f"結論傾向：保留—<一句話寫出你具體還沒解決的點>\n"
            f"5. 直接輸出發言內容本身，不要任何前綴、署名、標題或 markdown 程式碼圍欄。{tail}"
        )
    return head + (
        f"3. 針對逐字稿最後的最新發言，以「{disp}」的身分發表一則回應：同意就說為什麼、"
        f"反對就給理由與替代方案、看到風險就指出來。直接輸出發言內容本身，"
        f"不要任何前綴、署名、標題或 markdown 程式碼圍欄。\n"
        f"4. {tail}"
    )


def _find_claude():
    # 桌面 app (MSIX) 打包的 CLI；版本資料夾會隨更新換號，取最新版。
    base = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code"))
    candidates = list(base.glob("*/claude.exe")) if base.exists() else []

    def ver_key(p):
        return [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")]

    return str(max(candidates, key=ver_key)) if candidates else None


def _terminate_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception:  # noqa: BLE001 - fallback to Popen termination below
            pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - final best-effort kill
            try:
                proc.kill()
            except Exception:
                pass


def _run_process(name, args, *, input_text=None, stdin=None, cwd=None, env=None, timeout=CALL_TIMEOUT):
    started = time.time()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_text is not None else stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        shell=False,
    )
    with _lock:
        _processes[name] = proc
    try:
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            _last_run[name] = {"args": args, "stdout": stdout, "stderr": stderr,
                                "returncode": proc.returncode, "elapsed": time.time() - started}
            raise
        # 開發管線的稽核日誌需要完整 stdout/stderr/exit code/耗時；這裡是唯一能拿到
        # 完整 CompletedProcess 的地方（上層 _call_* 之後只回傳處理過的文字或拋例外）。
        _last_run[name] = {"args": args, "stdout": stdout, "stderr": stderr,
                            "returncode": proc.returncode, "elapsed": time.time() - started}
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    finally:
        with _lock:
            if _processes.get(name) is proc:
                _processes.pop(name, None)
            cancelled = name in _cancel_requested
            if cancelled:
                _cancel_requested.discard(name)
        if cancelled:
            raise CallCancelled()




def _batch_instruction(name, start_no, end_no):
    disp = PARTICIPANTS[name]["display"]
    return (
        f"You are the AI roundtable participant named {disp}.\n"
        f"1. Read the full UTF-8 transcript first: {MD_MIRROR}.\n"
        f"2. The discussion usually concerns this project directory: {_project_dir}. Inspect it read-only if needed.\n"
        f"3. For this batch, handle only human messages in transcript number range {_human_range_label(start_no, end_no)}. "
        f"AI and system messages inside that range are context only.\n"
        f"4. Reply in Traditional Chinese. For each human message that needs an answer, start the section with: "
        f"Reply name[number]: . If a human message needs no answer, write: Skip name[number]: reason.\n"
        f"5. You may answer multiple humans in one output. Be concise. Never create, modify, or delete files."
    )


def _unavailable_usage():
    return {
        "source": "unavailable", "input_tokens": None, "cached_input_tokens": None,
        "output_tokens": None, "total_tokens": None, "cost_usd": None,
    }


def _adapter_result(text, usage=None, session_id=None, *, resumed=False, resume_failed=False,
                    persistence_fallback=False):
    return {
        "text": text,
        "usage": usage or _unavailable_usage(),
        "session": {
            "id": session_id, "resumed": resumed, "resume_failed": resume_failed,
            "persistence_fallback": persistence_fallback,
        },
    }


def _codex_json_result(stdout, *, resumed=False, persistence_fallback=False):
    text = ""
    session_id = None
    usage = None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
        elif kind == "turn.completed" and isinstance(event.get("usage"), dict):
            raw = event["usage"]
            input_total = raw.get("input_tokens") if isinstance(raw.get("input_tokens"), int) else None
            cached = raw.get("cached_input_tokens") if isinstance(raw.get("cached_input_tokens"), int) else None
            output = raw.get("output_tokens") if isinstance(raw.get("output_tokens"), int) else None
            uncached = max(input_total - cached, 0) if input_total is not None and cached is not None else input_total
            total = input_total + output if input_total is not None and output is not None else None
            usage = {
                "source": "cli_json", "input_tokens": uncached, "cached_input_tokens": cached,
                "output_tokens": output, "total_tokens": total, "cost_usd": None,
            }
    if not text:
        raise RuntimeError("codex JSONL 沒有 agent_message 最終回覆")
    return _adapter_result(
        text, usage, None if persistence_fallback else session_id,
        resumed=resumed, persistence_fallback=persistence_fallback,
    )


def _claude_json_result(stdout, *, resumed=False):
    try:
        payload = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("claude JSON 輸出無法解析") from exc
    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("claude JSON 沒有 result 最終回覆")
    raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    input_tokens = raw.get("input_tokens") if isinstance(raw.get("input_tokens"), int) else None
    cached_values = [raw.get("cache_creation_input_tokens"), raw.get("cache_read_input_tokens")]
    cached = sum(v for v in cached_values if isinstance(v, int)) if any(isinstance(v, int) for v in cached_values) else None
    output_tokens = raw.get("output_tokens") if isinstance(raw.get("output_tokens"), int) else None
    total = None
    if input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens + (cached or 0)
    cost = payload.get("total_cost_usd")
    usage = {
        "source": "cli_json" if raw or isinstance(cost, (int, float)) else "unavailable",
        "input_tokens": input_tokens, "cached_input_tokens": cached,
        "output_tokens": output_tokens, "total_tokens": total,
        "cost_usd": cost if isinstance(cost, (int, float)) else None,
    }
    return _adapter_result(text.strip(), usage, payload.get("session_id"), resumed=resumed)


def _call_codex(name, instr, opt, env_extra=None, dev_role=None, timeout=CALL_TIMEOUT, session_id=None):
    # dev_role 保留給開發管線分流用：驗證棒沿用討論版的 read-only 沙箱，不需要另外分支。
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    if dev_role:
        args = [CODEX_CMD, "exec"]
        if session_id:
            args += ["resume", session_id, "-"]
            args += ["--skip-git-repo-check", "--json", "-m", opt["model"]]
        else:
            args += ["-"]
            args += ["--sandbox", "read-only", "--skip-git-repo-check", "-C", _project_dir,
                     "--json", "--color", "never", "-m", opt["model"]]
            if dev_role == "verifier":
                args += ["--ephemeral"]
        if opt.get("effort"):
            args += ["-c", f"model_reasoning_effort={opt['effort']}"]
        proc = _run_process(name, args, input_text=instr, env=env, cwd=_project_dir, timeout=timeout)
        persistence_fallback = False
        if proc.returncode != 0:
            blob = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
            persistence_error = (
                dev_role != "verifier"
                and "failed to record rollout items" in blob
                and "thread" in blob
                and "not found" in blob
                and not any(marker in blob for marker in RATE_LIMIT_MARKERS)
            )
            if not persistence_error:
                raise RuntimeError(f"codex exit={proc.returncode}。stderr：" + (proc.stderr or "")[-500:])
            fallback_args = [*args, "--ephemeral"]
            proc = _run_process(
                name, fallback_args, input_text=instr, env=env, cwd=_project_dir, timeout=timeout)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex persistence fallback exit={proc.returncode}。stderr："
                    + (proc.stderr or "")[-500:]
                )
            persistence_fallback = True
        return _codex_json_result(
            proc.stdout, resumed=bool(session_id), persistence_fallback=persistence_fallback)

    args = [CODEX_CMD, "exec", "-", "--sandbox", "read-only", "--skip-git-repo-check",
            "-C", _project_dir, "--ephemeral", "--color", "never", "-m", opt["model"]]
    if opt.get("effort"):
        args += ["-c", f"model_reasoning_effort={opt['effort']}"]
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False, encoding="utf-8") as tf:
        out_path = tf.name
    args += ["-o", out_path]
    try:
        proc = _run_process(name, args, input_text=instr, env=env, timeout=timeout)
        result = Path(out_path).read_text(encoding="utf-8", errors="replace").strip()
        if not result:
            tail = (proc.stdout or "").strip().splitlines()[-15:]
            err_tail = (proc.stderr or "").strip().splitlines()[-15:]
            raise RuntimeError(
                "codex 沒有輸出最終回覆。"
                "\nstdout 尾段：\n" + "\n".join(tail) +
                "\nstderr 尾段：\n" + "\n".join(err_tail)
            )
        return result
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _call_agy(name, instr, opt, dev_role=None, timeout=CALL_TIMEOUT, session_id=None):
    # 不使用 --dangerously-skip-permissions（會讓 agy 取得主機完全存取權，且會讓
    # --sandbox 失效）。改用 --sandbox：agy 目前沒有真正的唯讀/plan 模式可用於
    # 非互動 -p 執行（上游尚未支援，見 google-antigravity/antigravity-cli#45），
    # --sandbox 僅限制 shell/終端機工具，無法阻擋檔案寫入類工具──這是目前 agy
    # 所能提供的最大限制，並非完整唯讀保證。
    # 開發管線的實作棒（dev_role == "implementer"）是唯一有筆的席位：改用
    # --dangerously-skip-permissions 換取真正的可寫權限，殘餘風險見開發模式規格書 §9。
    args = [
        AGY_EXE, "-p", instr, "--model", opt["model"],
        "--add-dir", _project_dir, "--add-dir", str(DATA),
    ]
    if dev_role == "implementer":
        args += ["--dangerously-skip-permissions"]
    else:
        args += ["--sandbox"]
    args += ["--print-timeout", f"{timeout - 60}s"]
    proc = _run_process(name, args, stdin=subprocess.DEVNULL, timeout=timeout)
    result = (proc.stdout or "").strip()
    if not result:
        raise RuntimeError("agy 沒有輸出。stderr：" + (proc.stderr or "")[-500:])
    return _adapter_result(result) if dev_role else result


def _call_claude(name, instr, opt, dev_role=None, timeout=CALL_TIMEOUT, session_id=None):
    # dev_role 保留給開發管線分流用：主控棒沿用討論版的唯讀 allowedTools，不需要另外分支。
    exe = _find_claude()
    if not exe:
        raise RuntimeError("找不到 claude.exe（桌面 app 的 claude-code 資料夾不存在？）")
    # 僅允許唯讀的本機檢視工具。刻意排除 WebSearch/WebFetch：若 Claude 席位遭
    # prompt injection，這兩個工具可把本機專案內容外洩到攻擊者控制的網址；此席位
    # 的工作只需唯讀檢視本機專案，不需要網路存取（尤其此工具可經 Tailscale Funnel
    # 公開曝露）。
    args = [exe, "-p", instr, "--model", opt["model"],
            "--allowedTools", "Read,Glob,Grep", "--add-dir", str(DATA)]
    if dev_role:
        args += ["--output-format", "json"]
        if session_id:
            args += ["--resume", session_id]
        elif dev_role == "verifier":
            args += ["--no-session-persistence"]
    if opt.get("effort"):
        args += ["--effort", opt["effort"]]
    proc = _run_process(name, args, cwd=_project_dir, stdin=subprocess.DEVNULL, timeout=timeout)
    result = (proc.stdout or "").strip()
    if proc.returncode != 0 or not result:
        raise RuntimeError(f"claude exit={proc.returncode}。stderr：" + (proc.stderr or "")[-500:])
    return _claude_json_result(result, resumed=bool(session_id)) if dev_role else result


ADAPTERS = {
    # codex/ds 的 lambda 要轉發 dev_role/timeout，開發管線的驗證棒（走 codex）才吃得到
    # DEV_CALL_TIMEOUT；一般 ask() 路徑不帶這兩個關鍵字，沿用預設值，討論行為不變。
    "codex": lambda name, instr, opt, dev_role=None, timeout=CALL_TIMEOUT, session_id=None: _call_codex(
        name, instr, opt, dev_role=dev_role, timeout=timeout, session_id=session_id),
    "ds": lambda name, instr, opt, dev_role=None, timeout=CALL_TIMEOUT, session_id=None: _call_codex(
        name, instr, opt, {"CODEX_HOME": DS_CODEX_HOME}, dev_role=dev_role, timeout=timeout,
        session_id=session_id),
    "agy": _call_agy,
    "claude": _call_claude,
}


def _worker(name, phase=None, batch_start=None, batch_target=None):
    opt = _option(name)
    try:
        instr = _batch_instruction(name, batch_start, batch_target) if batch_target else _instruction(name, phase)
        text = ADAPTERS[name](name, instr, opt)
        append_message(name, text, sub=opt["label"])
        if batch_target:
            with _lock:
                _batch_watermark[name] = max(_batch_watermark.get(name, 0), batch_target)
    except CallCancelled:
        append_message("system", f"? {PARTICIPANTS[name]['display']}?{opt['label']}????????????")
    except Exception as e:  # noqa: BLE001 - ????????????
        append_message("system", f"? {PARTICIPANTS[name]['display']}?{opt['label']}??????{e}")
    finally:
        with _lock:
            _busy.pop(name, None)
        if batch_target:
            _maybe_start_auto_batch()


def ask(names, phase=None):
    started = []
    for name in names:
        if name not in ADAPTERS:
            continue
        with _lock:
            if name in _busy:
                continue
            _busy[name] = time.time()
        threading.Thread(target=_worker, args=(name, phase), daemon=True).start()
        started.append(name)
    return started


def _default_enabled_seats():
    return [name for name in PARTICIPANTS if name in ADAPTERS]


def _normalize_seats(names):
    # Keep canonical PARTICIPANTS order, drop unknown/duplicate seats.
    wanted = {name for name in (names or []) if name in ADAPTERS}
    return [name for name in PARTICIPANTS if name in wanted]


def _set_enabled_seats(names):
    global _enabled_seats
    _enabled_seats = _normalize_seats(names)
    _save_settings()


def _valid_batch_names(names):
    seen = set()
    valid = []
    for name in names or []:
        if name in ADAPTERS and name not in seen:
            valid.append(name)
            seen.add(name)
    return valid


def _prepare_batch(names=None, *, auto=False, reset_auto=False):
    global _batch_auto_rounds, _batch_blocked
    notice = None
    with _lock:
        if _discussion["active"] or _busy:
            return [], None
        # names is None -> keep the persisted enabled seats (guest/auto path);
        # names given (HOST) -> that selection becomes the new persisted set.
        if names is not None:
            _set_enabled_seats(names)
        if reset_auto:
            _batch_auto_rounds = 0
            _batch_blocked = False
        if not _enabled_seats or (_batch_blocked and not reset_auto):
            return [], None
        target = _latest_human_no()
        if target <= 0:
            return [], None
        pending = []
        for name in _enabled_seats:
            start_no = _batch_watermark.get(name, 0) + 1
            if target >= start_no:
                pending.append((name, start_no, target))
        if not pending:
            return [], None
        if auto:
            if _batch_auto_rounds >= MAX_AUTO_ROUNDS:
                _batch_blocked = True
                notice = (
                    f"Pending human messages up to [{target}] were not processed because "
                    f"the auto-batch limit ({MAX_AUTO_ROUNDS}) was reached. HOST must send a message or ask AI again to continue."
                )
                return [], notice
            _batch_auto_rounds += 1
        for name, _, _ in pending:
            _busy[name] = time.time()
        return pending, None


def _launch_batch(pending):
    started = []
    for name, start_no, target in pending:
        threading.Thread(
            target=_worker,
            args=(name, None, start_no, target),
            daemon=True,
        ).start()
        started.append(name)
    return started


def start_batch(names=None, *, reset_auto=False):
    pending, notice = _prepare_batch(names, auto=False, reset_auto=reset_auto)
    if notice:
        append_message("system", notice)
    return _launch_batch(pending)


def _maybe_start_auto_batch():
    pending, notice = _prepare_batch(auto=True, reset_auto=False)
    if notice:
        append_message("system", notice)
        return []
    return _launch_batch(pending)


def cancel_call(name):
    if name not in ADAPTERS:
        return False
    with _lock:
        proc = _processes.get(name)
        if name not in _busy or proc is None:
            return False
        _cancel_requested.add(name)
    _terminate_process(proc)
    return True


# ---- 共識討論模式 ----------------------------------------------------------
# 首輪平行收集獨立初始意見，第 2 輪起 round-robin 序列辯論；全票「結論傾向：共識」
# 才早停（首輪不判），到 max_rounds 仍未收斂就停下交主持人裁決。
DISCUSSION_MIN_ROUNDS = 2  # 首輪只是獨立開場白、非交鋒，故實質下限為 2
DISCUSSION_MAX_ROUNDS = 8
_INTERRUPT_MSG = "⏸ 偵測到主持人插話，已中止自動討論，主導權交還主持人。"


def _clamp_rounds(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DISCUSSION_MIN_ROUNDS
    return max(DISCUSSION_MIN_ROUNDS, min(DISCUSSION_MAX_ROUNDS, n))


def _stance(text):
    """讀取發言中最後一個「結論傾向」標記：consensus / reserved / None。"""
    for line in reversed((text or "").splitlines()):
        if "結論傾向" in line:
            if "共識" in line:
                return "consensus"
            if "保留" in line:
                return "reserved"
            return None
    return None


def _wait_busy_clear(names):
    """等到這批席位都離開 _busy（成功、失敗、取消都會清），或超時後放行。"""
    deadline = time.time() + CALL_TIMEOUT + 30
    while time.time() < deadline:
        with _lock:
            if not any(n in _busy for n in names):
                return
        time.sleep(0.3)


def _host_interrupted(baseline):
    with _lock:
        return any(_is_host_message(m) for m in _messages[baseline:])


def _run_seat_sync(name, phase):
    """序列輪：同步跑完一席（寫入逐字稿後才返回），下一席才讀得到它的發言。"""
    with _lock:
        if name in _busy:
            return
        _busy[name] = time.time()
    _worker(name, phase)  # _worker 的 finally 會清 _busy


def _round_stances(round_start, order):
    """該輪成功發言者（speaker 屬於 order）的結論標記；失敗席會落在 system、不列入。"""
    with _lock:
        msgs = list(_messages[round_start:])
    return {m["speaker"]: _stance(m.get("text")) for m in msgs if m.get("speaker") in order}


def _final_stance_lines(order):
    """每席最近一則發言裡的結論標記整行，供結束總結顯示。"""
    with _lock:
        msgs = list(_messages)
    lines = {}
    for m in reversed(msgs):
        sp = m.get("speaker")
        if sp in order and sp not in lines:
            lines[sp] = next((ln.strip() for ln in reversed((m.get("text") or "").splitlines())
                              if "結論傾向" in ln), None)
        if len(lines) == len(order):
            break
    return lines


def _append_discussion_summary(reason, rounds_done, max_rounds, order):
    if reason == "consensus":
        body = [f"✅ 共識討論在第 {rounds_done} 輪達成全票共識。"]
    else:
        body = [f"⏹ 共識討論已達上限 {max_rounds} 輪、仍未全票共識，交主持人裁決。"]
    body.append("各席最終結論標記：")
    lines = _final_stance_lines(order)
    for name in order:
        disp = PARTICIPANTS[name]["display"]
        body.append(f"· {disp}：{lines.get(name) or '（未表態或呼叫失敗）'}")
    append_message("system", "\n".join(body))


def start_discussion(names, max_rounds):
    with _lock:
        if _discussion["active"]:
            return False
        _discussion.update({"active": True, "round": 1, "max_rounds": _clamp_rounds(max_rounds)})
    threading.Thread(target=run_discussion, args=(names, max_rounds), daemon=True).start()
    return True


def run_discussion(names, max_rounds):
    try:
        order = [n for n in names if n in ADAPTERS]
        if not order:
            return
        max_rounds = _clamp_rounds(max_rounds)
        with _lock:
            baseline = len(_messages)  # 本場討論起點；之後出現的「你」發言＝插話
            _discussion.update({"round": 1, "max_rounds": max_rounds})

        # 第 1 輪：平行呼叫，收集各席獨立初始意見；barrier 等全部跑完，此輪不判早停。
        _wait_busy_clear(ask(order, phase="first"))
        if _host_interrupted(baseline):
            append_message("system", _INTERRUPT_MSG)
            return

        # 第 2 輪起：round-robin 序列，每席發言前都讀得到前一席剛寫進逐字稿的內容。
        for r in range(2, max_rounds + 1):
            with _lock:
                _discussion["round"] = r
                round_start = len(_messages)
            for name in order:
                if _host_interrupted(baseline):
                    append_message("system", _INTERRUPT_MSG)
                    return
                _run_seat_sync(name, phase="debate")
            stances = _round_stances(round_start, order)
            if stances and all(v == "consensus" for v in stances.values()):
                _append_discussion_summary("consensus", r, max_rounds, order)
                return
        _append_discussion_summary("max", max_rounds, max_rounds, order)
    finally:
        with _lock:
            _discussion["active"] = False


# ---- 開發模式管線（Phase D2） ------------------------------------------------
# 單一背景 thread 跑整條固定管線（拆任務→逐任務 實作/驗證/仲裁→收尾），tasks.json 是
# 斷點續作的唯一事實來源；每一棒（席位 subprocess 呼叫）前後都做安全網檢查（竄改/煞車/
# 插話/限流），細節見開發模式規格書 §5～§7 與開發設計方針.md。

_JSON_FENCE_RE = re.compile(re.escape(JSON_FENCE_OPEN) + r"\s*(.*?)" + re.escape(JSON_FENCE_CLOSE), re.DOTALL)
_devlog_seq = 0


def _empty_usage_state():
    return {"last": {}, "by_provider": {}, "known_total_tokens": 0, "incomplete": False}


def _migrate_tasks(data):
    if not isinstance(data, dict):
        return data, False
    changed = data.get("version") != 2
    data = json.loads(json.dumps(data, ensure_ascii=False))
    data["version"] = 2
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
        changed = True
    if not isinstance(data.get("usage"), dict):
        data["usage"] = _empty_usage_state()
        changed = True
    else:
        defaults = _empty_usage_state()
        for key, value in defaults.items():
            if key not in data["usage"]:
                data["usage"][key] = value
                changed = True
    for task in data.get("tasks", []):
        defaults = {
            "base_commit": "",
            "last_failure_fingerprint": "",
            "consecutive_same_failures": 0,
        }
        for key, value in defaults.items():
            if key not in task:
                task[key] = value
                changed = True
    if "integration_verified" not in data:
        data["integration_verified"] = False
        changed = True
    if "main_commit" not in data:
        data["main_commit"] = ""
        data["git_baselines_pending"] = True
        changed = True
    return data, changed


def _load_tasks():
    data = _read_json(TASKS_FILE, None)
    if data is None:
        return None
    migrated, changed = _migrate_tasks(data)
    if changed:
        _save_tasks(migrated)
    return migrated


def _save_tasks(data):
    global _trusted_tasks
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    _write_atomic(TASKS_FILE, payload)
    _trusted_tasks = json.loads(payload)


def _remember_loaded_tasks(data):
    # 僅供伺服器啟動時建立初始信任基準；執行期間的一般讀取不可刷新此快照。
    global _trusted_tasks
    _trusted_tasks = json.loads(json.dumps(data, ensure_ascii=False))
    _record_hash(TASKS_FILE)


def _trusted_tasks_copy():
    if _trusted_tasks is None:
        return None
    return json.loads(json.dumps(_trusted_tasks, ensure_ascii=False))


def _load_meeting_summary():
    return _read_json(MEETING_SUMMARY_FILE, None)


def _save_meeting_summary(summary):
    global _trusted_meeting_summary
    payload = json.dumps(summary, ensure_ascii=False, indent=1)
    _write_atomic(MEETING_SUMMARY_FILE, payload)
    _trusted_meeting_summary = json.loads(payload)


def _remember_loaded_meeting_summary(summary):
    global _trusted_meeting_summary
    _trusted_meeting_summary = json.loads(json.dumps(summary, ensure_ascii=False)) if summary else None
    _record_hash(MEETING_SUMMARY_FILE)


def _trusted_summary_copy():
    if _trusted_meeting_summary is None:
        return None
    return json.loads(json.dumps(_trusted_meeting_summary, ensure_ascii=False))


def _pause_tampered_data():
    append_message("system", "⚠ 偵測到 data/ 目錄下的檔案被非伺服器途徑竄改，管線已暫停。")
    data = _trusted_tasks_copy()
    if data is not None:
        _dev_pause_state(data, "tamper")
    else:
        with _lock:
            _dev["paused"] = True
            _dev["pause_reason"] = "tamper"



def _git(*args, timeout=60):
    return subprocess.run(
        ["git", *args], cwd=_project_dir, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _git_repo_check():
    try:
        r = _git("rev-parse", "--is-inside-work-tree")
    except Exception as e:  # noqa: BLE001 - 目錄不存在／git 未安裝等都回可讀錯誤
        return False, f"git 執行失敗：{e}"
    if r.returncode != 0 or (r.stdout or "").strip() != "true":
        return False, "專案目錄不是 git repo"
    return True, ""


def _git_precheck():
    # 新啟管線的前置檢查：repo + 乾淨 working tree。續作路徑的髒 tree 有另外的
    # 處置（見 _dev_start：管線分支上先 commit 殘留變更），不走本函式的乾淨要求。
    ok, reason = _git_repo_check()
    if not ok:
        return ok, reason
    r = _git("status", "--porcelain")
    if r.returncode != 0:
        return False, "git status 執行失敗"
    if (r.stdout or "").strip():
        return False, "working tree 有未提交的變更，請先手動處理後再啟動管線"
    return True, ""


def _git_commit_leftover(data):
    # 實作棒中斷（限流/錯誤/crash/逾時）可能留下未 commit 的變更；續作時在管線分支上
    # 由伺服器先收進一個標記 commit（規格 §5），讓驗證席仍有確定性的 diff 可查。
    added = _git("add", "-A")
    if added.returncode != 0:
        raise RuntimeError(f"git add 失敗：{(added.stderr or '').strip()}")
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        raise RuntimeError(f"git status 失敗：{(status.stderr or '').strip()}")
    if not (status.stdout or "").strip():
        return None
    task = next((t for t in data.get("tasks", []) if t.get("status") == "in_progress"), None)
    tid = task["id"] if task else 0
    commit = _git("commit", "-m", f"[roundtable] 任務{tid} 中斷殘留變更")
    if commit.returncode != 0:
        raise RuntimeError(f"git commit 失敗：{(commit.stderr or '').strip()}")
    rev_result = _git("rev-parse", "--short", "HEAD")
    if rev_result.returncode != 0:
        raise RuntimeError(f"讀取 commit hash 失敗：{(rev_result.stderr or '').strip()}")
    rev = (rev_result.stdout or "").strip()
    if task is not None:
        task["commits"].append(rev)  # 殘留變更屬於中斷的那個任務，驗證棒要看得到這個 diff
    return rev


def _git_new_branch():
    branch = f"{DEV_BRANCH_PREFIX}{_stamp()}"
    r = _git("checkout", "-b", branch)
    if r.returncode != 0:
        raise RuntimeError(f"建立分支失敗：{(r.stderr or '').strip()}")
    return branch


def _git_switch_branch(branch):
    if not branch:
        raise RuntimeError("tasks.json 未記錄分支名稱")
    r = _git("checkout", branch)
    if r.returncode != 0:
        raise RuntimeError(f"切換分支失敗：{(r.stderr or '').strip()}")


def _git_commit_task(task, attempt_no, seat_display):
    # 無變更視為該次嘗試失敗（回傳 None），由呼叫端記 feedback，不進驗證棒。
    added = _git("add", "-A")
    if added.returncode != 0:
        raise RuntimeError(f"git add 失敗：{(added.stderr or '').strip()}")
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        raise RuntimeError(f"git status 失敗：{(status.stderr or '').strip()}")
    if not (status.stdout or "").strip():
        return None
    msg = f"[roundtable] 任務{task['id']} {seat_display} 第{attempt_no}次: {task['title']}"
    commit = _git("commit", "-m", msg)
    if commit.returncode != 0:
        raise RuntimeError(f"git commit 失敗：{(commit.stderr or '').strip()}")
    rev = _git("rev-parse", "--short", "HEAD")
    if rev.returncode != 0:
        raise RuntimeError(f"讀取 commit hash 失敗：{(rev.stderr or '').strip()}")
    return (rev.stdout or "").strip()


def _git_show_stat(sha):
    r = _git("show", "--stat", sha)
    if r.returncode != 0:
        raise RuntimeError(f"git show 失敗：{(r.stderr or '').strip()}")
    return (r.stdout or "").strip()


def _git_head():
    r = _git("rev-parse", "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"讀取 HEAD 失敗：{(r.stderr or '').strip()}")
    return (r.stdout or "").strip()


def _git_diff_summary(base_commit):
    diff_range = f"{base_commit}...HEAD"
    name_status = _git("diff", "--name-status", diff_range)
    stat = _git("diff", "--stat", diff_range)
    if name_status.returncode != 0 or stat.returncode != 0:
        err = (name_status.stderr or stat.stderr or "").strip()
        raise RuntimeError(f"讀取最終 net diff 失敗：{err}")
    block = "=== name-status ===\n" + (name_status.stdout or "（無變更）").strip()
    block += "\n=== stat ===\n" + (stat.stdout or "（無變更）").strip()
    return diff_range, block


def _backfill_git_baselines(data):
    """舊 v1 管線續作時，從實際 commit graph 回填 D4 baseline；不可在純 JSON migration 猜測。"""
    if not data.get("git_baselines_pending") and data.get("main_commit"):
        return False
    merge_base = _git("merge-base", "HEAD", "main")
    if merge_base.returncode != 0 or not (merge_base.stdout or "").strip():
        raise RuntimeError(f"無法回填管線 main baseline：{(merge_base.stderr or '').strip()}")
    data["main_commit"] = (merge_base.stdout or "").strip()
    for task in data.get("tasks", []):
        if task.get("base_commit"):
            continue
        commits = task.get("commits") or []
        if commits:
            parent = _git("rev-parse", f"{commits[0]}^")
            if parent.returncode != 0 or not (parent.stdout or "").strip():
                raise RuntimeError(
                    f"無法由任務 {task.get('id')} 第一個 commit 回填 baseline："
                    f"{(parent.stderr or '').strip()}"
                )
            task["base_commit"] = (parent.stdout or "").strip()
        elif task.get("status") == "in_progress":
            # 沒有 commit 就沒有可驗證的實作；回到 pending，下一次 implement 會以當時 HEAD 建 baseline。
            task["status"] = "pending"
            task["last_verdict"] = "舊版中斷任務沒有可驗證 commit，已安全回到 pending"
    data.pop("git_baselines_pending", None)
    data["updated_at"] = _now()
    return True


def _failure_fingerprint(source, reason):
    normalized = unicodedata.normalize("NFKC", reason or "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return f"{source}:{normalized}"


def _record_task_failure(task, source, reason):
    fingerprint = _failure_fingerprint(source, reason)
    if fingerprint == task.get("last_failure_fingerprint"):
        task["consecutive_same_failures"] = int(task.get("consecutive_same_failures") or 0) + 1
    else:
        task["last_failure_fingerprint"] = fingerprint
        task["consecutive_same_failures"] = 1
    return task["consecutive_same_failures"] >= 2


class _ParsedTasks(list):
    def __init__(self, tasks, summary):
        super().__init__(tasks)
        self.summary = summary


def _validate_meeting_summary(raw, previous_summary=None):
    if not isinstance(raw, dict):
        return None
    required = (
        "source_message_watermark", "goal", "decisions", "non_goals",
        "global_constraints", "acceptance_criteria", "open_questions",
    )
    if not all(key in raw for key in required):
        return None
    watermark = raw["source_message_watermark"]
    if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
        return None
    if watermark > len(_messages):
        return None
    if previous_summary and watermark < previous_summary.get("source_message_watermark", 0):
        return None
    if not isinstance(raw["goal"], str) or not raw["goal"].strip():
        return None
    arrays = required[2:]
    if any(not isinstance(raw[key], list) or not all(isinstance(v, str) for v in raw[key]) for key in arrays):
        return None
    return {key: raw[key] for key in required}


def _parse_tasks(text, *, allow_empty=False, previous_summary=None):
    # 取最後一個 json 圍欄並嚴格驗證規格 §6.1；不做部分接受或自動修補。
    # done 任務的保留規則由消化棒呼叫端的 _merge_tasks 處理。
    matches = _JSON_FENCE_RE.findall(text or "")
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    summary = _validate_meeting_summary(data.get("meeting_summary"), previous_summary)
    if summary is None:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or (not tasks and not allow_empty):
        return None
    cleaned = []
    seen_ids = set()
    for t in tasks:
        if not isinstance(t, dict) or not all(k in t for k in ("id", "title", "files", "acceptance")):
            return None
        task_id = t["id"]
        title = t["title"]
        files = t["files"]
        acceptance = t["acceptance"]
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0 or task_id in seen_ids:
            return None
        if not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            return None
        if (not isinstance(acceptance, list) or not all(isinstance(item, str) for item in acceptance)
                or not any(item.strip() for item in acceptance)):
            return None
        seen_ids.add(task_id)
        cleaned.append(t)
    return _ParsedTasks(cleaned, summary)


def _parse_verdict(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        if line == VERDICT_PASS:
            return {"passed": True, "reason": ""}
        if line.startswith(VERDICT_FAIL_PREFIX):
            reason = line[len(VERDICT_FAIL_PREFIX):].strip()
            return {"passed": False, "reason": reason} if reason else None
        return None
    return None


def _parse_arbitration(text):
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith(ARBITRATION_REASSIGN_PREFIX):
            detail = line[len(ARBITRATION_REASSIGN_PREFIX):].strip()
            return {"action": "reassign", "detail": detail} if detail else None
        if line.startswith(ARBITRATION_SKIP_PREFIX):
            detail = line[len(ARBITRATION_SKIP_PREFIX):].strip()
            return {"action": "skip", "detail": detail} if detail else None
        if line.startswith(ARBITRATION_ASK_PREFIX):
            detail = line[len(ARBITRATION_ASK_PREFIX):].strip()
            return {"action": "ask", "detail": detail} if detail else None
        return None
    return None


def _tasks_from_parsed(parsed):
    return [{
        "id": t["id"], "title": t["title"],
        "files": t.get("files") or [], "acceptance": t.get("acceptance") or [],
        "status": "pending", "attempts": 0, "arbitrated": False,
        "base_commit": "", "commits": [], "last_verdict": "",
        "last_failure_fingerprint": "", "consecutive_same_failures": 0,
    } for t in parsed]


def _merge_tasks(existing, parsed):
    # 消化棒合併規則：以 id 對齊；done 任務保留原紀錄不可變更；新清單缺少的未完成任務視為刪除。
    existing_by_id = {t["id"]: t for t in existing}
    parsed_ids = {t["id"] for t in parsed}
    merged = [t for t in existing if t.get("status") == "done" and t["id"] not in parsed_ids]
    for t in parsed:
        old = existing_by_id.get(t.get("id"))
        if old and old.get("status") == "done":
            merged.append(old)
            continue
        merged.append({
            "id": t["id"], "title": t["title"],
            "files": t.get("files") or [], "acceptance": t.get("acceptance") or [],
            "status": (old or {}).get("status", "pending"),
            "attempts": (old or {}).get("attempts", 0),
            "arbitrated": (old or {}).get("arbitrated", False),
            "commits": list((old or {}).get("commits", [])),
            "base_commit": (old or {}).get("base_commit", ""),
            "last_verdict": (old or {}).get("last_verdict", ""),
            "last_failure_fingerprint": (old or {}).get("last_failure_fingerprint", ""),
            "consecutive_same_failures": (old or {}).get("consecutive_same_failures", 0),
        })
        if old and any(old.get(k) != t.get(k) for k in ("title", "files", "acceptance")):
            merged[-1]["last_failure_fingerprint"] = ""
            merged[-1]["consecutive_same_failures"] = 0
    return merged


def _session_id_for_call(data, role, seat, task_id):
    if role == "verifier" or not isinstance(data, dict):
        return None
    saved = (data.get("sessions") or {}).get(role)
    if not isinstance(saved, dict):
        return None
    if saved.get("provider") != seat or saved.get("project_dir") != _project_dir:
        return None
    if saved.get("branch") != data.get("branch"):
        return None
    if role == "implementer" and saved.get("task_id") != task_id:
        return None
    value = saved.get("session_id")
    return value if isinstance(value, str) and value.strip() else None


def _normalize_adapter_result(value):
    if isinstance(value, str):
        return _adapter_result(value)
    if not isinstance(value, dict) or not isinstance(value.get("text"), str) or not value["text"].strip():
        raise RuntimeError("adapter 未回傳合法的結構化結果")
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else _unavailable_usage()
    usage = {**_unavailable_usage(), **usage}
    session = value.get("session") if isinstance(value.get("session"), dict) else {}
    return {
        "text": value["text"].strip(), "usage": usage,
        "session": {
            "id": session.get("id"), "resumed": bool(session.get("resumed")),
            "resume_failed": bool(session.get("resume_failed")),
            "persistence_fallback": bool(session.get("persistence_fallback")),
        },
    }


def _record_usage_and_session(data, seat, role, task_id, result):
    if not isinstance(data, dict):
        return
    data.setdefault("sessions", {})
    session = result["session"]
    session_id = session.get("id")
    if role != "verifier" and isinstance(session_id, str) and session_id.strip():
        data["sessions"][role] = {
            "provider": seat, "session_id": session_id, "task_id": task_id if role == "implementer" else None,
            "branch": data.get("branch"), "project_dir": _project_dir,
        }
    elif role == "implementer" and role in data["sessions"]:
        saved = data["sessions"][role]
        if saved.get("task_id") != task_id or saved.get("provider") != seat:
            data["sessions"].pop(role, None)

    state = data.setdefault("usage", _empty_usage_state())
    usage = dict(result["usage"])
    last = {"provider": seat, "role": role, **usage}
    state["last"] = last
    total = usage.get("total_tokens")
    if isinstance(total, int):
        state["known_total_tokens"] = int(state.get("known_total_tokens") or 0) + total
        provider = state.setdefault("by_provider", {}).setdefault(seat, {
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0,
        })
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            if isinstance(usage.get(key), int):
                provider[key] += usage[key]
        if isinstance(usage.get("cost_usd"), (int, float)):
            provider["cost_usd"] += usage["cost_usd"]
    else:
        state["incomplete"] = True


def _write_devlog(task_id, seat, stage, instruction, error=None, metadata=None):
    global _devlog_seq
    _devlog_seq += 1
    info = _last_run.get(seat) or {}
    DEVLOG_DIR.mkdir(exist_ok=True)
    lines = [
        f"args: {info.get('args')}",
        f"returncode: {info.get('returncode')}",
        f"elapsed: {info.get('elapsed', 0):.2f}s",
        "",
        "=== instruction ===",
        instruction,
        "",
        "=== stdout ===",
        info.get("stdout") or "",
        "",
        "=== stderr ===",
        info.get("stderr") or "",
    ]
    if error:
        lines += ["", "=== error ===", error]
    lines += ["", "metadata_json=" + json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))]
    tid = task_id if task_id is not None else 0
    # stamp 只到秒，管線快跑（尤其測試）可能同秒多棒，故加遞增序號避免檔名撞掉。
    path = DEVLOG_DIR / f"{_stamp()}-{_devlog_seq:04d}-task{tid}-{seat}-{stage}.log"
    path.write_text("\n".join(str(x) for x in lines), encoding="utf-8")


def _call_seat_checked(seat, instr, stage, task_id=None, dev_role=None, data=None):
    # 管線唯一的席位呼叫入口：統一寫稽核日誌、統一偵測限流特徵。
    if data is not None and _dev_gate_or_pause():
        raise _PipelinePaused(_dev.get("pause_reason") or "gate")
    _last_run.pop(seat, None)
    resume_id = _session_id_for_call(data, dev_role, seat, task_id)
    result = None
    try:
        try:
            value = ADAPTERS[seat](seat, instr, _option(seat), dev_role=dev_role,
                                   timeout=DEV_CALL_TIMEOUT, session_id=resume_id)
        except Exception as first_error:
            info = _last_run.get(seat) or {}
            blob = f"{info.get('stdout', '')}\n{info.get('stderr', '')}\n{first_error}".lower()
            invalid_session = resume_id and any(
                noun in blob for noun in ("session", "thread", "conversation")) and any(
                marker in blob for marker in ("not found", "expired", "invalid", "unknown", "不存在", "過期"))
            if not invalid_session:
                raise
            _last_run.pop(seat, None)
            value = ADAPTERS[seat](seat, instr, _option(seat), dev_role=dev_role,
                                   timeout=DEV_CALL_TIMEOUT, session_id=None)
            result = _normalize_adapter_result(value)
            result["session"]["resume_failed"] = True
        if result is None:
            result = _normalize_adapter_result(value)
        info = _last_run.get(seat) or {}
        if info.get("returncode") not in (None, 0):
            raise RuntimeError(f"{seat} exit={info['returncode']}")
        _record_usage_and_session(data, seat, dev_role, task_id, result)
        _write_devlog(task_id, seat, stage, instr, metadata={
            "usage": result["usage"], "session": result["session"], "role": dev_role,
        })
        return result["text"]
    except Exception as e:  # noqa: BLE001 - 先判斷是否為限流特徵，再決定要不要往外拋
        _write_devlog(task_id, seat, stage, instr, error=str(e), metadata={
            "usage": _unavailable_usage(),
            "session": {
                "id": resume_id, "resumed": bool(resume_id), "resume_failed": False,
                "persistence_fallback": False,
            },
            "role": dev_role,
        })
        info = _last_run.get(seat) or {}
        blob = f"{info.get('stdout', '')}\n{info.get('stderr', '')}\n{e}".lower()
        if any(marker in blob for marker in RATE_LIMIT_MARKERS):
            raise _RateLimited(str(e)) from e
        raise
    finally:
        if data is not None:
            _dev_record_turn(data)


def _protocol_call(seat, instr, parse_fn, stage, task_id=None, dev_role=None, data=None):
    # 解析失敗處理（規格 §6.4）：附「上次輸出不符格式」重問一次；再失敗由呼叫端轉入暫停。
    text = _call_seat_checked(seat, instr, stage, task_id=task_id, dev_role=dev_role, data=data)
    parsed = parse_fn(text)
    if parsed is not None:
        return text, parsed
    retry_instr = instr + "\n\n" + PROTOCOL_RETRY_NOTE
    text2 = _call_seat_checked(
        seat, retry_instr, f"{stage}-retry", task_id=task_id, dev_role=dev_role, data=data)
    return text2, parse_fn(text2)


def _valid_saved_summary():
    raw = _load_meeting_summary()
    return _validate_meeting_summary(raw) if raw else None


def _controller_context():
    summary = _valid_saved_summary()
    if summary is None:
        return f"摘要不可用；請讀取完整 UTF-8 逐字稿重建：{MD_MIRROR}", None
    watermark = summary["source_message_watermark"]
    recent = _messages[watermark:]
    return (
        "現行會議摘要：\n" + json.dumps(summary, ensure_ascii=False, indent=1)
        + "\n摘要水位後的新訊息：\n" + json.dumps(recent, ensure_ascii=False, indent=1),
        summary,
    )


def _summary_projection():
    summary = _valid_saved_summary()
    if summary is None:
        return "（會議摘要不可用；依任務簡報與專案規格文件判斷）"
    return (
        "相關決議：" + "; ".join(summary.get("decisions") or ["（無）"])
        + "\n全域限制：" + "; ".join(summary.get("global_constraints") or ["（無）"])
        + "\n全域驗收：" + "; ".join(summary.get("acceptance_criteria") or ["（無）"])
    )


def _dev_instruction(stage, **kw):
    # 上下文裁剪（規格 §5）：拆任務/仲裁/消化/收尾讀完整逐字稿；實作/驗證只讀固定小包裹。
    controller_disp = PARTICIPANTS[_dev_roles["controller"]]["display"]
    if stage == "dispatch":
        return (
            f"你是開發模式管線的主控席「{controller_disp}」，負責拆解任務。\n"
            f"1. 先讀取完整逐字稿（UTF-8）：{MD_MIRROR}——內含主持人交代的開發目標與先前討論。\n"
            f"2. 專案目錄：{_project_dir}，可唯讀查閱檔案佐證判斷。\n"
            f"3. 現行 tasks.json 內容如下（tasks 為空代表尚未拆過任務）：\n{kw['tasks_json']}\n"
            f"4. 請根據逐字稿中主持人交代的目標，拆出有序、可獨立驗收的任務清單，"
            f"每項包含 id（從 1 遞增整數）、title、files（涉及檔案路徑陣列）、acceptance（驗收條件陣列）。\n"
            f"5. 同一份輸出也要建立會議摘要；source_message_watermark 應設為目前訊息數 {len(_messages)}。\n"
            f"6. 輸出末尾必須包含且只包含一個下列格式的 json 圍欄：\n"
            f"{JSON_FENCE_OPEN}\n"
            f'{{"meeting_summary": {{"source_message_watermark": {len(_messages)}, "goal": "...", '
            f'"decisions": [], "non_goals": [], "global_constraints": [], "acceptance_criteria": [], '
            f'"open_questions": []}}, "tasks": [{{"id": 1, "title": "...", "files": ["..."], "acceptance": ["..."]}}]}}\n'
            f"{JSON_FENCE_CLOSE}\n"
            f"用繁體中文書寫圍欄前的說明文字。絕對不要建立、修改或刪除任何檔案。"
        )
    if stage == "digest":
        context, _ = _controller_context()
        return (
            f"你是開發模式管線的主控席「{controller_disp}」，負責消化主持人插話並修訂任務清單。\n"
            f"1. 依下列裁剪後上下文處理；只有其中明示摘要不可用時才讀完整逐字稿：\n{context}\n"
            f"2. 專案目錄：{_project_dir}。\n"
            f"3. 現行 tasks.json 內容：\n{kw['tasks_json']}\n"
            f"4. 請依主持人最新發言修訂任務清單：已標記 status=done 的任務內容與狀態不可更動；"
            f"其餘可依插話內容新增、修改；輸出清單中缺少的未完成任務將被視為刪除。\n"
            f"5. 同棒更新完整 meeting_summary，source_message_watermark 應設為目前訊息數 {len(_messages)}。\n"
            f"6. 輸出末尾必須包含且只包含一個下列格式的**完整**（非增量）json 圍欄：\n"
            f"{JSON_FENCE_OPEN}\n"
            f'{{"meeting_summary": {{"source_message_watermark": {len(_messages)}, "goal": "...", '
            f'"decisions": [], "non_goals": [], "global_constraints": [], "acceptance_criteria": [], '
            f'"open_questions": []}}, "tasks": [{{"id": 1, "title": "...", "files": ["..."], "acceptance": ["..."]}}]}}\n'
            f"{JSON_FENCE_CLOSE}\n"
            f"用繁體中文書寫圍欄前的說明文字。絕對不要建立、修改或刪除任何檔案。"
        )
    if stage == "arbitrate":
        task = kw["task"]
        context, _ = _controller_context()
        return (
            f"你是開發模式管線的主控席「{controller_disp}」，負責仲裁一個連續失敗的任務。\n"
            f"1. 依下列裁剪後上下文處理；只有其中明示摘要不可用時才讀完整逐字稿：\n{context}\n"
            f"2. 專案目錄：{_project_dir}。\n"
            f"3. 現行 tasks.json 內容：\n{kw['tasks_json']}\n"
            f"4. 任務「{task['title']}」（id={task['id']}）已連續失敗 {task['attempts']} 次，"
            f"最近一次驗證意見：{task.get('last_verdict') or '（無）'}。\n"
            f"5. 請三選一裁決：重派（給實作席新指示、重試計數歸零重來）／跳過（放棄此任務標記 blocked）／"
            f"詢問（暫停管線、交主持人拍板）。\n"
            f"6. 輸出最後一行必須是（照抄格式，<>內填你的內容，不要多加標點）：\n"
            f"{ARBITRATION_REASSIGN_PREFIX}<給實作席的新指示>\n"
            f"{ARBITRATION_SKIP_PREFIX}<原因>\n"
            f"{ARBITRATION_ASK_PREFIX}<要主持人拍板的問題>\n"
            f"用繁體中文。絕對不要建立、修改或刪除任何檔案。"
        )
    if stage == "handoff":
        context, _ = _controller_context()
        return (
            f"你是開發模式管線的主控席「{controller_disp}」，管線即將結束，請寫交接摘要。\n"
            f"1. 依下列裁剪後上下文處理；只有其中明示摘要不可用時才讀完整逐字稿：\n{context}\n"
            f"2. 專案目錄：{_project_dir}。\n"
            f"3. 現行 tasks.json 內容：\n{kw['tasks_json']}\n"
            f"4. 請用繁體中文寫一段交接摘要：完成了什麼、哪些任務被跳過及原因、下次接手建議從哪裡開始。"
            f"自由文字即可，不需要特殊格式。絕對不要建立、修改或刪除任何檔案。"
        )
    if stage == "implement":
        task = kw["task"]
        feedback = kw.get("last_verdict") or ""
        feedback_block = f"\n5. 上次驗證意見（請針對此修正）：{feedback}\n" if feedback else "\n"
        return (
            f"你是開發模式管線的實作席，本棒任務是動手修改程式碼完成以下任務。\n"
            f"1. 專案目錄（工作範圍僅限於此，不得寫入本目錄以外的路徑，"
            f"尤其不得建立/修改/刪除 .git/ 或本工具自身的 data/ 目錄）：{_project_dir}\n"
            f"2. 若專案目錄內有規格文件，請自行尋找並閱讀以理解需求脈絡。\n"
            f"3. 本次任務簡報：\n"
            f"   標題：{task['title']}\n"
            f"   涉及檔案：{', '.join(task.get('files') or []) or '（未指定，依任務判斷）'}\n"
            f"   驗收條件：{'; '.join(task.get('acceptance') or [])}\n"
            f"   適用的會議限制：\n{_summary_projection()}\n"
            f"4. 請直接動手建立/修改/刪除必要的檔案完成任務；完成後簡短說明你做了什麼變更即可，"
            f"不需要自己執行 git commit（伺服器會處理）。"
            f"{feedback_block}"
            f"用繁體中文回覆。"
        )
    if stage == "verify":
        task = kw["task"]
        return (
            f"你是開發模式管線的驗證席，只能唯讀查閱，任務是驗收下列任務的實作是否通過。\n"
            f"1. 專案目錄：{_project_dir}，只以指定 range 的最終 net diff 驗證；不得逐一重讀歷史 commits。\n"
            f"2. 本次任務簡報：\n"
            f"   標題：{task['title']}\n"
            f"   涉及檔案：{', '.join(task.get('files') or []) or '（未指定）'}\n"
            f"   驗收條件：{'; '.join(task.get('acceptance') or [])}\n"
            f"   適用的會議限制：\n{_summary_projection()}\n"
            f"3. 驗證 range：{kw['diff_range']}；最終 name-status／stat：\n{kw['diff_block']}\n"
            f"4. 請先比對最終 net diff 與任務簡報範圍，凡最終仍動到與任務無關的檔案，"
            f"一律判定不通過並在原因中列出越界檔案；再檢查是否滿足所有驗收條件。\n"
            f"5. 輸出最後一行必須是（照抄格式，不要多加標點）：\n"
            f"{VERDICT_PASS}\n{VERDICT_FAIL_PREFIX}<一句話原因>\n"
            f"用繁體中文書寫理由段落。絕對不要建立、修改或刪除任何檔案。"
        )
    if stage == "integration_verify":
        return (
            f"你是開發模式管線的驗證席，只能唯讀查閱。請對整條管線做收尾整合驗收。\n"
            f"1. 專案目錄：{_project_dir}。只看 {kw['diff_range']} 的最終 net diff，不重讀歷史 commits。\n"
            f"2. 全部任務與驗收：\n{kw['tasks_json']}\n"
            f"3. 最終 name-status／stat：\n{kw['diff_block']}\n"
            f"4. 適用的會議限制：\n{_summary_projection()}\n"
            f"5. 輸出最後一行必須是：\n{VERDICT_PASS}\n{VERDICT_FAIL_PREFIX}<一句話原因>\n"
            f"用繁體中文。絕對不要建立、修改或刪除任何檔案。"
        )
    raise ValueError(f"unknown dev stage: {stage}")


def _dev_touch(data):
    with _lock:
        _dev["turn_count"] += 1
        data["turn_count"] = _dev["turn_count"]

def _dev_record_turn(data):
    # 每次實際啟動 subprocess 都是一棒；protocol retry 也必須各自計數並在重問前受 cap 約束。
    _dev_touch(data)
    data["updated_at"] = _now()
    if not _check_tamper():
        append_message("system", "⚠ 偵測到 data/ 目錄下的檔案被非伺服器途徑竄改，管線已暫停。")
        _dev_pause_state(data, "tamper")
        raise _PipelinePaused("tamper")
    _save_tasks(data)



def _dev_pause_state(data, reason):
    with _lock:
        _dev["paused"] = True
        _dev["pause_reason"] = reason
        watermark = _latest_human_no()  # 訊息水位：續作時據此判斷暫停期間是否有新的人類發言（規格 §5）
    data["status"] = "paused"
    data["pause_reason"] = reason
    data["message_watermark"] = watermark
    data["updated_at"] = _now()
    _save_tasks(data)


def _dev_run_dispatch(data):
    with _lock:
        _dev["stage"] = "dispatch"
        _dev["current_task"] = None
    seat = _dev_roles["controller"]
    tasks_json = json.dumps(data, ensure_ascii=False, indent=1)
    instr = _dev_instruction("dispatch", tasks_json=tasks_json)
    text, parsed = _protocol_call(seat, instr, _parse_tasks, "dispatch", task_id=0, dev_role="controller", data=data)
    append_message(seat, text, sub="拆任務")
    if parsed is None:
        _dev_pause_state(data, "parse_fail")
        append_message("system", "⛔ 主控拆任務輸出連續兩次不符協議格式，管線暫停。")
        return False
    summary = {"version": 1, **parsed.summary, "updated_at": _now()}
    data["tasks"] = _tasks_from_parsed(parsed)
    data["dispatched"] = True
    data["status"] = "running"
    data["updated_at"] = _now()
    _save_meeting_summary(summary)
    _save_tasks(data)
    return True


def _dev_run_implement(data, task):
    with _lock:
        _dev["stage"] = "implement"
        _dev["current_task"] = task["id"]
    seat = _dev_roles["implementer"]
    if not task.get("base_commit"):
        task["base_commit"] = _git_head()
    task["attempts"] += 1
    task["status"] = "in_progress"
    data["updated_at"] = _now()
    _save_tasks(data)  # 呼叫前先落盤：crash 續作時至少知道本次嘗試已計入

    instr = _dev_instruction("implement", task=task, last_verdict=task.get("last_verdict"))
    try:
        text = _call_seat_checked(
            seat, instr, "implement", task_id=task["id"], dev_role="implementer", data=data)
    except Exception:
        # 限流或席位錯誤不算「任務失敗」：attempts 滾回。若席位中斷時留下變更，
        # 保留 in_progress 讓續作能把殘留 commit 掛回正確任務；乾淨則回 pending 重跑。
        task["attempts"] -= 1
        status = _git("status", "--porcelain")
        has_leftover = status.returncode == 0 and bool((status.stdout or "").strip())
        task["status"] = "in_progress" if has_leftover else "pending"
        data["updated_at"] = _now()
        _save_tasks(data)
        raise
    append_message(seat, text, sub=f"任務 {task['id']} · 實作")

    commit = _git_commit_task(task, task["attempts"], PARTICIPANTS[seat]["display"])
    if commit is None:
        task["status"] = "pending"
        task["last_verdict"] = "無任何檔案變更"
        append_message("system", f"任務 {task['id']} 實作棒未產生任何檔案變更，記為本次嘗試失敗。")
        repeated = _record_task_failure(task, "implement_no_change", task["last_verdict"])
        if repeated:
            _dev_pause_state(data, "repeated_failure")
            append_message("system", f"⏸ 任務 {task['id']} 相同失敗連續出現第 2 次，管線暫停。")
            return False
    else:
        task["commits"].append(commit)
    data["updated_at"] = _now()
    _save_tasks(data)
    return True


def _dev_run_verify(data, task):
    with _lock:
        _dev["stage"] = "verify"
        _dev["current_task"] = task["id"]
    seat = _dev_roles["verifier"]
    base_commit = task.get("base_commit")
    if not base_commit:
        raise RuntimeError(f"任務 {task['id']} 缺少 base_commit")
    diff_range, diff_block = _git_diff_summary(base_commit)
    instr = _dev_instruction("verify", task=task, diff_range=diff_range, diff_block=diff_block)
    text, verdict = _protocol_call(
        seat, instr, _parse_verdict, "verify", task_id=task["id"], dev_role="verifier", data=data)
    append_message(seat, text, sub=f"任務 {task['id']} · 驗證")
    if verdict is None:
        _dev_pause_state(data, "parse_fail")
        append_message("system", f"⛔ 任務 {task['id']} 驗證棒輸出連續兩次不符協議格式，管線暫停。")
        return False
    if verdict["passed"]:
        task["status"] = "done"
        task["last_verdict"] = VERDICT_PASS
    else:
        task["status"] = "pending"
        task["last_verdict"] = f"{VERDICT_FAIL_PREFIX}{verdict['reason']}"
        repeated = _record_task_failure(task, "verify", verdict["reason"])
        if repeated:
            _dev_pause_state(data, "repeated_failure")
            append_message("system", f"⏸ 任務 {task['id']} 相同驗證失敗連續出現第 2 次，管線暫停。")
            return False
    data["updated_at"] = _now()
    _save_tasks(data)
    return True


def _dev_run_arbitration(data, task):
    with _lock:
        _dev["stage"] = "arbitrate"
        _dev["current_task"] = task["id"]
    seat = _dev_roles["controller"]
    tasks_json = json.dumps(data, ensure_ascii=False, indent=1)
    instr = _dev_instruction("arbitrate", task=task, tasks_json=tasks_json)
    text, verdict = _protocol_call(
        seat, instr, _parse_arbitration, "arbitrate", task_id=task["id"], dev_role="controller", data=data)
    append_message(seat, text, sub=f"任務 {task['id']} · 仲裁")
    if verdict is None:
        _dev_pause_state(data, "parse_fail")
        append_message("system", f"⛔ 任務 {task['id']} 仲裁棒輸出連續兩次不符協議格式，管線暫停。")
        return False
    if verdict["action"] == "reassign":
        task["arbitrated"] = True
        task["attempts"] = 0
        task["status"] = "pending"
        task["last_verdict"] = f"{ARBITRATION_REASSIGN_PREFIX}{verdict['detail']}"
        task["last_failure_fingerprint"] = ""
        task["consecutive_same_failures"] = 0
        data["updated_at"] = _now()
        _save_tasks(data)
        return True
    if verdict["action"] == "skip":
        task["arbitrated"] = True
        task["status"] = "blocked"
        task["last_verdict"] = f"{ARBITRATION_SKIP_PREFIX}{verdict['detail']}"
        data["updated_at"] = _now()
        _save_tasks(data)
        return True
    # ask：暫停管線交主持人拍板。刻意不設 arbitrated——主持人回覆經消化棒後，
    # 該任務 attempts 仍達上限且未仲裁，會再次進入仲裁棒，主控此時已有裁示可據以重派或跳過（規格 §5）。
    task["last_verdict"] = f"{ARBITRATION_ASK_PREFIX}{verdict['detail']}"
    _dev_pause_state(data, "ask_host")
    append_message("system", f"⏸ 仲裁裁決為「詢問」，管線暫停等待主持人回應：{verdict['detail']}")
    return False


def _dev_run_digest(data):
    with _lock:
        _dev["stage"] = "digest"
        _dev["current_task"] = None
    seat = _dev_roles["controller"]
    tasks_json = json.dumps(data, ensure_ascii=False, indent=1)
    instr = _dev_instruction("digest", tasks_json=tasks_json)
    previous_summary = _valid_saved_summary()
    text, parsed = _protocol_call(
        seat, instr, lambda output: _parse_tasks(
            output, allow_empty=True, previous_summary=previous_summary),
                                  "digest", task_id=0, dev_role="controller", data=data)
    append_message(seat, text, sub="消化插話")
    if parsed is None:
        _dev_pause_state(data, "parse_fail")
        append_message("system", "⛔ 主控消化棒輸出連續兩次不符協議格式，管線暫停。")
        return False
    summary = {"version": 1, **parsed.summary, "updated_at": _now()}
    data["tasks"] = _merge_tasks(data["tasks"], parsed)
    data["updated_at"] = _now()
    _save_meeting_summary(summary)
    _save_tasks(data)
    return True


def _dev_run_integration_verify(data):
    with _lock:
        _dev["stage"] = "integration_verify"
        _dev["current_task"] = None
    seat = _dev_roles["verifier"]
    main_commit = data.get("main_commit")
    if not main_commit:
        raise RuntimeError("tasks.json 缺少管線 main_commit")
    diff_range, diff_block = _git_diff_summary(main_commit)
    task_briefs = [{
        key: task.get(key)
        for key in ("id", "title", "files", "acceptance", "status", "last_verdict")
    } for task in data.get("tasks", [])]
    instr = _dev_instruction(
        "integration_verify", diff_range=diff_range, diff_block=diff_block,
        tasks_json=json.dumps(task_briefs, ensure_ascii=False, indent=1),
    )
    text, verdict = _protocol_call(
        seat, instr, _parse_verdict, "integration-verify", task_id=0,
        dev_role="verifier", data=data,
    )
    append_message(seat, text, sub="收尾 · 整合驗證")
    if verdict is None:
        _dev_pause_state(data, "parse_fail")
        append_message("system", "⛔ 整合驗證輸出連續兩次不符協議格式，管線暫停。")
        return False
    if not verdict["passed"]:
        data["integration_verdict"] = f"{VERDICT_FAIL_PREFIX}{verdict['reason']}"
        _dev_pause_state(data, "integration_failed")
        append_message("system", f"⏸ 收尾整合驗證不通過，管線暫停：{verdict['reason']}")
        return False
    data["integration_verified"] = True
    data["integration_verdict"] = VERDICT_PASS
    data["updated_at"] = _now()
    _save_tasks(data)
    return True


def _dev_run_handoff(data):
    with _lock:
        _dev["stage"] = "handoff"
        _dev["current_task"] = None
    seat = _dev_roles["controller"]
    tasks_json = json.dumps(data, ensure_ascii=False, indent=1)
    instr = _dev_instruction("handoff", tasks_json=tasks_json)
    text = _call_seat_checked(seat, instr, "handoff", task_id=0, dev_role="controller", data=data)
    append_message(seat, text, sub="收尾")
    data["handoff"] = text
    data["status"] = "done"
    data["pause_reason"] = ""
    data["updated_at"] = _now()
    _save_tasks(data)
    with _lock:
        _dev["paused"] = False
        _dev["pause_reason"] = ""
        _dev["stage"] = "done"
        _dev["current_task"] = None


def _dev_next_action(data):
    """回傳 (action, task)：dispatch/handoff（無任務）或 implement/verify/arbitrate/auto_block（含任務）。"""
    if not data["tasks"]:
        if not data.get("dispatched"):
            return "dispatch", None
        return ("handoff" if data.get("integration_verified") else "integration_verify"), None
    for t in data["tasks"]:
        if t["status"] in ("done", "blocked"):
            continue
        if t["status"] == "pending" and t["attempts"] >= DEV_MAX_ATTEMPTS:
            if t.get("arbitrated"):
                return "auto_block", t
            return "arbitrate", t
        if t["status"] == "in_progress":
            return "verify", t
        return "implement", t
    return ("handoff" if data.get("integration_verified") else "integration_verify"), None


def _dev_pre_gate():
    """每棒開始前的安全網檢查。回傳 None 代表可以繼續，否則回傳 pause_reason。"""
    if not _check_tamper():
        return "tamper"
    with _lock:
        if _dev["turn_count"] >= DEV_MAX_TURNS:
            return "turn_cap"
    return None


def _dev_post_gate(baseline):
    """每棒結束後的檢查。回傳 'stop'（手動暫停生效）/ 'digest'（偵測到插話）/ 'continue'。"""
    global _dev_pause_requested
    with _lock:
        if _dev_pause_requested:
            _dev_pause_requested = False
            return "stop"
    with _lock:
        interjected = any(_is_host_message(m) for m in _messages[baseline[0]:])
    return "digest" if interjected else "continue"


def _dev_gate_or_pause():
    """跑每棒前置閘門；有問題時落盤暫停並回傳 True（代表管線要停線）。"""
    gate = _dev_pre_gate()
    if not gate:
        return False
    if gate == "tamper":
        _pause_tampered_data()
        return True
    data = _load_tasks()
    if data is not None:
        _dev_pause_state(data, gate)
    else:
        with _lock:
            _dev["paused"] = True
            _dev["pause_reason"] = gate
    return True


def _dev_digest_step():
    """跑一棒主控消化棒（含例外處理）；回傳 True 代表管線可繼續。"""
    data = _load_tasks()
    if data is None:
        return False
    try:
        return _dev_run_digest(data)
    except _PipelinePaused:
        return False
    except _RateLimited:
        append_message("system", "⏸ 偵測到限流／用量上限特徵，管線暫停。")
        _dev_pause_state(_load_tasks() or data, "rate_limit")
        return False
    except Exception as e:  # noqa: BLE001 - 未預期例外一律安全暫停，不讓執行緒靜默死掉
        append_message("system", f"⛔ 管線發生未預期錯誤，已暫停：{e}")
        _dev_pause_state(_load_tasks() or data, "seat_error")
        return False


def run_dev_pipeline(digest_first=False):
    baseline = [0]
    with _lock:
        baseline[0] = len(_messages)
    try:
        if digest_first:
            # 續作且暫停期間有新的人類發言（含仲裁「詢問」後主持人的拍板回覆）：
            # 第一棒先跑消化棒讓主控讀到裁示、更新任務清單，之後照常走 _dev_next_action（規格 §5）。
            if _dev_gate_or_pause():
                return
            if not _dev_digest_step():
                return
            with _lock:
                baseline[0] = len(_messages)
        while True:
            if _dev_gate_or_pause():
                return

            data = _load_tasks()
            if data is None:
                return
            action, task = _dev_next_action(data)

            if action == "auto_block":
                task["status"] = "blocked"
                data["updated_at"] = _now()
                _save_tasks(data)
                continue  # 伺服器端狀態轉換，不算一棒，不需要後置閘門檢查

            if action == "handoff":
                try:
                    _dev_run_handoff(data)
                except _PipelinePaused:
                    pass
                except _RateLimited:
                    append_message("system", "⏸ 偵測到限流／用量上限特徵，管線暫停。")
                    _dev_pause_state(_load_tasks() or data, "rate_limit")
                except Exception as e:  # noqa: BLE001 - 未預期例外一律安全暫停，不讓執行緒靜默死掉
                    append_message("system", f"⛔ 管線發生未預期錯誤，已暫停：{e}")
                    _dev_pause_state(_load_tasks() or data, "seat_error")
                return

            if action == "integration_verify":
                try:
                    ok = _dev_run_integration_verify(data)
                except _PipelinePaused:
                    return
                except _RateLimited:
                    append_message("system", "⏸ 偵測到限流／用量上限特徵，管線暫停。")
                    _dev_pause_state(_load_tasks() or data, "rate_limit")
                    return
                except Exception as e:
                    append_message("system", f"⛔ 整合驗證發生錯誤，已暫停：{e}")
                    _dev_pause_state(_load_tasks() or data, "seat_error")
                    return
                if not ok:
                    return
                outcome = _dev_post_gate(baseline)
                if outcome == "stop":
                    latest = _load_tasks()
                    if latest is not None:
                        _dev_pause_state(latest, "manual")
                    return
                if outcome == "digest":
                    if _dev_gate_or_pause():
                        return
                    if not _dev_digest_step():
                        return
                    with _lock:
                        baseline[0] = len(_messages)
                continue

            try:
                if action == "dispatch":
                    ok = _dev_run_dispatch(data)
                elif action == "implement":
                    ok = _dev_run_implement(data, task)
                elif action == "verify":
                    ok = _dev_run_verify(data, task)
                else:  # arbitrate
                    ok = _dev_run_arbitration(data, task)
            except _PipelinePaused:
                return
            except _RateLimited:
                append_message("system", "⏸ 偵測到限流／用量上限特徵，管線暫停（不計入該任務重試次數）。")
                _dev_pause_state(_load_tasks() or data, "rate_limit")
                return
            except Exception as e:  # noqa: BLE001 - 未預期例外一律安全暫停，不讓執行緒靜默死掉
                append_message("system", f"⛔ 管線發生未預期錯誤，已暫停：{e}")
                _dev_pause_state(_load_tasks() or data, "seat_error")
                return

            if not ok:
                return  # stage 內部已處理暫停狀態（parse_fail / ask_host）

            outcome = _dev_post_gate(baseline)
            if outcome == "stop":
                data = _load_tasks()
                if data is not None:
                    _dev_pause_state(data, "manual")
                return
            if outcome == "digest":
                if _dev_gate_or_pause():
                    return
                if not _dev_digest_step():
                    return
                with _lock:
                    baseline[0] = len(_messages)
    finally:
        with _lock:
            _dev["active"] = False


def _latest_human_message_text():
    for msg in reversed(_messages):
        if _is_human_message(msg):
            return msg.get("text", "")
    return ""


def _dev_start():
    with _lock:
        if _dev["active"]:
            return False, "開發管線已在執行中"

    if _trusted_tasks is not None and not _check_tamper():
        _pause_tampered_data()
        return False, "偵測到 data/ 管理檔案遭竄改，管線已暫停"

    data = _load_tasks()
    resuming = bool(data and data.get("status") in ("paused", "running"))
    digest_first = False
    if resuming:
        ok, reason = _git_repo_check()
        if not ok:
            append_message("system", f"⛔ 開發管線無法續作：{reason}")
            return False, reason
        stored_project = data.get("project_dir")
        if not isinstance(stored_project, str) or not _same_path(stored_project, _project_dir):
            reason = "tasks.json 記錄的專案目錄與目前專案不一致"
            append_message("system", f"⛔ 開發管線無法續作：{reason}")
            return False, reason
        branch = data.get("branch", "")
        if not isinstance(branch, str) or not branch.startswith(DEV_BRANCH_PREFIX):
            reason = "tasks.json 記錄的管線分支名稱無效"
            append_message("system", f"⛔ 開發管線無法續作：{reason}")
            return False, reason
        data.setdefault("dispatched", bool(data.get("tasks")))
        # 續作的髒 working tree（規格 §5）：實作棒中斷可能留下未 commit 的變更。
        # 在管線分支上 → 伺服器先 commit 殘留變更再續跑；不在管線分支上 → 維持拒絕。
        status = _git("status", "--porcelain")
        if status.returncode != 0:
            reason = f"git status 失敗：{(status.stderr or '').strip()}"
            append_message("system", f"⛔ 開發管線無法續作：{reason}")
            return False, reason
        if (status.stdout or "").strip():
            current_result = _git("branch", "--show-current")
            if current_result.returncode != 0:
                reason = f"讀取目前分支失敗：{(current_result.stderr or '').strip()}"
                append_message("system", f"⛔ 開發管線無法續作：{reason}")
                return False, reason
            current = (current_result.stdout or "").strip()
            if current != branch:
                reason = "working tree 有未提交的變更且目前不在管線分支上，請先手動處理"
                append_message("system", f"⛔ 開發管線無法續作：{reason}")
                return False, reason
            try:
                rev = _git_commit_leftover(data)
            except RuntimeError as e:
                append_message("system", f"⛔ 開發管線無法續作：{e}")
                return False, str(e)
            if rev:
                _save_tasks(data)
                append_message("system", f"已將中斷殘留的未提交變更 commit（{rev}）後續作。")
        try:
            _git_switch_branch(branch)
        except RuntimeError as e:
            append_message("system", f"⛔ 開發管線無法續作：{e}")
            return False, str(e)
        try:
            if _backfill_git_baselines(data):
                _save_tasks(data)
        except RuntimeError as e:
            append_message("system", f"⛔ 開發管線無法續作：{e}")
            return False, str(e)
        # 暫停期間的人類發言（含仲裁「詢問」後主持人的拍板）→ 續作第一棒先跑消化棒。
        # 舊檔或 crash 降級沒有水位紀錄（get 回 None）→ 保守起見有人類發言就先消化。
        watermark = data.get("message_watermark")
        digest_first = _latest_human_no() > (watermark if isinstance(watermark, int) else 0)
        # 總棒數上限的續作語意（規格 §5）：HOST 按「▶」視為重新授權一輪預算，計數歸零重計。
        data["turn_count"] = 0
        data["status"] = "running"
        data["pause_reason"] = ""
        data["updated_at"] = _now()
        _save_tasks(data)
    else:
        ok, reason = _git_precheck()
        if not ok:
            append_message("system", f"⛔ 開發管線無法啟動：{reason}")
            return False, reason
        latest = _latest_human_message_text()
        if not latest:
            return False, "尚未有主持人發言可作為開發目標，請先輸入開發目標"
        try:
            branch = _git_new_branch()
        except RuntimeError as e:
            append_message("system", f"⛔ 開發管線無法啟動：{e}")
            return False, str(e)
        data = {
            "version": 2, "status": "running", "pause_reason": "",
            "branch": branch, "project_dir": _project_dir,
            "session_goal": latest, "turn_count": 0,
            "message_watermark": _latest_human_no(), "dispatched": False,
            "main_commit": _git_head(), "integration_verified": False,
            "sessions": {}, "usage": _empty_usage_state(),
            "updated_at": _now(), "tasks": [], "handoff": "",
        }
        _save_tasks(data)

    global _dev_pause_requested
    with _lock:
        _dev.update({
            "active": True, "paused": False, "pause_reason": "",
            "stage": "", "current_task": None,
            "turn_count": data.get("turn_count", 0), "branch": branch,
        })
        _dev_pause_requested = False
    threading.Thread(target=run_dev_pipeline, kwargs={"digest_first": digest_first}, daemon=True).start()
    return True, ""


def _dev_request_pause():
    global _dev_pause_requested
    with _lock:
        _dev_pause_requested = True


def _dev_pipeline_engaged():
    # 插話路由判斷（規格 §5）：管線執行中、或未收尾的暫停期間，人類訊息只入逐字稿
    # 由管線消化（執行中靠棒後檢查、暫停靠續作水位），不得啟動一般圓桌回覆。
    # 呼叫者不可持有 _lock。
    with _lock:
        return _dev["active"] or _dev["paused"]


def _dev_roles_custom():
    # 非預設角色映射（規格 §4）：能力與安全性由使用者自行確認；此旗標供 UI 顯示警告。
    return dict(_dev_roles) != DEFAULT_DEV_ROLES


def _dev_downgrade_crash():
    # 伺服器重啟時發現 tasks.json 仍是「執行中」→ 一定是異常結束（crash），降級為暫停。
    data = _load_tasks()
    if data and data.get("status") == "running":
        data["status"] = "paused"
        data["pause_reason"] = "crash"
        data["updated_at"] = _now()
        _save_tasks(data)


def _dev_load_from_tasks():
    # 啟動時把 tasks.json 現況同步進 _dev（記憶體狀態不持久化，重啟後預設值不反映實際暫停中）。
    data = _load_tasks()
    if not data:
        return
    _remember_loaded_tasks(data)
    _remember_loaded_meeting_summary(_load_meeting_summary())
    with _lock:
        _dev["active"] = False
        _dev["paused"] = data.get("status") == "paused"
        _dev["pause_reason"] = data.get("pause_reason", "") if _dev["paused"] else ""
        _dev["branch"] = data.get("branch", "")
        _dev["turn_count"] = data.get("turn_count", 0)
        _dev["stage"] = ""
        _dev["current_task"] = None


def _dev_payload():
    # 呼叫者必須已持有 _lock（threading.Lock 不可重入，內部再取鎖會死鎖）。
    # 目前唯一呼叫點是 /api/state 的鎖內；鎖內做一次 tasks.json 小檔案讀取 v1 可接受。
    data = _load_tasks() if DEVMODE else None
    total = len(data.get("tasks", [])) if data else 0
    return {
        "devmode": DEVMODE, "roles": dict(_dev_roles), "roles_custom": _dev_roles_custom(),
        "active": _dev["active"], "paused": _dev["paused"],
        "pause_reason": _dev["pause_reason"], "stage": _dev["stage"],
        "current_task": _dev["current_task"], "task_total": total,
        "turn_count": _dev["turn_count"], "branch": _dev["branch"],
        "usage": data.get("usage", _empty_usage_state()) if data else _empty_usage_state(),
    }


def _participants_payload():
    return {
        name: {
            "display": p["display"], "color": p["color"],
            "models": [m["label"] for m in p["models"]],
            "selected": _selected[name],
        }
        for name, p in PARTICIPANTS.items()
    }


def _sessions_for_project(project_dir):
    items = [s for s in _load_sessions() if _same_path(s.get("project_dir", ""), project_dir)]
    return sorted(items, key=lambda s: s.get("archived_at", ""), reverse=True)


def _active_message_count():
    if _messages:
        return len(_messages)
    if not TRANSCRIPT.exists():
        return 0
    return sum(1 for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines() if line.strip())


def _print_active_session():
    print("\n\u76ee\u524d\u6703\u8b70\uff08\u76f4\u63a5 Enter \u6703\u6cbf\u7528\u9019\u500b\uff09:")
    created = (_active_session.get("created_at") or "")[:16]
    title = _session_title()
    count = _active_message_count()
    print(f"[*] {created or 'active'}  {count} messages  {title}")


def _rename_session(session_id, title):
    sessions = _load_sessions()
    for s in sessions:
        if s.get("id") == session_id:
            s["title"] = (title or "").strip() or UNNAMED_TITLE
            _save_sessions(sessions)
            return True
    return False


def _prompt_project_dir():
    global _project_dir
    if not sys.stdin.isatty():
        return
    print("\n目前專案資料夾:")
    print(_project_dir)
    try:
        raw = input("\n輸入新的專案資料夾，或直接 Enter 沿用目前設定:\n> ").strip().strip('"')
    except EOFError:
        return
    if not raw:
        return
    candidate = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if not Path(candidate).is_dir():
        print(f"找不到資料夾，沿用目前設定：{_project_dir}")
        return
    if not _same_path(candidate, _project_dir):
        _archive_active_session()
        _project_dir = candidate
        _active_session["title"] = UNNAMED_TITLE
        _active_session["created_at"] = _now()
        _save_settings()


def _print_session_menu(items):
    _print_active_session()
    print("\n找到此專案的歸檔會議:")
    for i, s in enumerate(items, 1):
        when = s.get("archived_at", "")[:16]
        title = s.get("title") or UNNAMED_TITLE
        count = s.get("message_count", 0)
        print(f"[{i}] {when}  {count} messages  {title}")


def _prompt_restore_session():
    if not sys.stdin.isatty():
        return
    while True:
        items = _sessions_for_project(_project_dir)[:10]
        if not items:
            return
        _print_session_menu(items)
        try:
            choice = input("\n\u8f38\u5165\u7de8\u865f\u6062\u5fa9\uff0c\u8f38\u5165 r \u7de8\u865f\u91cd\u65b0\u547d\u540d\uff0c\u6216\u76f4\u63a5 Enter \u6cbf\u7528\u76ee\u524d\u6703\u8b70:\n> ").strip()
        except EOFError:
            return
        if not choice:
            return
        parts = choice.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "r" and parts[1].isdigit():
            idx = int(parts[1]) - 1
            if 0 <= idx < len(items):
                new_title = input("新的名稱:\n> ").strip()
                _rename_session(items[idx]["id"], new_title)
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                try:
                    _restore_session(items[idx])
                    print(f"已恢復會議：{_session_title()}")
                except Exception as e:  # noqa: BLE001 - CLI 選單要顯示可讀錯誤
                    print(f"恢復失敗：{e}")
                return
        print("輸入格式無效。")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._set_cookie = None
        self.end_headers()
        self.wfile.write(body)

    def _check_host(self):
        # DNS-rebinding 防護：在任何路由／授權邏輯之前驗證 Host header。
        # 缺 Host 或不在白名單一律拒絕（fail closed）。
        if not _host_allowed(self.headers.get("Host")):
            self._json({"error": "forbidden host"}, 403)
            return False
        return True

    def _current_session(self):
        sid = _read_cookie_sid(self.headers)
        with _lock:
            session = _auth_sessions.get(sid) if sid else None
            if session:
                session["last_seen"] = time.time()
                return session
            # PUBLIC 模式下不能信任來源 IP：Funnel 轉發的公網流量在本機也是 127.0.0.1，
            # 一律要靠 HOST 進場碼 / 邀請碼，避免任何訪客被自動當成 HOST。
            if _is_loopback(self.client_address[0]) and not PUBLIC_MODE:
                sid, session = _new_auth_session("host", "HOST")
                self._set_cookie = _cookie_header(sid)
                return session
        return None

    def _require_session(self):
        session = self._current_session()
        if not session:
            self._json({"error": "unauthorized"}, 401)
            return None
        return session

    def _require_host(self):
        session = self._require_session()
        if not session:
            return None
        if session.get("role") != "host":
            self._json({"error": "host required"}, 403)
            return None
        return session

    def _send_index(self):
        self._current_session()
        body = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._set_cookie = None
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_host():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/" or path.startswith("/index"):
            self._send_index()
        elif path == "/api/state":
            session = self._require_session()
            if not session:
                return
            since = 0
            values = parse_qs(parsed.query).get("since")
            if values:
                try:
                    since = int(values[0])
                except ValueError:
                    since = 0
            with _lock:
                self._json({
                    "total": len(_messages),
                    "messages": _messages[since:],
                    "busy": sorted(_busy.keys()),
                    "participants": _participants_payload(),
                    "enabled_seats": list(_enabled_seats),
                    "project_dir": _project_dir,
                    "session_title": _session_title(),
                    "discussion": dict(_discussion),
                    "session": _session_payload(session),
                    "dev": _dev_payload(),
                })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_host():
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if self.path == "/api/token/verify":
            if DEVMODE:  # 開發模式停用邀請／guest 加入：單人本機工作模式。
                self._json({"error": "invites disabled in dev mode"}, 403)
                return
            result = _redeem_invite(payload.get("token"), payload.get("name"))
            if not result:
                self._json({"error": "invalid or expired token"}, 401)
                return
            sid, session = result
            self._set_cookie = _cookie_header(sid)
            self._json({"ok": True, "session": _session_payload(session)})
            return

        if self.path == "/api/token/generate":
            if DEVMODE:  # 開發模式停用邀請／guest 加入：單人本機工作模式。
                self._json({"error": "invites disabled in dev mode"}, 403)
                return
            session = self._require_host()
            if not session:
                return
            role = payload.get("role") or "guest"
            result = _create_invite(role)
            if not result:
                self._json({"error": "bad role"}, 400)
                return
            token, invite = result
            if PUBLIC_URL:
                # 公網 Funnel 網址（https、無 port），讓沒裝 Tailscale 的 guest 也能直接開。
                url = f"{PUBLIC_URL}/?invite={token}"
            else:
                host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
                host_name = host.split(":", 1)[0].lower()
                if host_name in {"localhost", "127.0.0.1"}:
                    ts_ip = _tailscale_ip()
                    if ts_ip:
                        host = f"{ts_ip}:{PORT}"
                url = f"http://{host}/?invite={token}"
            self._json({
                "ok": True,
                "role": invite["role"],
                "token": token,
                "expires_in": INVITE_TTL_SECONDS,
                "url": url,
            })
            return

        session = self._require_session()
        if not session:
            return

        if self.path == "/api/send":
            text = (payload.get("text") or "").strip()
            role = session.get("role")
            name = session.get("name", "HOST" if role == "host" else "Guest")
            speaker = "你" if role == "host" else name
            if _dev_pipeline_engaged():
                # 插話不得啟動一般圓桌回覆（規格 §5）：訊息只入逐字稿標記為待消化，
                # 不呼叫 start_batch／start_discussion；mode=discussion 同樣被抑制。
                if text:
                    append_message(speaker, text, role=role, name=name)
                self._json({"ok": True, "dev_pending": True})
                return
            with _lock:
                discussing = _discussion["active"]
            if discussing:
                if role != "host":
                    self._json({"error": "host required"}, 403)
                    return
                # Discussion is active: host messages interrupt it without starting another call.
                if text:
                    append_message(speaker, text, role=role, name=name)
                self._json({"ok": True, "interrupted": True})
            elif payload.get("mode") == "discussion":
                if role != "host":
                    self._json({"error": "host required"}, 403)
                    return
                if text:
                    append_message(speaker, text, role=role, name=name)
                names = payload.get("ask") or []
                self._json({"ok": True, "discussion": start_discussion(names, payload.get("max_rounds", 3))})
            else:
                if text:
                    append_message(speaker, text, role=role, name=name)
                # HOST發言帶著席位選擇（更新持久化 enabled seats）；
                # guest 傳 None，沿用目前已啟用的席位自動回覆。
                names = (payload.get("ask") or []) if role == "host" else None
                started = start_batch(names, reset_auto=(role == "host"))
                self._json({"ok": True, "started": started})
        elif self.path == "/api/ask":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            if _dev_pipeline_engaged():
                # 插話不得啟動一般圓桌回覆（規格 §5）：管線期間拒絕手動徵詢席位。
                self._json({"error": "開發管線進行中或尚未收尾，一般圓桌回覆已暫停；發言將由管線消化"}, 409)
                return
            self._json({"ok": True, "started": start_batch(payload.get("names"), reset_auto=True)})
        elif self.path == "/api/cancel":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            self._json({"ok": True, "cancelled": cancel_call(payload.get("name"))})
        elif self.path == "/api/title":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            title = (payload.get("title") or "").strip() or UNNAMED_TITLE
            with _lock:
                _set_session_title(title)
            self._json({"ok": True, "title": _session_title()})
        elif self.path == "/api/config":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            name = payload.get("name")
            label = payload.get("label")
            if name in PARTICIPANTS and label in [m["label"] for m in PARTICIPANTS[name]["models"]]:
                with _lock:
                    _selected[name] = label
                    _save_settings()
                self._json({"ok": True})
            else:
                self._json({"error": "bad name/label"}, 400)
        elif self.path == "/api/enabled":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            name = payload.get("name")
            if name not in PARTICIPANTS:
                self._json({"error": "bad name"}, 400)
                return
            with _lock:
                seats = set(_enabled_seats)
                seats.add(name) if payload.get("on") else seats.discard(name)
                _set_enabled_seats(seats)
                enabled = list(_enabled_seats)
            self._json({"ok": True, "enabled_seats": enabled})
        elif self.path == "/api/new":
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            with _lock:
                dev_active = _dev["active"]
                dev_unfinished = _dev["paused"]
            if dev_active:
                self._json({"error": "開發管線執行中，無法開新會議"}, 409)
                return
            if dev_unfinished:
                self._json({"error": "開發管線已暫停但尚未收尾，無法開新會議"}, 409)
                return
            with _lock:
                archived = _archive_active_session()
            self._json({"ok": True, "archived": archived, "session_title": _session_title()})
        elif self.path == "/api/dev/start":
            if not DEVMODE:
                self._json({"error": "dev mode required"}, 403)
                return
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            ok, reason = _dev_start()
            if not ok:
                self._json({"error": reason}, 400)
                return
            self._json({"ok": True})
        elif self.path == "/api/dev/pause":
            if not DEVMODE:
                self._json({"error": "dev mode required"}, 403)
                return
            if session.get("role") != "host":
                self._json({"error": "host required"}, 403)
                return
            _dev_request_pause()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def _tailscale_ip():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=10)
        lines = (out.stdout or "").strip().splitlines()
        return lines[0].strip() if out.returncode == 0 and lines else None
    except Exception:  # noqa: BLE001 - tailscale 沒裝 / 沒開就只綁 loopback
        return None


def _tailscale_funnel_url():
    # 本機 MagicDNS 名稱即 Funnel 的公開網址（https、443）。用於自動填 guest 邀請連結。
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode != 0:
            return None
        name = ((json.loads(out.stdout or "{}").get("Self") or {}).get("DNSName") or "").rstrip(".")
        return f"https://{name}" if name else None
    except Exception:  # noqa: BLE001 - tailscale 沒裝 / 沒開就沒有公開網址
        return None


def _open_browser_later(url=None):
    if os.environ.get("AI_ROUNDTABLE_NO_BROWSER") == "1":
        return
    if os.name != "nt":
        return
    target = url or f"http://127.0.0.1:{PORT}/"

    def opener():
        try:
            os.startfile(target)  # noqa: S606 - local convenience launcher
        except OSError:
            pass

    threading.Timer(0.5, opener).start()


def main():
    if DEVMODE and PUBLIC_MODE:
        # 開發模式的可寫權限與 PUBLIC 模式對外曝露互斥：兩者同時開等於把有筆的
        # 席位攤在公網前，直接拒絕啟動而不是挑一邊默默生效。
        print(
            "error: AI_ROUNDTABLE_DEVMODE=1 與 AI_ROUNDTABLE_PUBLIC=1 不能同時啟用"
            "（開發模式的可寫席位不可對外公開曝露）。",
            file=sys.stderr,
        )
        sys.exit(1)
    _load_settings()
    _load()
    if DEVMODE:
        # 伺服器重啟時發現 tasks.json 是「執行中」一律降級為暫停（規格 §5 驗收）；
        # 接著把現況同步進記憶體中的 _dev，讓 /api/state 立刻反映真實暫停狀態。
        _dev_downgrade_crash()
        _dev_load_from_tasks()
    _prompt_project_dir()
    _prompt_restore_session()
    for path, label in [(AGY_EXE, "agy"), (CODEX_CMD, "codex")]:
        if not Path(path).exists():
            print(f"warning: {label} not found at {path}", file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ai-roundtable listening on http://127.0.0.1:{PORT}")
    print(f"project_dir: {_project_dir}")
    print(f"session_title: {_session_title()}")
    if PUBLIC_MODE:
        global _host_bootstrap_token, PUBLIC_URL
        _host_bootstrap_token = secrets.token_urlsafe(24)
        if not PUBLIC_URL:  # 未手動指定就從 Tailscale 自動抓公開網址
            PUBLIC_URL = _tailscale_funnel_url() or ""
        host_link = f"http://127.0.0.1:{PORT}/?invite={_host_bootstrap_token}"
        print("\n[PUBLIC 模式] loopback 自動 HOST 已關閉；公網訪客一律要邀請碼。")
        print(f"[PUBLIC 模式] HOST 進場連結（僅本機、勿外流）: {host_link}")
        if PUBLIC_URL:
            print(f"[PUBLIC 模式] guest 邀請連結網址: {PUBLIC_URL}/?invite=<token>")
        else:
            print("[PUBLIC 模式] warning: 抓不到 Tailscale 公開網址，"
                  "請設 AI_ROUNDTABLE_PUBLIC_URL，否則 guest 邀請連結無法從公網開啟", file=sys.stderr)
        _open_browser_later(host_link)  # 直接開 HOST 進場連結，省得手動貼
    else:
        _open_browser_later()
    # 開發模式只綁 loopback：不嘗試偵測／綁定 Tailscale IP，避免有可寫席位的
    # 管線經區網／Tailscale 曝露。
    ts_ip = None if DEVMODE else _tailscale_ip()
    # DNS-rebinding 防護：把本次執行才知道的合法主機名補進白名單，否則 Tailscale 綁定
    # 或公網 Funnel 訪客會被 Host 檢查擋成 403。_tailscale_* 傳回 None 就略過（不 crash）。
    if PUBLIC_MODE:
        for u in (PUBLIC_URL, _tailscale_funnel_url()):
            h = urlsplit(u).hostname if u else None
            if h:
                _ALLOWED_HOSTS.add(h.lower())
    if ts_ip:
        _ALLOWED_HOSTS.add(ts_ip.lower())
        try:
            ts_server = ThreadingHTTPServer((ts_ip, PORT), Handler)
            threading.Thread(target=ts_server.serve_forever, daemon=True).start()
            print(f"ai-roundtable also listening on http://{ts_ip}:{PORT} (tailscale)")
        except OSError as e:
            print(f"warning: tailscale bind failed: {e}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
