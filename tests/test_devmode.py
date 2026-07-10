import json
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import server


class DevmodeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.project_dir = self.root / "project"
        self.data_dir.mkdir()
        self.project_dir.mkdir()

        replacements = {
            "DATA": self.data_dir,
            "TRANSCRIPT": self.data_dir / "transcript.jsonl",
            "MD_MIRROR": self.data_dir / "roundtable.md",
            "SETTINGS": self.data_dir / "settings.json",
            "SESSIONS": self.data_dir / "sessions.json",
            "TASKS_FILE": self.data_dir / "tasks.json",
            "DEVLOG_DIR": self.data_dir / "devlogs",
            "_project_dir": str(self.project_dir),
            "_messages": [],
            "_selected": dict(server.DEFAULT_SELECTIONS),
            "_active_session": {"title": "test", "created_at": "now"},
            "_dev_roles": dict(server.DEFAULT_DEV_ROLES),
            "_dev": {
                "active": False, "paused": False, "pause_reason": "",
                "stage": "", "current_task": None, "turn_count": 0, "branch": "",
            },
            "_last_run": {},
            "_data_hashes": {},
            "_trusted_tasks": None,
            "_devlog_seq": 0,
            "ADAPTERS": dict(server.ADAPTERS),
        }
        self.patchers = [mock.patch.object(server, name, value) for name, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()
        for path in (server.TRANSCRIPT, server.MD_MIRROR, server.TASKS_FILE):
            server._record_hash(path)

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    @staticmethod
    def task(task_id=1, status="pending", attempts=0, arbitrated=False):
        return {
            "id": task_id,
            "title": f"task {task_id}",
            "files": [f"file{task_id}.py"],
            "acceptance": ["works"],
            "status": status,
            "attempts": attempts,
            "arbitrated": arbitrated,
            "commits": [],
            "last_verdict": "",
        }

    def state(self, tasks=None, *, dispatched=True, status="running"):
        return {
            "version": 1,
            "status": status,
            "pause_reason": "",
            "branch": f"{server.DEV_BRANCH_PREFIX}test",
            "project_dir": str(self.project_dir),
            "session_goal": "goal",
            "turn_count": 0,
            "message_watermark": 0,
            "dispatched": dispatched,
            "updated_at": "now",
            "tasks": list(tasks or []),
            "handoff": "",
        }

    @staticmethod
    def fenced(tasks):
        return (
            f"{server.JSON_FENCE_OPEN}\n"
            + json.dumps({"tasks": tasks}, ensure_ascii=False)
            + f"\n{server.JSON_FENCE_CLOSE}"
        )

    def install_adapter(self, seat, outputs, calls=None):
        queued = list(outputs)
        calls = calls if calls is not None else []

        def fake(name, instruction, option, dev_role=None, timeout=None):
            calls.append({
                "name": name, "instruction": instruction,
                "dev_role": dev_role, "timeout": timeout,
            })
            value = queued.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

        server.ADAPTERS[seat] = fake
        return calls


class ProtocolParserTests(DevmodeTestCase):
    def test_tasks_schema_is_strict_and_last_fence_wins(self):
        valid = {
            "id": 1,
            "title": "build endpoint",
            "files": [],
            "acceptance": ["returns 200"],
        }
        text = self.fenced([]) + "\nignored\n" + self.fenced([valid])
        self.assertEqual(server._parse_tasks(text), [valid])
        self.assertEqual(server._parse_tasks(self.fenced([]), allow_empty=True), [])
        self.assertIsNone(server._parse_tasks(self.fenced([])))

        invalid = [
            {"id": 1, "title": "x", "acceptance": ["ok"]},
            {"id": True, "title": "x", "files": [], "acceptance": ["ok"]},
            {"id": 0, "title": "x", "files": [], "acceptance": ["ok"]},
            {"id": 1, "title": " ", "files": [], "acceptance": ["ok"]},
            {"id": 1, "title": "x", "files": "x.py", "acceptance": ["ok"]},
            {"id": 1, "title": "x", "files": [1], "acceptance": ["ok"]},
            {"id": 1, "title": "x", "files": [], "acceptance": []},
            {"id": 1, "title": "x", "files": [], "acceptance": [" "]},
            {"id": 1, "title": "x", "files": [], "acceptance": [1]},
        ]
        for item in invalid:
            with self.subTest(item=item):
                self.assertIsNone(server._parse_tasks(self.fenced([item]), allow_empty=True))
        self.assertIsNone(server._parse_tasks(self.fenced([valid, dict(valid)])))

    def test_verdict_and_arbitration_require_nonempty_detail(self):
        self.assertEqual(server._parse_verdict(server.VERDICT_PASS), {"passed": True, "reason": ""})
        self.assertEqual(
            server._parse_verdict(server.VERDICT_FAIL_PREFIX + "missing test"),
            {"passed": False, "reason": "missing test"},
        )
        self.assertIsNone(server._parse_verdict(server.VERDICT_FAIL_PREFIX + "   "))
        self.assertIsNone(server._parse_verdict(server.VERDICT_PASS + "\ntrailing"))

        for prefix, action in (
            (server.ARBITRATION_REASSIGN_PREFIX, "reassign"),
            (server.ARBITRATION_SKIP_PREFIX, "skip"),
            (server.ARBITRATION_ASK_PREFIX, "ask"),
        ):
            self.assertEqual(
                server._parse_arbitration(prefix + "detail"),
                {"action": action, "detail": "detail"},
            )
            self.assertIsNone(server._parse_arbitration(prefix + "   "))

    def test_digest_merge_preserves_omitted_done_and_empty_goes_to_handoff(self):
        done = self.task(1, status="done")
        done["commits"] = ["abc123"]
        pending = self.task(2)
        merged = server._merge_tasks([done, pending], [])
        self.assertEqual(merged, [done])
        data = self.state(merged, dispatched=True)
        self.assertEqual(server._dev_next_action(data), ("handoff", None))
        self.assertEqual(
            server._dev_next_action(self.state([], dispatched=False)),
            ("dispatch", None),
        )


class ProtocolExecutionTests(DevmodeTestCase):
    def test_protocol_retry_is_a_second_turn_and_forwards_controller_role(self):
        valid = {"id": 1, "title": "x", "files": [], "acceptance": ["ok"]}
        calls = self.install_adapter("claude", ["bad", self.fenced([valid])])
        data = self.state([], dispatched=False)
        server._save_tasks(data)

        text, parsed = server._protocol_call(
            "claude", "instruction", server._parse_tasks, "dispatch",
            task_id=0, dev_role="controller", data=data,
        )

        self.assertEqual(parsed, [valid])
        self.assertEqual(text, self.fenced([valid]))
        self.assertEqual(data["turn_count"], 2)
        self.assertEqual(server._dev["turn_count"], 2)
        self.assertEqual([call["dev_role"] for call in calls], ["controller", "controller"])
        self.assertNotIn(server.PROTOCOL_RETRY_NOTE, calls[0]["instruction"])
        self.assertIn(server.PROTOCOL_RETRY_NOTE, calls[1]["instruction"])
        self.assertEqual(len(list(server.DEVLOG_DIR.glob("*.log"))), 2)

    def test_turn_cap_blocks_protocol_retry_before_second_subprocess(self):
        calls = self.install_adapter("claude", ["bad", "should not run"])
        data = self.state([], dispatched=False)
        server._save_tasks(data)

        with mock.patch.object(server, "DEV_MAX_TURNS", 1):
            with self.assertRaises(server._PipelinePaused):
                server._protocol_call(
                    "claude", "instruction", server._parse_tasks, "dispatch",
                    task_id=0, dev_role="controller", data=data,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(server._dev["turn_count"], 1)
        saved = server._load_tasks()
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["pause_reason"], "turn_cap")

    def test_two_invalid_dispatch_outputs_pause_as_parse_fail(self):
        calls = self.install_adapter("claude", ["bad one", "bad two"])
        data = self.state([], dispatched=False)
        server._save_tasks(data)

        self.assertFalse(server._dev_run_dispatch(data))

        self.assertEqual(len(calls), 2)
        self.assertEqual(data["turn_count"], 2)
        self.assertEqual(data["status"], "paused")
        self.assertEqual(data["pause_reason"], "parse_fail")

    def test_digest_accepts_empty_and_preserves_done(self):
        done = self.task(1, status="done")
        pending = self.task(2)
        calls = self.install_adapter("claude", [self.fenced([])])
        data = self.state([done, pending], dispatched=True)
        server._save_tasks(data)

        self.assertTrue(server._dev_run_digest(data))

        self.assertEqual(data["tasks"], [done])
        self.assertEqual(server._dev_next_action(data), ("handoff", None))
        self.assertEqual(calls[0]["dev_role"], "controller")
        self.assertEqual(data["turn_count"], 1)

    def test_rate_limit_does_not_increment_task_attempts(self):
        task = self.task()
        data = self.state([task])
        server._save_tasks(data)

        def limited(name, instruction, option, dev_role=None, timeout=None):
            server._last_run[name] = {
                "args": ["fake"], "stdout": "", "stderr": "HTTP 429 rate limit",
                "returncode": 1, "elapsed": 0.01,
            }
            raise RuntimeError("adapter failed")

        server.ADAPTERS["agy"] = limited
        with self.assertRaises(server._RateLimited):
            server._dev_run_implement(data, task)

        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["status"], "pending")
        self.assertEqual(data["turn_count"], 1)
        self.assertEqual(server._load_tasks()["tasks"][0]["attempts"], 0)

    def test_arbitration_ask_does_not_mark_task_arbitrated(self):
        task = self.task(attempts=server.DEV_MAX_ATTEMPTS)
        data = self.state([task])
        server._save_tasks(data)
        self.install_adapter("claude", [server.ARBITRATION_ASK_PREFIX + "choose scope"])

        self.assertFalse(server._dev_run_arbitration(data, task))

        self.assertFalse(task["arbitrated"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(data["pause_reason"], "ask_host")
        self.assertEqual(data["message_watermark"], 0)




class GitIntegrationTests(DevmodeTestCase):
    def git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def init_git(self):
        self.git("init", "-b", "main")
        self.git("config", "user.email", "devmode@example.test")
        self.git("config", "user.name", "Devmode Test")
        (self.project_dir / "spec.md").write_text("spec", encoding="utf-8")
        self.git("add", "spec.md")
        self.git("commit", "-m", "initial")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_task_commit_stays_on_dev_branch_and_has_expected_message(self):
        main_head = self.init_git()
        branch = server._git_new_branch()
        self.assertTrue(branch.startswith(server.DEV_BRANCH_PREFIX))
        self.assertEqual(self.git("branch", "--show-current").stdout.strip(), branch)

        task = self.task()
        (self.project_dir / "file1.py").write_text("value = 1\n", encoding="utf-8")
        commit = server._git_commit_task(task, 1, "agy")

        self.assertTrue(commit)
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s").stdout.strip(),
            "[roundtable] 任務1 agy 第1次: task 1",
        )
        self.assertEqual(self.git("rev-parse", "main").stdout.strip(), main_head)
        self.assertIsNone(server._git_commit_task(task, 2, "agy"))

    def test_resume_resets_turn_count_and_persists_leftover_commit(self):
        self.init_git()
        branch = server._git_new_branch()
        task = self.task(status="in_progress", attempts=1)
        data = self.state([task], status="paused")
        data["branch"] = branch
        data["pause_reason"] = "turn_cap"
        data["turn_count"] = 40
        server._save_tasks(data)
        (self.project_dir / "leftover.py").write_text("leftover = True\n", encoding="utf-8")

        started = server.threading.Event()
        thread_args = {}

        def fake_pipeline(**kwargs):
            thread_args.update(kwargs)
            started.set()

        with mock.patch.object(server, "run_dev_pipeline", side_effect=fake_pipeline):
            ok, reason = server._dev_start()

        self.assertTrue(ok, reason)
        saved = server._load_tasks()
        self.assertEqual(saved["turn_count"], 0)
        self.assertEqual(saved["status"], "running")
        self.assertEqual(len(saved["tasks"][0]["commits"]), 1)
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s").stdout.strip(),
            "[roundtable] 任務1 中斷殘留變更",
        )
        self.assertEqual(server._dev["turn_count"], 0)
        self.assertTrue(started.wait(2))
        self.assertEqual(thread_args, {"digest_first": False})

    def test_dirty_tree_on_nonpipeline_branch_is_rejected(self):
        self.init_git()
        data = self.state([self.task(status="in_progress")], status="paused")
        data["branch"] = f"{server.DEV_BRANCH_PREFIX}elsewhere"
        server._save_tasks(data)
        (self.project_dir / "dirty.py").write_text("dirty = True\n", encoding="utf-8")

        with mock.patch.object(server, "run_dev_pipeline") as pipeline:
            ok, reason = server._dev_start()

        self.assertFalse(ok)
        self.assertIn("不在管線分支", reason)
        pipeline.assert_not_called()
        self.assertEqual(self.git("branch", "--show-current").stdout.strip(), "main")

    def test_ask_host_resume_requests_digest_before_continuing(self):
        self.init_git()
        branch = server._git_new_branch()
        task = self.task(attempts=server.DEV_MAX_ATTEMPTS)
        data = self.state([task], status="paused")
        data["branch"] = branch
        data["pause_reason"] = "ask_host"
        data["message_watermark"] = 0
        server._save_tasks(data)
        server.append_message("你", "please reassign", role="host", name="HOST")

        started = server.threading.Event()
        thread_args = {}

        def fake_pipeline(**kwargs):
            thread_args.update(kwargs)
            started.set()

        with mock.patch.object(server, "run_dev_pipeline", side_effect=fake_pipeline):
            ok, reason = server._dev_start()

        self.assertTrue(ok, reason)
        self.assertTrue(started.wait(2))
        self.assertEqual(thread_args, {"digest_first": True})
        self.assertFalse(server._load_tasks()["tasks"][0]["arbitrated"])

    def test_crash_downgrade_is_persisted(self):
        data = self.state([self.task()], status="running")
        server._save_tasks(data)

        server._dev_downgrade_crash()

        saved = server._load_tasks()
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["pause_reason"], "crash")



    def test_interrupted_dirty_implementation_keeps_task_for_leftover_commit(self):
        self.init_git()
        branch = server._git_new_branch()
        task = self.task()
        data = self.state([task])
        data["branch"] = branch
        server._save_tasks(data)

        def interrupted(name, instruction, option, dev_role=None, timeout=None):
            (self.project_dir / "file1.py").write_text("partial = True\n", encoding="utf-8")
            server._last_run[name] = {
                "args": ["fake"], "stdout": "", "stderr": "429 rate limit",
                "returncode": 1, "elapsed": 0.01,
            }
            raise RuntimeError("interrupted")

        server.ADAPTERS["agy"] = interrupted
        with self.assertRaises(server._RateLimited):
            server._dev_run_implement(data, task)

        self.assertEqual(task["attempts"], 0)
        self.assertEqual(task["status"], "in_progress")
        commit = server._git_commit_leftover(data)
        self.assertTrue(commit)
        self.assertEqual(task["commits"], [commit])
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s").stdout.strip(),
            "[roundtable] 任務1 中斷殘留變更",
        )

    def test_resume_rejects_tasks_from_another_project(self):
        self.init_git()
        branch = server._git_new_branch()
        data = self.state([self.task()], status="paused")
        data["branch"] = branch
        data["project_dir"] = str(self.root / "other-project")
        server._save_tasks(data)

        with mock.patch.object(server, "run_dev_pipeline") as pipeline:
            ok, reason = server._dev_start()

        self.assertFalse(ok)
        self.assertIn("專案目錄與目前專案不一致", reason)
        pipeline.assert_not_called()


