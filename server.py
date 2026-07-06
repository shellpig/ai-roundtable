# ai-roundtable: 多 AI 圓桌討論室（localhost 單機工具）
# 參與者 = CLI 呼叫配方；逐字稿(jsonl + md 鏡像)是唯一資料層。
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
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
_batch_watermark = {}  # seat name -> latest human message number successfully handled
_enabled_seats = []  # persisted AI seats that auto-answer any human message (host or guest)
_batch_auto_rounds = 0
_batch_blocked = False


class CallCancelled(Exception):
    pass


def _is_loopback(ip):
    return ip == "::1" or ip.startswith("127.")


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
    _clean_invites()
    invite = _invites.pop((token or "").strip(), None)
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


def _default_settings():
    return {
        "project_dir": DEFAULT_PROJECT_DIR,
        "participants": DEFAULT_SELECTIONS.copy(),
        "enabled_seats": _default_enabled_seats(),
        "active_session": {"title": UNNAMED_TITLE, "created_at": _now()},
    }


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
    return settings


def _load_settings():
    global _project_dir, _active_session, _enabled_seats
    settings = _coerce_settings(_read_json(SETTINGS, {}))
    project = settings.get("project_dir") or DEFAULT_PROJECT_DIR
    _project_dir = project if Path(project).is_dir() else DEFAULT_PROJECT_DIR

    for name, p in PARTICIPANTS.items():
        labels = [m["label"] for m in p["models"]]
        saved = settings["participants"].get(name)
        default = DEFAULT_SELECTIONS.get(name, labels[0])
        _selected[name] = saved if saved in labels else default

    _enabled_seats = _normalize_seats(settings.get("enabled_seats"))

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
        if not transcript.exists() and not mirror.exists():
            return suffix, transcript, mirror
        suffix = f"{stamp}-{counter}"
        counter += 1


def _archive_active_session(reset_title=True):
    global _active_session
    if not _messages:
        if reset_title:
            _active_session = {"title": UNNAMED_TITLE, "created_at": _now()}
            _save_settings()
        _reset_batch_state()
        return None

    sid, archived_transcript, archived_mirror = _unique_archive_paths(_stamp())
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

    entry = {
        "id": sid,
        "project_dir": _project_dir,
        "title": _archive_title(),
        "created_at": _active_session.get("created_at") or "",
        "archived_at": _now(),
        "message_count": len(_messages),
        "transcript_path": archived_transcript.name,
        "mirror_path": archived_mirror.name,
    }
    sessions = _load_sessions()
    sessions.append(entry)
    _save_sessions(sessions)
    _messages.clear()
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
    _project_dir = entry.get("project_dir") or _project_dir
    _active_session = {
        "title": entry.get("title") or UNNAMED_TITLE,
        "created_at": entry.get("created_at") or _now(),
    }
    sessions = [s for s in _load_sessions() if s.get("id") != entry.get("id")]
    _save_sessions(sessions)
    _save_settings()
    _load()


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
            raise
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


def _call_codex(name, instr, opt, env_extra=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    args = [CODEX_CMD, "exec", "-", "--sandbox", "read-only", "--skip-git-repo-check",
            "-C", _project_dir, "--ephemeral", "--color", "never", "-m", opt["model"]]
    if opt.get("effort"):
        args += ["-c", f"model_reasoning_effort={opt['effort']}"]
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False, encoding="utf-8") as tf:
        out_path = tf.name
    args += ["-o", out_path]
    try:
        proc = _run_process(name, args, input_text=instr, env=env, timeout=CALL_TIMEOUT)
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


def _call_agy(name, instr, opt):
    args = [
        AGY_EXE, "-p", instr, "--model", opt["model"],
        "--add-dir", _project_dir, "--add-dir", str(DATA),
        "--dangerously-skip-permissions", "--print-timeout", f"{CALL_TIMEOUT - 60}s",
    ]
    proc = _run_process(name, args, stdin=subprocess.DEVNULL, timeout=CALL_TIMEOUT)
    result = (proc.stdout or "").strip()
    if not result:
        raise RuntimeError("agy 沒有輸出。stderr：" + (proc.stderr or "")[-500:])
    return result


