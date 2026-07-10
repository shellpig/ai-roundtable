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
            "MEETING_SUMMARY_FILE": self.data_dir / "meeting_summary.json",
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
            "_trusted_meeting_summary": None,
            "_devlog_seq": 0,
            "ADAPTERS": dict(server.ADAPTERS),
        }
        self.patchers = [mock.patch.object(server, name, value) for name, value in replacements.items()]
        for patcher in self.patchers:
            patcher.start()
        for path in (server.TRANSCRIPT, server.MD_MIRROR, server.TASKS_FILE, server.MEETING_SUMMARY_FILE):
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
            "base_commit": "",
            "last_failure_fingerprint": "",
            "consecutive_same_failures": 0,
        }

    def state(self, tasks=None, *, dispatched=True, status="running"):
        return {
            "version": 2,
            "status": status,
            "pause_reason": "",
            "branch": f"{server.DEV_BRANCH_PREFIX}test",
            "project_dir": str(self.project_dir),
            "session_goal": "goal",
            "turn_count": 0,
            "message_watermark": 0,
            "dispatched": dispatched,
            "main_commit": "main-base",
            "integration_verified": False,
            "sessions": {},
            "usage": server._empty_usage_state(),
            "updated_at": "now",
            "tasks": list(tasks or []),
            "handoff": "",
        }

    @staticmethod
    def fenced(tasks):
        return (
            f"{server.JSON_FENCE_OPEN}\n"
            + json.dumps({
                "meeting_summary": {
                    "source_message_watermark": 0,
                    "goal": "test goal",
                    "decisions": [],
                    "non_goals": [],
                    "global_constraints": [],
                    "acceptance_criteria": [],
                    "open_questions": [],
                },
                "tasks": tasks,
            }, ensure_ascii=False)
            + f"\n{server.JSON_FENCE_CLOSE}"
        )

    def install_adapter(self, seat, outputs, calls=None):
        queued = list(outputs)
        calls = calls if calls is not None else []

        def fake(name, instruction, option, dev_role=None, timeout=None, session_id=None):
            calls.append({
                "name": name, "instruction": instruction,
                "dev_role": dev_role, "timeout": timeout,
                "session_id": session_id,
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

    def test_integration_gate_schema_is_typed_and_rejects_unsafe_values(self):
        summary = {
            "source_message_watermark": 0, "goal": "goal", "decisions": [], "non_goals": [],
            "global_constraints": [], "acceptance_criteria": [], "open_questions": [],
        }
        summary["integration_gates"] = [
            {"kind": server.INTEGRATION_GATE_UNITTEST},
            {"kind": server.INTEGRATION_GATE_MODULE, "module": "transcript_tool.cli", "args": ["roundtable.md"]},
        ]
        parsed = server._validate_meeting_summary(summary)
        self.assertEqual(parsed["integration_gates"], summary["integration_gates"])
        for invalid in (
            [{"kind": "shell", "args": ["dir"]}],
            [{"kind": server.INTEGRATION_GATE_MODULE, "module": "x;bad", "args": []}],
            [{"kind": server.INTEGRATION_GATE_MODULE, "module": "tool.cli", "args": ["..\\secret"]}],
        ):
            with self.subTest(invalid=invalid):
                candidate = dict(summary, integration_gates=invalid)
                self.assertIsNone(server._validate_meeting_summary(candidate))

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
        self.assertEqual(server._dev_next_action(data), ("integration_verify", None))
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
        self.assertEqual(server._dev_next_action(data), ("integration_verify", None))
        self.assertEqual(calls[0]["dev_role"], "controller")
        self.assertEqual(data["turn_count"], 1)

    def test_rate_limit_does_not_increment_task_attempts(self):
        task = self.task()
        data = self.state([task])
        server._save_tasks(data)

        def limited(name, instruction, option, dev_role=None, timeout=None, session_id=None):
            server._last_run[name] = {
                "args": ["fake"], "stdout": "", "stderr": "HTTP 429 rate limit",
                "returncode": 1, "elapsed": 0.01,
            }
            raise RuntimeError("adapter failed")

        server.ADAPTERS["agy"] = limited
        with mock.patch.object(server, "_git_head", return_value="base"):
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

    def test_arbitration_uses_compact_task_brief(self):
        task = self.task(attempts=server.DEV_MAX_ATTEMPTS)
        other = self.task(2, status="done")
        data = self.state([task, other])
        server._save_tasks(data)
        server.append_message("agy", "very old implementation output" * 200)
        calls = self.install_adapter("claude", [server.ARBITRATION_ASK_PREFIX + "choose scope"])

        self.assertFalse(server._dev_run_arbitration(data, task))

        instruction = calls[0]["instruction"]
        self.assertIn('"other_tasks"', instruction)
        self.assertIn('"id": 2', instruction)
        self.assertNotIn("very old implementation output", instruction)
        self.assertNotIn('"acceptance_criteria"', instruction)




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

    def test_per_task_final_net_diff_excludes_prior_task_and_deleted_artifact(self):
        self.init_git()
        server._git_new_branch()
        task1 = self.task(1)
        (self.project_dir / "file1.py").write_text("one\n", encoding="utf-8")
        server._git_commit_task(task1, 1, "agy")
        task2_base = self.git("rev-parse", "HEAD").stdout.strip()

        artifact = self.project_dir / "artifact.pyc"
        artifact.write_bytes(b"temporary")
        self.git("add", "artifact.pyc")
        self.git("commit", "-m", "temporary artifact")
        artifact.unlink()
        (self.project_dir / "file2.py").write_text("two\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-m", "final task two")

        diff_range, block = server._git_diff_summary(task2_base)
        self.assertEqual(diff_range, f"{task2_base}...HEAD")
        self.assertIn("file2.py", block)
        self.assertNotIn("file1.py", block)
        self.assertNotIn("artifact.pyc", block)

    def test_integration_verify_uses_pipeline_main_final_diff(self):
        main = self.init_git()
        branch = server._git_new_branch()
        (self.project_dir / "file1.py").write_text("done\n", encoding="utf-8")
        self.git("add", "file1.py")
        self.git("commit", "-m", "task")
        task = self.task(1, status="done")
        task["commits"] = ["secret-history-sha"]
        data = self.state([task])
        data["main_commit"] = main
        data["branch"] = branch
        server._save_tasks(data)
        calls = self.install_adapter("codex", [server.VERDICT_PASS])

        self.assertTrue(server._dev_run_integration_verify(data))

        self.assertTrue(data["integration_verified"])
        self.assertIn(f"{main}...HEAD", calls[0]["instruction"])
        self.assertIn("file1.py", calls[0]["instruction"])
        self.assertNotIn("git show --stat", calls[0]["instruction"])
        self.assertNotIn("secret-history-sha", calls[0]["instruction"])

    def test_integration_gate_evidence_is_recorded_and_forwarded(self):
        main = self.init_git()
        branch = server._git_new_branch()
        (self.project_dir / "file1.py").write_text("done\n", encoding="utf-8")
        self.git("add", "file1.py")
        self.git("commit", "-m", "task")
        python = self.project_dir / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        summary = {
            "version": 1, "source_message_watermark": 0, "goal": "goal", "decisions": [],
            "non_goals": [], "global_constraints": [], "acceptance_criteria": [], "open_questions": [],
            "integration_gates": [{"kind": server.INTEGRATION_GATE_UNITTEST}], "updated_at": "now",
        }
        server._save_meeting_summary(summary)
        data = self.state([self.task(1, status="done")])
        data["main_commit"] = main
        data["branch"] = branch
        server._save_tasks(data)
        calls = self.install_adapter("codex", [server.VERDICT_PASS, server.VERDICT_PASS])
        with mock.patch.object(server, "_run_process", return_value=subprocess.CompletedProcess([], 0, "gate passed", "")) as run:
            self.assertTrue(server._dev_run_integration_verify(data))
        self.assertEqual(data["integration_gate_results"][0]["status"], "passed")
        self.assertIn("gate passed", calls[0]["instruction"])
        self.assertEqual(run.call_args.args[1][1:5], ["-m", "unittest", "discover", "-s"])
        with mock.patch.object(server, "_run_process", return_value=subprocess.CompletedProcess([], 1, "", "gate failed")):
            self.assertFalse(server._dev_run_integration_verify(data))
        self.assertEqual(data["pause_reason"], "integration_gate_failed")

    def test_integration_gate_reports_failure_and_missing_venv(self):
        gate = {"kind": server.INTEGRATION_GATE_UNITTEST}
        self.assertEqual(server._run_integration_gates([gate])[0]["status"], "blocked")
        python = self.project_dir / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        with mock.patch.object(server, "_run_process", return_value=subprocess.CompletedProcess([], 1, "", "test failed")):
            result = server._run_integration_gates([gate])[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["stderr_tail"], "test failed")

    def test_v1_paused_resume_backfills_git_baselines_and_completes_verification(self):
        main = self.init_git()
        branch = server._git_new_branch()
        (self.project_dir / "file1.py").write_text("legacy task\n", encoding="utf-8")
        self.git("add", "file1.py")
        self.git("commit", "-m", "legacy task commit")
        task_commit = self.git("rev-parse", "--short", "HEAD").stdout.strip()

        task = self.task(1, status="in_progress", attempts=1)
        task["commits"] = [task_commit]
        task.pop("base_commit")
        task.pop("last_failure_fingerprint")
        task.pop("consecutive_same_failures")
        legacy = self.state([task], status="paused")
        legacy["version"] = 1
        legacy["branch"] = branch
        for key in ("main_commit", "integration_verified", "sessions", "usage"):
            legacy.pop(key)
        server.TASKS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        server._record_hash(server.TASKS_FILE)

        started = server.threading.Event()
        with mock.patch.object(server, "run_dev_pipeline", side_effect=lambda **kwargs: started.set()):
            ok, reason = server._dev_start()
        self.assertTrue(ok, reason)
        self.assertTrue(started.wait(2))

        resumed = server._load_tasks()
        self.assertEqual(resumed["version"], 2)
        self.assertEqual(resumed["main_commit"], main)
        self.assertEqual(resumed["tasks"][0]["base_commit"], main)
        self.assertNotIn("git_baselines_pending", resumed)

        self.install_adapter("codex", [server.VERDICT_PASS, server.VERDICT_PASS])
        resumed_task = resumed["tasks"][0]
        self.assertTrue(server._dev_run_verify(resumed, resumed_task))
        self.assertEqual(server._dev_next_action(resumed), ("integration_verify", None))
        self.assertTrue(server._dev_run_integration_verify(resumed))
        self.assertEqual(server._dev_next_action(resumed), ("handoff", None))

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

        def interrupted(name, instruction, option, dev_role=None, timeout=None, session_id=None):
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

        def tampering(name, instruction, option, dev_role=None, timeout=None, session_id=None):
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

        with mock.patch.object(server, "_git_diff_summary", return_value=("base...HEAD", "final diff")):
            task["base_commit"] = "base"
            self.assertTrue(server._dev_run_verify(data, task))

        self.assertEqual(calls[0]["dev_role"], "verifier")
        self.assertIn("越界", calls[0]["instruction"])
        self.assertIn("base...HEAD", calls[0]["instruction"])
        self.assertNotIn("git show --stat", calls[0]["instruction"])
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
        self.assertNotIn("instruction", discussion_args)
        self.assertEqual(agy_calls[0][1]["input_text"], "instruction")
        self.assertEqual(agy_calls[1][1]["timeout"], server.DEV_CALL_TIMEOUT)

        claude_calls = []

        def fake_claude_run(name, args, **kwargs):
            claude_calls.append((args, kwargs))
            payload = {"result": "done", "session_id": "claude-session", "usage": {
                "input_tokens": 10, "output_tokens": 2,
            }}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        with mock.patch.object(server, "_find_claude", return_value="claude.exe"):
            with mock.patch.object(server, "_run_process", side_effect=fake_claude_run):
                server._call_claude(
                    "claude", "instruction", server._option("claude"),
                    dev_role="controller", timeout=server.DEV_CALL_TIMEOUT,
                )
                server._call_claude(
                    "claude", "instruction", server._option("claude"),
                    dev_role="verifier", timeout=server.DEV_CALL_TIMEOUT,
                )

        claude_args, claude_kwargs = claude_calls[0]
        allowed_index = claude_args.index("--allowedTools")
        self.assertEqual(claude_args[allowed_index + 1], "Read,Glob,Grep")
        self.assertNotIn("WebFetch", claude_args)
        self.assertNotIn("WebSearch", claude_args)
        self.assertEqual(claude_kwargs["cwd"], str(self.project_dir))
        self.assertEqual(claude_kwargs["timeout"], server.DEV_CALL_TIMEOUT)
        self.assertNotIn("instruction", claude_args)
        self.assertEqual(claude_kwargs["input_text"], "instruction")
        self.assertIn("--output-format", claude_args)
        self.assertIn("--no-session-persistence", claude_calls[1][0])

        codex_calls = []

        def fake_codex_run(name, args, **kwargs):
            codex_calls.append((args, kwargs))
            events = [
                {"type": "thread.started", "thread_id": "codex-session"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2,
                }},
            ]
            return subprocess.CompletedProcess(args, 0, "\n".join(json.dumps(e) for e in events), "")

        with mock.patch.object(server, "_run_process", side_effect=fake_codex_run):
            server._call_codex(
                "codex", "instruction", server._option("codex"),
                dev_role="verifier", timeout=server.DEV_CALL_TIMEOUT,
            )

        codex_args, codex_kwargs = codex_calls[0]
        sandbox_index = codex_args.index("--sandbox")
        self.assertEqual(codex_args[sandbox_index + 1], "read-only")
        self.assertIn("--ephemeral", codex_args)
        self.assertEqual(codex_kwargs["timeout"], server.DEV_CALL_TIMEOUT)

    def test_devlog_contains_full_process_record(self):
        data = self.state([self.task()])
        server._save_tasks(data)

        def fake(name, instruction, option, dev_role=None, timeout=None, session_id=None):
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


class D4FeatureTests(DevmodeTestCase):
    def usage_result(self, text="reply", session_id=None, total=12):
        return server._adapter_result(
            text,
            {
                "source": "cli_json", "input_tokens": 7, "cached_input_tokens": 3,
                "output_tokens": 2, "total_tokens": total, "cost_usd": None,
            },
            session_id,
        )

    def test_v1_tasks_migrate_without_losing_progress(self):
        legacy = self.state([self.task(status="in_progress", attempts=2)])
        legacy["version"] = 1
        legacy.pop("sessions")
        legacy.pop("usage")
        legacy["tasks"][0].pop("base_commit")
        server.TASKS_FILE.write_text(json.dumps(legacy), encoding="utf-8")
        server._record_hash(server.TASKS_FILE)

        loaded = server._load_tasks()

        self.assertEqual(loaded["version"], 2)
        self.assertEqual(loaded["branch"], legacy["branch"])
        self.assertEqual(loaded["tasks"][0]["attempts"], 2)
        self.assertEqual(loaded["tasks"][0]["status"], "in_progress")
        self.assertIn("sessions", loaded)
        self.assertIn("usage", loaded)
        self.assertEqual(json.loads(server.TASKS_FILE.read_text(encoding="utf-8"))["version"], 2)
        self.assertTrue(server._check_tamper())

    def test_structured_codex_and_claude_usage_parsing(self):
        codex_events = [
            {"type": "thread.started", "thread_id": "codex-id"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 5,
            }},
        ]
        codex = server._codex_json_result("\n".join(json.dumps(e) for e in codex_events))
        self.assertEqual(codex["text"], "answer")
        self.assertEqual(codex["session"]["id"], "codex-id")
        self.assertEqual(codex["usage"]["input_tokens"], 12)
        self.assertEqual(codex["usage"]["total_tokens"], 25)

        claude = server._claude_json_result(json.dumps({
            "result": "answer", "session_id": "claude-id", "total_cost_usd": 0.02,
            "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4, "output_tokens": 2,
            },
        }))
        self.assertEqual(claude["session"]["id"], "claude-id")
        self.assertEqual(claude["usage"]["cached_input_tokens"], 7)
        self.assertEqual(claude["usage"]["total_tokens"], 19)
        self.assertEqual(claude["usage"]["cost_usd"], 0.02)

    def test_summary_is_strict_atomic_and_tamper_protected(self):
        valid_task = {"id": 1, "title": "x", "files": [], "acceptance": ["ok"]}
        parsed = server._parse_tasks(self.fenced([valid_task]))
        self.assertIsNotNone(parsed)
        summary = {"version": 1, **parsed.summary, "updated_at": "now"}
        server._save_meeting_summary(summary)
        self.assertTrue(server._check_tamper())

        bad = json.loads(json.dumps({"meeting_summary": parsed.summary, "tasks": [valid_task]}))
        bad["meeting_summary"]["source_message_watermark"] = 1
        wrapped = f"{server.JSON_FENCE_OPEN}\n{json.dumps(bad)}\n{server.JSON_FENCE_CLOSE}"
        self.assertIsNone(server._parse_tasks(wrapped))
        self.assertEqual(server._load_meeting_summary()["source_message_watermark"], 0)

        server.MEETING_SUMMARY_FILE.write_text("{}", encoding="utf-8")
        self.assertEqual(server._dev_pre_gate(), "tamper")

    def test_summary_context_is_selective_and_archived_with_meeting(self):
        summary = {
            "version": 1, "source_message_watermark": 0, "goal": "goal",
            "decisions": ["decision"], "non_goals": [],
            "global_constraints": ["no deps"], "acceptance_criteria": ["tests pass"],
            "open_questions": [], "updated_at": "now",
        }
        server._save_meeting_summary(summary)
        task = self.task()
        implement = server._dev_instruction("implement", task=task)
        verify = server._dev_instruction("verify", task=task, diff_range="base...HEAD", diff_block="diff")
        integration = server._dev_instruction(
            "integration_verify", diff_range="main...HEAD", diff_block="diff", tasks_json="[]")
        digest = server._dev_instruction("digest", tasks_json="{}")
        self.assertIn("no deps", implement)
        self.assertNotIn("tests pass", implement)
        self.assertNotIn("tests pass", verify)
        self.assertIn("tests pass", integration)
        self.assertNotIn(str(server.MEETING_SUMMARY_FILE), implement)
        self.assertNotIn(str(server.MD_MIRROR), implement)
        self.assertIn("decision", digest)
        self.assertNotIn(str(server.MD_MIRROR), digest)

        server.append_message("你", "goal", role="host", name="HOST")
        server._save_tasks(self.state([task], status="done"))
        entry = server._archive_active_session()
        self.assertTrue(entry["meeting_summary_path"])
        self.assertTrue((self.data_dir / entry["meeting_summary_path"]).exists())

    def test_same_failure_second_time_pauses_even_after_new_attempt(self):
        task = self.task(status="in_progress", attempts=1)
        task["base_commit"] = "base"
        data = self.state([task])
        server._save_tasks(data)
        self.install_adapter("codex", [
            server.VERDICT_FAIL_PREFIX + " Missing   Test ",
            server.VERDICT_FAIL_PREFIX + "missing test",
        ])

        with mock.patch.object(server, "_git_diff_summary", return_value=("base...HEAD", "diff")):
            self.assertTrue(server._dev_run_verify(data, task))
            task["status"] = "in_progress"
            task["attempts"] += 1
            task["commits"].append("newcommit")
            self.assertFalse(server._dev_run_verify(data, task))

        self.assertEqual(data["status"], "paused")
        self.assertEqual(data["pause_reason"], "repeated_failure")
        self.assertEqual(task["consecutive_same_failures"], 2)

    def test_session_policy_usage_and_resume_fallback(self):
        calls = []

        def fake(name, instruction, option, dev_role=None, timeout=None, session_id=None):
            calls.append((dev_role, session_id))
            return self.usage_result(session_id="controller-id")

        server.ADAPTERS["claude"] = fake
        data = self.state([self.task()])
        server._save_tasks(data)
        server._call_seat_checked("claude", "one", "dispatch", 0, "controller", data)
        server._call_seat_checked("claude", "two", "digest", 0, "controller", data)
        self.assertEqual(calls, [("controller", None), ("controller", "controller-id")])
        self.assertEqual(data["usage"]["known_total_tokens"], 24)
        self.assertEqual(data["usage"]["by_provider"]["claude"]["total_tokens"], 24)

        implement_calls = []
        server.ADAPTERS["agy"] = lambda name, instruction, option, dev_role=None, timeout=None, session_id=None: (
            implement_calls.append((task_id := (1 if instruction != "task2" else 2), session_id))
            or self.usage_result(session_id=f"implement-{task_id}"))
        server._call_seat_checked("agy", "task1", "implement", 1, "implementer", data)
        server._call_seat_checked("agy", "task1", "implement", 1, "implementer", data)
        server._call_seat_checked("agy", "task2", "implement", 2, "implementer", data)
        self.assertEqual(implement_calls, [(1, None), (1, "implement-1"), (2, None)])

        verifier_calls = []
        server.ADAPTERS["codex"] = lambda name, instruction, option, dev_role=None, timeout=None, session_id=None: (
            verifier_calls.append(session_id) or self.usage_result(session_id="ignored"))
        server._call_seat_checked("codex", "verify", "verify", 1, "verifier", data)
        server._call_seat_checked("codex", "verify", "verify", 1, "verifier", data)
        self.assertEqual(verifier_calls, [None, None])
        self.assertNotIn("verifier", data["sessions"])

        data["sessions"]["controller"] = {
            "provider": "claude", "session_id": "expired", "task_id": None,
            "branch": data["branch"], "project_dir": str(self.project_dir),
        }
        fallback_calls = []

        def fallback(name, instruction, option, dev_role=None, timeout=None, session_id=None):
            fallback_calls.append(session_id)
            if session_id:
                server._last_run[name] = {"stderr": "thread abc not found", "returncode": 1}
                raise RuntimeError("thread abc not found")
            return self.usage_result(session_id="fresh")

        server.ADAPTERS["claude"] = fallback
        server._call_seat_checked("claude", "resume", "digest", 0, "controller", data)
        self.assertEqual(fallback_calls, ["expired", None])
        metadata_line = [line for line in sorted(server.DEVLOG_DIR.glob("*.log"))[-1].read_text(
            encoding="utf-8").splitlines() if line.startswith("metadata_json=")][0]
        metadata = json.loads(metadata_line.split("=", 1)[1])
        self.assertTrue(metadata["session"]["resume_failed"])

    def test_codex_fresh_persistence_failure_falls_back_ephemeral_without_saving_id(self):
        calls = []
        events = [
            {"type": "thread.started", "thread_id": "unusable-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 8, "cached_input_tokens": 3, "output_tokens": 2,
            }},
        ]

        def fake_run(name, args, **kwargs):
            calls.append(list(args))
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    args, 1, "", "failed to record rollout items: thread unusable-thread not found")
            return subprocess.CompletedProcess(
                args, 0, "\n".join(json.dumps(event) for event in events), "")

        data = self.state([self.task()])
        server._save_tasks(data)
        with mock.patch.object(server, "_run_process", side_effect=fake_run):
            text = server._call_seat_checked(
                "codex", "controller prompt", "dispatch", 0, "controller", data)

        self.assertEqual(text, "answer")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--ephemeral", calls[0])
        self.assertIn("--ephemeral", calls[1])
        self.assertNotIn("controller", data["sessions"])
        metadata_line = [line for line in next(server.DEVLOG_DIR.glob("*.log")).read_text(
            encoding="utf-8").splitlines() if line.startswith("metadata_json=")][0]
        metadata = json.loads(metadata_line.split("=", 1)[1])
        self.assertIsNone(metadata["session"]["id"])
        self.assertFalse(metadata["session"]["resumed"])
        self.assertTrue(metadata["session"]["persistence_fallback"])

    def test_codex_usage_limit_with_secondary_thread_error_does_not_retry(self):
        calls = []
        stdout = json.dumps({
            "type": "turn.failed",
            "error": {"message": "You've hit your usage limit. Try again later."},
        })
        stderr = "failed to record rollout items: thread secondary not found"

        def fake_run(name, args, **kwargs):
            calls.append(list(args))
            server._last_run[name] = {
                "args": list(args), "stdout": stdout, "stderr": stderr,
                "returncode": 1, "elapsed": 0.01,
            }
            return subprocess.CompletedProcess(args, 1, stdout, stderr)

        task = self.task()
        data = self.state([task])
        server._save_tasks(data)
        server._dev_roles["implementer"] = "codex"
        with mock.patch.object(server, "_run_process", side_effect=fake_run):
            with mock.patch.object(server, "_git_head", return_value="base"):
                with self.assertRaises(server._RateLimited):
                    server._dev_run_implement(data, task)

        self.assertEqual(len(calls), 1)
        self.assertNotIn("--ephemeral", calls[0])
        self.assertEqual(task["attempts"], 0)

    def test_dev_payload_exposes_known_usage(self):
        data = self.state([self.task()])
        data["usage"] = {
            "last": {"provider": "codex", "total_tokens": 9},
            "by_provider": {"codex": {"total_tokens": 9}},
            "known_total_tokens": 9,
            "incomplete": True,
        }
        server._save_tasks(data)
        with mock.patch.object(server, "DEVMODE", True):
            payload = server._dev_payload()
        self.assertEqual(payload["usage"]["known_total_tokens"], 9)
        self.assertTrue(payload["usage"]["incomplete"])

    def test_interjection_during_integration_verify_is_digested_before_handoff(self):
        data = self.state([self.task(status="done")])
        data["integration_verified"] = False
        server._save_tasks(data)

        def integration_with_interjection(current):
            current["integration_verified"] = True
            server._save_tasks(current)
            server.append_message("你", "收尾前請補一項", role="host", name="HOST")
            return True

        with mock.patch.object(server, "_dev_run_integration_verify", side_effect=integration_with_interjection):
            with mock.patch.object(server, "_dev_digest_step", return_value=False) as digest:
                with mock.patch.object(server, "_dev_run_handoff") as handoff:
                    server.run_dev_pipeline()

        digest.assert_called_once()
        handoff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