class SafetyAndAdapterTests(DevmodeTestCase):
    def test_tasks_write_is_atomic_and_leaves_no_temp_file(self):
        data = self.state([self.task()])
        with mock.patch.object(server.os, "replace", wraps=server.os.replace) as replaced:
            server._save_tasks(data)

        replaced.assert_called_once()
        self.assertEqual(json.loads(server.TASKS_FILE.read_text(encoding="utf-8")), data)
        self.assertEqual(list(self.data_dir.glob(".tasks.json.*.tmp")), [])

    def test_manual_mirror_tamper_is_detected(self):
        data = self.state([self.task()])
        server._save_tasks(data)
        server.append_message("system", "baseline")
        server.MD_MIRROR.write_text("tampered", encoding="utf-8")

        self.assertEqual(server._dev_pre_gate(), "tamper")


    def test_tampered_tasks_are_not_promoted_to_trusted_paused_state(self):
        original = self.state([self.task(1)], status="paused")
        server._save_tasks(original)
        injected = self.state([self.task(99)], status="paused")
        server.TASKS_FILE.write_text(
            json.dumps(injected, ensure_ascii=False),
            encoding="utf-8",
        )

        ok, reason = server._dev_start()

        self.assertFalse(ok)
        self.assertIn("遭竄改", reason)
        saved = server._load_tasks()
        self.assertEqual([task["id"] for task in saved["tasks"]], [1])
        self.assertEqual(saved["pause_reason"], "tamper")


    def test_tamper_during_adapter_call_pauses_before_server_overwrites_state(self):
        valid = {"id": 1, "title": "x", "files": [], "acceptance": ["ok"]}
        data = self.state([], dispatched=False)
        server._save_tasks(data)
        server.append_message("system", "baseline")

        def tampering(name, instruction, option, dev_role=None, timeout=None):
            server.MD_MIRROR.write_text("tampered", encoding="utf-8")
            return self.fenced([valid])

        server.ADAPTERS["claude"] = tampering
        with self.assertRaises(server._PipelinePaused):
            server._dev_run_dispatch(data)

        self.assertEqual(data["pause_reason"], "tamper")
        self.assertEqual(server._dev["pause_reason"], "tamper")
        self.assertEqual(data["turn_count"], 1)

    def test_verifier_role_and_scope_instruction_are_forwarded(self):
        task = self.task(status="in_progress", attempts=1)
        data = self.state([task])
        server._save_tasks(data)
        calls = self.install_adapter("codex", [server.VERDICT_PASS])

        self.assertTrue(server._dev_run_verify(data, task))

        self.assertEqual(calls[0]["dev_role"], "verifier")
        self.assertIn("越界", calls[0]["instruction"])
        self.assertIn("commit", calls[0]["instruction"])
        self.assertEqual(task["status"], "done")
        self.assertEqual(data["turn_count"], 1)

    def test_default_adapter_arguments_keep_discussion_readonly_and_pipeline_roles(self):
        agy_calls = []

        def fake_agy_run(name, args, **kwargs):
            agy_calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "done", "")

        with mock.patch.object(server, "_run_process", side_effect=fake_agy_run):
            server._call_agy("agy", "instruction", server._option("agy"))
            server._call_agy(
                "agy", "instruction", server._option("agy"),
                dev_role="implementer", timeout=server.DEV_CALL_TIMEOUT,
            )

        discussion_args = agy_calls[0][0]
        implement_args = agy_calls[1][0]
        self.assertIn("--sandbox", discussion_args)
        self.assertNotIn("--dangerously-skip-permissions", discussion_args)
        self.assertIn("--dangerously-skip-permissions", implement_args)
        self.assertNotIn("--sandbox", implement_args)
        self.assertEqual(agy_calls[1][1]["timeout"], server.DEV_CALL_TIMEOUT)

        claude_calls = []

        def fake_claude_run(name, args, **kwargs):
            claude_calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "done", "")

        with mock.patch.object(server, "_find_claude", return_value="claude.exe"):
            with mock.patch.object(server, "_run_process", side_effect=fake_claude_run):
                server._call_claude(
                    "claude", "instruction", server._option("claude"),
                    dev_role="controller", timeout=server.DEV_CALL_TIMEOUT,
                )

        claude_args, claude_kwargs = claude_calls[0]
        allowed_index = claude_args.index("--allowedTools")
        self.assertEqual(claude_args[allowed_index + 1], "Read,Glob,Grep")
        self.assertNotIn("WebFetch", claude_args)
        self.assertNotIn("WebSearch", claude_args)
        self.assertEqual(claude_kwargs["cwd"], str(self.project_dir))
        self.assertEqual(claude_kwargs["timeout"], server.DEV_CALL_TIMEOUT)

        codex_calls = []

        def fake_codex_run(name, args, **kwargs):
            codex_calls.append((args, kwargs))
            output_path = Path(args[args.index("-o") + 1])
            output_path.write_text("done", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(server, "_run_process", side_effect=fake_codex_run):
            server._call_codex(
                "codex", "instruction", server._option("codex"),
                dev_role="verifier", timeout=server.DEV_CALL_TIMEOUT,
            )

        codex_args, codex_kwargs = codex_calls[0]
        sandbox_index = codex_args.index("--sandbox")
        self.assertEqual(codex_args[sandbox_index + 1], "read-only")
        self.assertEqual(codex_kwargs["timeout"], server.DEV_CALL_TIMEOUT)

    def test_devlog_contains_full_process_record(self):
        data = self.state([self.task()])
        server._save_tasks(data)

        def fake(name, instruction, option, dev_role=None, timeout=None):
            server._last_run[name] = {
                "args": ["fake", "--sandbox", "read-only"],
                "stdout": "full stdout",
                "stderr": "full stderr",
                "returncode": 0,
                "elapsed": 1.25,
            }
            return "reply"

        server.ADAPTERS["codex"] = fake
        result = server._call_seat_checked(
            "codex", "full instruction", "verify", task_id=1,
            dev_role="verifier", data=data,
        )

        self.assertEqual(result, "reply")
        logs = list(server.DEVLOG_DIR.glob("*.log"))
        self.assertEqual(len(logs), 1)
        content = logs[0].read_text(encoding="utf-8")
        self.assertIn("full instruction", content)
        self.assertIn("full stdout", content)
        self.assertIn("full stderr", content)
        self.assertIn("returncode: 0", content)

class InterjectRoutingTests(DevmodeTestCase):
    """規格 §5：管線 active／未收尾 paused 期間，人類發言只入逐字稿，不得啟動一般圓桌回覆。

    走真 Handler（loopback 自動 HOST），把 start_batch／start_discussion 換成會失敗的 stub。
    """

    def setUp(self):
        super().setUp()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        server.threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def failing_stubs(self):
        return (
            mock.patch.object(server, "start_batch", side_effect=AssertionError("start_batch called")),
            mock.patch.object(server, "start_discussion", side_effect=AssertionError("start_discussion called")),
        )

    def test_send_during_active_pipeline_only_appends(self):
        server._dev["active"] = True
        batch_p, disc_p = self.failing_stubs()
        with batch_p as batch, disc_p as disc:
            status, j = self.post("/api/send", {"text": "任務 2 改用 pathlib", "ask": ["codex"]})

        self.assertEqual(status, 200)
        self.assertTrue(j.get("dev_pending"))
        batch.assert_not_called()
        disc.assert_not_called()
        self.assertEqual(server._messages[-1]["text"], "任務 2 改用 pathlib")
        self.assertEqual(server._messages[-1]["role"], "host")

    def test_discussion_mode_send_during_paused_pipeline_only_appends(self):
        server._dev["paused"] = True
        batch_p, disc_p = self.failing_stubs()
        with batch_p as batch, disc_p as disc:
            status, j = self.post(
                "/api/send",
                {"text": "拍板：跳過任務 3", "mode": "discussion", "ask": ["codex"], "max_rounds": 3},
            )

        self.assertEqual(status, 200)
        self.assertTrue(j.get("dev_pending"))
        batch.assert_not_called()
        disc.assert_not_called()
        self.assertEqual(server._messages[-1]["text"], "拍板：跳過任務 3")

    def test_ask_rejected_while_pipeline_unfinished(self):
        server._dev["paused"] = True
        batch_p, _ = self.failing_stubs()
        with batch_p as batch:
            status, j = self.post("/api/ask", {"names": ["codex"]})

        self.assertEqual(status, 409)
        self.assertIn("管線", j.get("error", ""))
        batch.assert_not_called()

    def test_send_without_pipeline_still_starts_batch(self):
        with mock.patch.object(server, "start_batch", return_value=[]) as batch:
            status, j = self.post("/api/send", {"text": "hello", "ask": []})

        self.assertEqual(status, 200)
        self.assertNotIn("dev_pending", j)
        batch.assert_called_once()


class DevPayloadTests(DevmodeTestCase):
    def test_roles_custom_flag_reflects_non_default_mapping(self):
        with mock.patch.object(server, "DEVMODE", True):
            self.assertFalse(server._dev_payload()["roles_custom"])

            server._dev_roles.update(
                {"controller": "codex", "implementer": "agy", "verifier": "claude"})
            payload = server._dev_payload()

        self.assertTrue(payload["roles_custom"])
        self.assertEqual(payload["roles"]["controller"], "codex")

    def test_roles_custom_ignores_key_order(self):
        server._dev_roles.clear()
        server._dev_roles.update(
            {"verifier": "codex", "implementer": "agy", "controller": "claude"})
        self.assertFalse(server._dev_roles_custom())


if __name__ == "__main__":
    unittest.main()