def _call_claude(name, instr, opt):
    exe = _find_claude()
    if not exe:
        raise RuntimeError("找不到 claude.exe（桌面 app 的 claude-code 資料夾不存在？）")
    args = [exe, "-p", instr, "--model", opt["model"],
            "--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch", "--add-dir", str(DATA)]
    if opt.get("effort"):
        args += ["--effort", opt["effort"]]
    proc = _run_process(name, args, cwd=_project_dir, stdin=subprocess.DEVNULL, timeout=CALL_TIMEOUT)
    result = (proc.stdout or "").strip()
    if proc.returncode != 0 or not result:
        raise RuntimeError(f"claude exit={proc.returncode}。stderr：" + (proc.stderr or "")[-500:])
    return result


ADAPTERS = {
    "codex": lambda name, instr, opt: _call_codex(name, instr, opt),
    "ds": lambda name, instr, opt: _call_codex(name, instr, opt, {"CODEX_HOME": DS_CODEX_HOME}),
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

    def _current_session(self):
        sid = _read_cookie_sid(self.headers)
        with _lock:
            session = _auth_sessions.get(sid) if sid else None
            if session:
                session["last_seen"] = time.time()
                return session
            if _is_loopback(self.client_address[0]):
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
                })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        if self.path == "/api/token/verify":
            result = _redeem_invite(payload.get("token"), payload.get("name"))
            if not result:
                self._json({"error": "invalid or expired token"}, 401)
                return
            sid, session = result
            self._set_cookie = _cookie_header(sid)
            self._json({"ok": True, "session": _session_payload(session)})
            return

        if self.path == "/api/token/generate":
            session = self._require_host()
            if not session:
                return
            role = payload.get("role") or "guest"
            result = _create_invite(role)
            if not result:
                self._json({"error": "bad role"}, 400)
                return
            token, invite = result
            host = self.headers.get("Host") or f"127.0.0.1:{PORT}"
            host_name = host.split(":", 1)[0].lower()
            if host_name in {"localhost", "127.0.0.1"}:
                ts_ip = _tailscale_ip()
                if ts_ip:
                    host = f"{ts_ip}:{PORT}"
            self._json({
                "ok": True,
                "role": invite["role"],
                "token": token,
                "expires_in": INVITE_TTL_SECONDS,
                "url": f"http://{host}/?invite={token}",
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
                archived = _archive_active_session()
            self._json({"ok": True, "archived": archived, "session_title": _session_title()})
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


def _open_browser_later():
    if os.environ.get("AI_ROUNDTABLE_NO_BROWSER") == "1":
        return
    if os.name != "nt":
        return

    def opener():
        try:
            os.startfile(f"http://127.0.0.1:{PORT}/")  # noqa: S606 - local convenience launcher
        except OSError:
            pass

    threading.Timer(0.5, opener).start()


def main():
    _load_settings()
    _load()
    _prompt_project_dir()
    _prompt_restore_session()
    for path, label in [(AGY_EXE, "agy"), (CODEX_CMD, "codex")]:
        if not Path(path).exists():
            print(f"warning: {label} not found at {path}", file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ai-roundtable listening on http://127.0.0.1:{PORT}")
    print(f"project_dir: {_project_dir}")
    print(f"session_title: {_session_title()}")
    _open_browser_later()
    ts_ip = _tailscale_ip()
    if ts_ip:
        try:
            ts_server = ThreadingHTTPServer((ts_ip, PORT), Handler)
            threading.Thread(target=ts_server.serve_forever, daemon=True).start()
            print(f"ai-roundtable also listening on http://{ts_ip}:{PORT} (tailscale)")
        except OSError as e:
            print(f"warning: tailscale bind failed: {e}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()

