# ai-roundtable: 多 AI 圓桌討論室（localhost 單機工具）
# 參與者 = CLI 呼叫配方；逐字稿(jsonl + md 鏡像)是唯一資料層。
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
_selected = {}  # name -> label
_project_dir = DEFAULT_PROJECT_DIR
_active_session = {"title": UNNAMED_TITLE, "created_at": None}


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
    if isinstance(raw.get("active_session"), dict):
        settings["active_session"].update(raw["active_session"])
    return settings


def _load_settings():
    global _project_dir, _active_session
    settings = _coerce_settings(_read_json(SETTINGS, {}))
    project = settings.get("project_dir") or DEFAULT_PROJECT_DIR
    _project_dir = project if Path(project).is_dir() else DEFAULT_PROJECT_DIR

    for name, p in PARTICIPANTS.items():
        labels = [m["label"] for m in p["models"]]
        saved = settings["participants"].get(name)
        default = DEFAULT_SELECTIONS.get(name, labels[0])
        _selected[name] = saved if saved in labels else default

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


def _archive_title():
    title = _session_title()
    if title != UNNAMED_TITLE:
        return title
    for msg in _messages:
        if msg.get("speaker") == "你":
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


def _rebuild_md():
    parts = ["# 圓桌會議逐字稿", ""]
    for i, m in enumerate(_messages, 1):
        who = m["speaker"]
        disp = PARTICIPANTS.get(who, {}).get("display", who)
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


def append_message(speaker, text, sub=None):
    msg = {"speaker": speaker, "text": text.strip(), "ts": _now()}
    if sub:
        msg["sub"] = sub
    with _lock:
        if not _messages and speaker == "你" and _session_title() == UNNAMED_TITLE:
            _active_session["title"] = _title_from_text(text)
            _save_settings()
        _messages.append(msg)
        with TRANSCRIPT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        _rebuild_md()
    return msg


def _instruction(name):
    disp = PARTICIPANTS[name]["display"]
    return (
        f"你是多 AI 圓桌規格討論會的參與者「{disp}」。\n"
        f"1. 先讀取逐字稿檔案（UTF-8）：{MD_MIRROR} —— 這是到目前為止的完整討論。\n"
        f"2. 討論主題通常圍繞位於 {_project_dir} 的專案，你可以唯讀查閱專案檔案來佐證論點。\n"
        f"3. 針對逐字稿最後的最新發言，以「{disp}」的身分發表一則回應：同意就說為什麼、"
        f"反對就給理由與替代方案、看到風險就指出來。直接輸出發言內容本身，"
        f"不要任何前綴、署名、標題或 markdown 程式碼圍欄。\n"
        f"4. 用繁體中文，發言精煉聚焦。絕對不要建立、修改或刪除任何檔案。\n"
    )


def _find_claude():
    # 桌面 app (MSIX) 打包的 CLI；版本資料夾會隨更新換號，取最新版。
    base = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code"))
    candidates = list(base.glob("*/claude.exe")) if base.exists() else []

    def ver_key(p):
        return [int(x) if x.isdigit() else 0 for x in p.parent.name.split(".")]

    return str(max(candidates, key=ver_key)) if candidates else None


def _call_codex(instr, opt, env_extra=None):
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
        proc = subprocess.run(
            args, input=instr, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=CALL_TIMEOUT, env=env, shell=False,
        )
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


def _call_agy(instr, opt):
    proc = subprocess.run(
        [AGY_EXE, "-p", instr, "--model", opt["model"],
         "--add-dir", _project_dir, "--add-dir", str(DATA),
         "--dangerously-skip-permissions", "--print-timeout", f"{CALL_TIMEOUT - 60}s"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT,
    )
    result = (proc.stdout or "").strip()
    if not result:
        raise RuntimeError("agy 沒有輸出。stderr：" + (proc.stderr or "")[-500:])
    return result


def _call_claude(instr, opt):
    exe = _find_claude()
    if not exe:
        raise RuntimeError("找不到 claude.exe（桌面 app 的 claude-code 資料夾不存在？）")
    args = [exe, "-p", instr, "--model", opt["model"],
            "--allowedTools", "Read,Glob,Grep,WebSearch,WebFetch", "--add-dir", str(DATA)]
    if opt.get("effort"):
        args += ["--effort", opt["effort"]]
    proc = subprocess.run(
        args, cwd=_project_dir, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=CALL_TIMEOUT,
    )
    result = (proc.stdout or "").strip()
    if proc.returncode != 0 or not result:
        raise RuntimeError(f"claude exit={proc.returncode}。stderr：" + (proc.stderr or "")[-500:])
    return result


ADAPTERS = {
    "codex": lambda instr, opt: _call_codex(instr, opt),
    "ds": lambda instr, opt: _call_codex(instr, opt, {"CODEX_HOME": DS_CODEX_HOME}),
    "agy": _call_agy,
    "claude": _call_claude,
}


def _worker(name):
    opt = _option(name)
    try:
        text = ADAPTERS[name](_instruction(name), opt)
        append_message(name, text, sub=opt["label"])
    except Exception as e:  # noqa: BLE001 - 任何失敗都要回報進聊天室
        append_message("system", f"⚠ {PARTICIPANTS[name]['display']}（{opt['label']}）呼叫失敗：{e}")
    finally:
        with _lock:
            _busy.pop(name, None)


def ask(names):
    started = []
    for name in names:
        if name not in ADAPTERS:
            continue
        with _lock:
            if name in _busy:
                continue
            _busy[name] = time.time()
        threading.Thread(target=_worker, args=(name,), daemon=True).start()
        started.append(name)
    return started


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
            choice = input("\n輸入編號恢復，輸入 r 編號重新命名，或直接 Enter 開新會議:\n> ").strip()
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/state"):
            since = 0
            if "since=" in self.path:
                try:
                    since = int(self.path.split("since=")[1].split("&")[0])
                except ValueError:
                    since = 0
            with _lock:
                self._json({
                    "total": len(_messages),
                    "messages": _messages[since:],
                    "busy": sorted(_busy.keys()),
                    "participants": _participants_payload(),
                    "project_dir": _project_dir,
                    "session_title": _session_title(),
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
        if self.path == "/api/send":
            text = (payload.get("text") or "").strip()
            speaker = payload.get("speaker") or "你"
            if text:
                append_message(speaker, text)
            started = ask(payload.get("ask") or [])
            self._json({"ok": True, "started": started})
        elif self.path == "/api/ask":
            self._json({"ok": True, "started": ask(payload.get("names") or [])})
        elif self.path == "/api/title":
            title = (payload.get("title") or "").strip() or UNNAMED_TITLE
            with _lock:
                _set_session_title(title)
            self._json({"ok": True, "title": _session_title()})
        elif self.path == "/api/config":
            name = payload.get("name")
            label = payload.get("label")
            if name in PARTICIPANTS and label in [m["label"] for m in PARTICIPANTS[name]["models"]]:
                with _lock:
                    _selected[name] = label
                    _save_settings()
                self._json({"ok": True})
            else:
                self._json({"error": "bad name/label"}, 400)
        elif self.path == "/api/new":
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

