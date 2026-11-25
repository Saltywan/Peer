import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "peer" / "scripts" / "peer.py"
spec = importlib.util.spec_from_file_location("peer", SCRIPT)
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


FAKE_CLI = """#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
if sys.argv[1:] == ['models']:
    print('Fetching available models...')
    print('gemini-test-high\tGemini Test (High)')
    raise SystemExit(0)
payload = sys.stdin.read()
pathlib.Path(os.environ['FAKE_LOG']).write_text(json.dumps({'name': name, 'args': sys.argv[1:], 'stdin': payload}))
if os.environ.get('FAKE_NONFATAL_WARN') == '1':
    print('WARNING: proceeding, even though we could not create PATH aliases: Operation not permitted (os error 1)', file=sys.stderr)
args = sys.argv[1:]
forked = 'fork' in args or '--fork-session' in args
if forked:
    default_sid = name + '-fork-id'
elif '--resume' in args:
    default_sid = args[args.index('--resume') + 1]
elif '--conversation' in args:
    default_sid = args[args.index('--conversation') + 1]
elif 'resume' in args:
    default_sid = args[-2]
else:
    default_sid = name + '-id'
sid = os.environ.get('FAKE_SESSION', default_sid)
if name == 'codex':
    if os.environ.get('FAKE_NO_SESSION') != '1':
        print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
elif name == 'claude':
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
    print(json.dumps({'type': 'assistant', 'session_id': sid, 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}}), flush=True)
    print(json.dumps({'type': 'user', 'session_id': sid, 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}), flush=True)
else:
    print(json.dumps({'event': 'init', 'conversation_id': sid}), flush=True)
if os.environ.get('FAKE_SLEEP'):
    def on_term(*_):
        pathlib.Path(os.environ['FAKE_TERM_MARK']).write_text('term')
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, on_term)
    pathlib.Path(os.environ['FAKE_PID']).write_text(str(os.getpid()))
    time.sleep(float(os.environ['FAKE_SLEEP']))
if os.environ.get('FAKE_FAIL_PROVIDER') == name:
    print('model overloaded', file=sys.stderr)
    raise SystemExit(1)
reply = os.environ.get('FAKE_REPLY', name + ' reply')
if os.environ.get('FAKE_EDIT') == '1':
    pathlib.Path('edited.txt').write_text('changed')
if os.environ.get('FAKE_DENY') == '1':
    print('Tool run_command requires approval and was soft-denied', file=sys.stderr)
if os.environ.get('FAKE_HOST_DENY') == '1':
    print('open state_5.sqlite: operation not permitted', file=sys.stderr)
    raise SystemExit(1)
if name == 'codex':
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'Permission denied by sandbox', 'exit_code': 1, 'status': 'failed'}}))
    if os.environ.get('FAKE_CRASH') == '1':
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'chrome --headless', 'aggregated_output': '', 'exit_code': 134, 'status': 'failed'}}))
    if os.environ.get('FAKE_GREP_HIT') == '1':
        print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'peer.py:9: permission denied|operation not permitted', 'exit_code': 0, 'status': 'completed'}}))
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': reply}}))
elif name == 'claude':
    result = {'type': 'result', 'subtype': 'success', 'session_id': sid, 'result': reply, 'is_error': False}
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        result['permission_denials'] = [{'tool': 'Bash', 'reason': 'approval required'}]
    print(json.dumps(result))
else:
    if os.environ.get('FAKE_STRUCTURED_DENY') == '1':
        print(json.dumps({'event': 'step_update', 'step_update': {'step_type': 'tool_call', 'error': 'permission denied'}}))
    if os.environ.get('FAKE_TOOL_INFO_DENY') == '1':
        print(json.dumps({'event': 'step_update', 'step_update': {'step_type': 'tool_call', 'tool_info': {'error': 'permission denied'}}}))
    result = {'conversation_id': sid, 'status': 'SUCCESS', 'response': reply}
    if os.environ.get('FAKE_RESULT_DENY') == '1':
        result['denied_actions'] = [{'action': 'command', 'display_name': 'RunCommand'}]
    if os.environ.get('FAKE_FINISHED_REASON'):
        result['finished_reason'] = os.environ['FAKE_FINISHED_REASON']
    print(json.dumps({'event': 'result', 'result': result}))
"""


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in peer.PROVIDERS:
            cli = self.bin / name
            cli.write_text(FAKE_CLI.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
            cli.chmod(0o755)
        self.log = root / "call.json"
        self.cwd = root / "project"
        self.cwd.mkdir()
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"providers": {"claude": {
            "work_allowed_tools": ["Bash(git diff *)"]}}}))
        self.state = root / "state"
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log), "PEER_BRIDGE_DEPTH": "0",
            "FAKE_TERM_MARK": str(root / "term"), "FAKE_PID": str(root / "pid"),
            "XDG_STATE_HOME": str(self.state),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = peer.main(list(args) + ["--config", str(self.config), "--json"])
        self.stderr = errors.getvalue()
        return code, json.loads(output.getvalue())

    def text(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = peer.main(list(args) + ["--config", str(self.config)])
        self.stderr = errors.getvalue()
        return code, output.getvalue()

    def logged(self):
        return json.loads(self.log.read_text())

    def test_fresh_and_explicit_resume_for_each_provider(self):
        for provider in peer.PROVIDERS:
            with self.subTest(provider=provider):
                code, first = self.call("ask", "--to", provider, "--cwd", str(self.cwd),
                                        "--prompt", "First question")
                self.assertEqual(code, 0)
                self.assertEqual(first["status"], "ok")
                self.assertEqual(first["session_id"], provider + "-id")
                code, resumed = self.call("ask", "--to", provider, "--cwd", str(self.cwd),
                                          "--session", first["session_id"], "--prompt", "Follow up")
                self.assertEqual(code, 0)
                self.assertEqual(resumed["session_id"], first["session_id"])
                command = self.logged()["args"]
                self.assertIn(first["session_id"], command)
                self.assertNotIn("--ephemeral", command)
                self.assertNotIn("--no-session-persistence", command)

    def test_work_mode_passes_permissions_and_reports_git_status(self):
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--cwd", str(self.cwd), "--model", "gpt-6-sol",
                                     "--effort", "high", "--prompt", "Edit a file")
        self.assertEqual(code, 0)
        self.assertEqual(result["changed_paths"], ["?? edited.txt"])
        self.assertTrue(result["worktree_changed"])
        command = self.logged()["args"]
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("gpt-6-sol", command)
        self.assertIn('model_reasoning_effort="high"', command)
        code, result = self.call("ask", "--to", "claude", "--mode", "work",
                                 "--cwd", str(self.cwd), "--prompt", "Edit a file")
        command = self.logged()["args"]
        self.assertIn("acceptEdits", command)
        self.assertEqual(command[-2:], ["--allowedTools", "Bash(git diff *)"])
        code, result = self.call("ask", "--to", "agy", "--mode", "work",
                                 "--cwd", str(self.cwd), "--prompt", "Edit a file")
        command = self.logged()["args"]
        self.assertIn("accept-edits", command)
        self.assertIn("--sandbox", command)

    def test_agy_soft_denial_is_blocked_even_with_zero_exit(self):
        with mock.patch.dict(os.environ, {"FAKE_DENY": "1"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "work",
                                     "--cwd", str(self.cwd), "--prompt", "Run tests")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["session_id"], "agy-id")
        self.assertTrue(result["permission_notices"])

    def test_agy_result_denied_actions_are_blocked(self):
        with mock.patch.dict(os.environ, {"FAKE_RESULT_DENY": "1"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("RunCommand", result["permission_notices"][0])

    def test_agy_early_finished_reason_is_error(self):
        with mock.patch.dict(os.environ, {"FAKE_FINISHED_REASON": "print_timeout"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "error")
        self.assertIn("print_timeout", result["error"])
        with mock.patch.dict(os.environ, {"FAKE_TOOL_INFO_DENY": "1"}):
            code, result = self.call("ask", "--to", "agy", "--mode", "work",
                                     "--cwd", str(self.cwd), "--prompt", "Run command")
        self.assertEqual((code, result["status"]), (2, "blocked"))
        self.assertTrue(result["permission_notices"])

    def test_host_filesystem_denial_is_blocked(self):
        with mock.patch.dict(os.environ, {"FAKE_HOST_DENY": "1", "FAKE_NO_SESSION": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["error"], "codex was blocked before returning a session ID")
        self.assertTrue(result["permission_notices"])

    def test_codex_nonfatal_startup_warning_is_not_blocked(self):
        with mock.patch.dict(os.environ, {"FAKE_NONFATAL_WARN": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "consult",
                                     "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["permission_warnings"], 1)

    def test_structured_denials_are_blocked_for_every_provider(self):
        for provider in peer.PROVIDERS:
            with self.subTest(provider=provider):
                with mock.patch.dict(os.environ, {"FAKE_STRUCTURED_DENY": "1"}):
                    code, result = self.call("ask", "--to", provider, "--mode", "work",
                                             "--cwd", str(self.cwd), "--prompt", "Run a command")
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "blocked")
                self.assertTrue(result["permission_notices"])

    def test_success_without_session_id_is_error(self):
        with mock.patch.dict(os.environ, {"FAKE_NO_SESSION": "1"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                     "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("no session ID", result["error"])

    def test_codex_resume_uses_config_override_not_fresh_only_flags(self):
        self.call("ask", "--to", "codex", "--mode", "consult", "--cwd", str(self.cwd),
                  "--session", "codex-id", "--prompt", "Review")
        command = self.logged()["args"]
        self.assertIn("resume", command)
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertNotIn("-C", command)
        self.assertNotIn("--sandbox", command)

    def test_invalid_effort_fails_before_launch(self):
        code, result = self.call("ask", "--to", "agy", "--effort", "ultra",
                                 "--cwd", str(self.cwd), "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("agy effort", result["error"])
        self.assertFalse(self.log.exists())
        for level in ("xhigh", "max"):
            code, result = self.call("ask", "--to", "agy", "--effort", level,
                                     "--cwd", str(self.cwd), "--prompt", "Hello")
            self.assertEqual((code, result["effort"]), (0, level))

    def test_bounded_debate_retains_both_ids(self):
        code, result = self.call("debate", "--a", "codex", "--b", "claude",
                                 "--rounds", "2", "--cwd", str(self.cwd), "--prompt", "Tradeoffs?")
        self.assertEqual(code, 0)
        self.assertEqual(result["transcript"][0], {"provider": "codex", "response": "codex reply"})
        self.assertEqual(len(result["transcript"]), 4)
        self.assertEqual(result["sessions"], {"codex": "codex-id", "claude": "claude-id"})
        saved = json.loads(Path(result["output_file"]).read_text())
        self.assertTrue(all(turn["mode"] == "consult" for turn in saved["transcript"]))

    def test_nested_peer_call_is_rejected(self):
        with mock.patch.dict(os.environ, {"PEER_BRIDGE_DEPTH": "1"}):
            code, result = self.call("ask", "--to", "claude", "--cwd", str(self.cwd),
                                     "--prompt", "Hello")
        self.assertEqual(code, 2)
        self.assertIn("nested Peer calls", result["error"])

    def test_denial_text_in_successful_output_is_only_a_warning(self):
        with mock.patch.dict(os.environ, {"FAKE_GREP_HIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                     "--prompt", "Review the denial regex")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["permission_warnings"], 1)
        saved = json.loads(Path(result["output_file"]).read_text())
        self.assertEqual(saved["permission_notices"], [])
        self.assertIn("permission denied", saved["permission_warnings"][0])
        with mock.patch.dict(os.environ, {"FAKE_REPLY": "I fixed the 'Permission denied' error in deploy.sh"}):
            code, result = self.call("ask", "--to", "claude", "--mode", "work",
                                     "--cwd", str(self.cwd), "--prompt", "Fix deploy.sh")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["permission_warnings"])

    def test_timeout_asks_child_to_exit_before_killing(self):
        with mock.patch.dict(os.environ, {"FAKE_SLEEP": "30"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                     "--timeout", "0.5", "--prompt", "Slow")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["duration_seconds"], 10)
        self.assertTrue(Path(os.environ["FAKE_TERM_MARK"]).exists())

    def test_sigterm_stops_child_and_reports_interrupted(self):
        env = dict(os.environ, FAKE_SLEEP="30")
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "ask", "--to", "codex", "--cwd", str(self.cwd),
             "--config", str(self.config), "--prompt", "Slow", "--json"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pid_file = Path(os.environ["FAKE_PID"])
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists())
        proc.send_signal(signal.SIGTERM)
        stdout, _ = proc.communicate(timeout=20)
        result = json.loads(stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(result["status"], "interrupted")
        self.assertIn("SIGTERM", result["error"])
        self.assertTrue(Path(os.environ["FAKE_TERM_MARK"]).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        self.assertEqual(json.loads(Path(result["output_file"]).read_text())["status"], "interrupted")

    def test_work_detects_edits_to_an_already_dirty_file(self):
        (self.cwd / "edited.txt").write_text("original")
        code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                 "--cwd", str(self.cwd), "--prompt", "Look only")
        self.assertFalse(result["worktree_changed"])
        self.assertEqual(result["changed_paths"], [])
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--cwd", str(self.cwd), "--prompt", "Edit a file")
        self.assertTrue(result["worktree_changed"])
        self.assertEqual(result["changed_paths"], ["?? edited.txt"])

    def test_result_is_saved_privately_with_prompt(self):
        code, result = self.call("ask", "--to", "agy", "--cwd", str(self.cwd), "--prompt", "Hello")
        saved_path = Path(result["output_file"])
        self.assertEqual(saved_path.parent, self.state / "peer" / "runs")
        saved = json.loads(saved_path.read_text())
        self.assertEqual(saved["prompt"], "Hello")
        self.assertEqual(saved["session_id"], "agy-id")
        self.assertEqual(stat.S_IMODE(saved_path.stat().st_mode), 0o600)
        chosen = Path(self.tmp.name) / "out" / "result.json"
        code, result = self.call("ask", "--to", "agy", "--cwd", str(self.cwd),
                                 "--output", str(chosen), "--prompt", "Hello")
        self.assertEqual(result["output_file"], str(chosen.resolve()))
        self.assertTrue(chosen.exists())
        code, result = self.call("ask", "--to", "agy", "--cwd", str(self.cwd),
                                 "--no-save", "--prompt", "Hello")
        self.assertNotIn("output_file", result)

    def test_debate_stops_at_first_failed_turn(self):
        with mock.patch.dict(os.environ, {"FAKE_FAIL_PROVIDER": "claude"}):
            code, result = self.call("debate", "--a", "codex", "--b", "claude",
                                     "--rounds", "3", "--cwd", str(self.cwd), "--prompt", "Tradeoffs?")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "error")
        self.assertEqual([turn["provider"] for turn in result["transcript"]], ["codex", "claude"])
        self.assertEqual(result["sessions"]["codex"], "codex-id")
        self.assertEqual(len(json.loads(Path(result["output_file"]).read_text())["transcript"]), 2)

    def test_debate_marks_truncated_relay(self):
        text = peer.relay_text("x" * (peer.RELAY_LIMIT + 5))
        self.assertTrue(text.startswith("x" * peer.RELAY_LIMIT))
        self.assertIn("[truncated: first 12000 of 12005 characters shown]", text)
        self.assertEqual(peer.relay_text("short"), "short")

    def test_unknown_config_key_and_effort_override(self):
        self.config.write_text(json.dumps({"providers": {"agy": {"efort": "high"}}}))
        code, result = self.call("doctor")
        self.assertEqual(code, 2)
        self.assertIn("providers.agy.efort", result["error"])
        self.config.write_text(json.dumps({"providers": {"agy": {"efforts": ["low", "ultra"]}}}))
        code, result = self.call("ask", "--to", "agy", "--effort", "ultra",
                                 "--cwd", str(self.cwd), "--prompt", "Hello")
        self.assertEqual(result["status"], "ok")
        self.assertIn("ultra", self.logged()["args"])

    def test_stdout_is_one_compact_json_object_and_stderr_is_empty(self):
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Hi")
        self.assertEqual(self.stderr, "")
        self.assertEqual(result["response_chars"], len("codex reply"))
        for detail in ("diagnostics", "permission_notices", "exit_code", "error"):
            self.assertNotIn(detail, result)
        code, full = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--full", "--prompt", "Hi")
        self.assertEqual(full["permission_notices"], [])
        self.assertEqual(full["exit_code"], 0)

    def test_running_record_and_heartbeat_are_saved(self):
        records = []
        real_save = peer.save_result

        def capture(path, record):
            records.append(dict(record))
            real_save(path, record)

        with mock.patch.object(peer, "HEARTBEAT_SECONDS", 0.2), \
                mock.patch.object(peer, "save_result", side_effect=capture), \
                mock.patch.dict(os.environ, {"FAKE_SLEEP": "1"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Slow")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(records[0]["status"], "running")
        self.assertEqual(records[0]["pid"], os.getpid())
        beats = [r for r in records if "elapsed_seconds" in r and r["status"] == "running"]
        self.assertTrue(beats)
        self.assertIn("updated_at", beats[-1])
        self.assertEqual(records[-1]["status"], "ok")
        self.assertNotIn("pid", records[-1])

    def test_work_outside_git_compares_files(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        (plain / "slide.html").write_text("<p>old</p>")
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--cwd", str(plain), "--prompt", "Edit")
        self.assertTrue(result["worktree_changed"])
        self.assertEqual(result["changed_paths"], ["A edited.txt"])
        self.assertIn("not a Git repository", result["worktree_note"])
        with mock.patch.object(peer, "FILE_SCAN_LIMIT", 1):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--cwd", str(plain), "--prompt", "Edit")
        self.assertIsNone(result["worktree_changed"])
        self.assertIn("more than 1 files", result["worktree_note"])

    def test_session_names_create_resume_and_replace(self):
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                 "--session-name", "review", "--prompt", "Hi")
        self.assertEqual(result["status"], "error")
        self.assertIn("--new-session review", result["error"])
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                 "--new-session", "review", "--prompt", "Review the slide")
        self.assertEqual(result["session_name"], "review")
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                 "--session-name", "review", "--prompt", "Follow up")
        self.assertEqual(result["session_id"], "codex-id")
        self.assertIn("codex-id", self.logged()["args"])
        with mock.patch.dict(os.environ, {"FAKE_SESSION": "codex-id-2"}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd),
                                     "--new-session", "review", "--prompt", "Start over")
        self.assertEqual(result["replaced_session_id"], "codex-id")
        code, listing = self.call("sessions", "--cwd", str(self.cwd))
        newest = listing["sessions"][0]
        self.assertEqual((newest["session_id"], newest["names"]), ("codex-id-2", ["review"]))
        self.assertEqual(newest["first_prompt"], "Start over")
        code, result = self.call("ask", "--to", "claude", "--cwd", str(self.cwd),
                                 "--session-name", "review", "--prompt", "Hi")
        self.assertEqual(result["status"], "error")

    def test_debate_names_and_per_side_models(self):
        code, result = self.call("debate", "--a", "codex", "--b", "agy", "--rounds", "1",
                                 "--model-a", "gpt-6-sol", "--effort-b", "high", "--full",
                                 "--new-session", "cache", "--cwd", str(self.cwd), "--prompt", "Tradeoffs?")
        self.assertEqual(code, 0)
        self.assertEqual(result["transcript"][0]["model"], "gpt-6-sol")
        self.assertEqual(result["transcript"][1]["effort"], "high")
        code, result = self.call("debate", "--a", "codex", "--b", "agy", "--rounds", "1",
                                 "--session-name", "cache", "--cwd", str(self.cwd), "--prompt", "Again")
        self.assertEqual(result["sessions"], {"codex": "codex-id", "agy": "agy-id"})
        self.assertIn("--conversation", self.logged()["args"])

    def test_model_aliases_resolve_and_are_reported(self):
        self.config.write_text(json.dumps({"providers": {"codex": {"aliases": {"Sol": "gpt-6-sol"}}}}))
        code, result = self.call("ask", "--to", "codex", "--model", "sol",
                                 "--cwd", str(self.cwd), "--prompt", "Hi")
        self.assertEqual((result["model"], result["model_alias"]), ("gpt-6-sol", "sol"))
        self.assertIn("gpt-6-sol", self.logged()["args"])
        code, result = self.call("models")
        self.assertEqual(result["providers"]["agy"]["live"], ["gemini-test-high"])
        self.assertEqual(result["providers"]["codex"]["aliases"], {"Sol": "gpt-6-sol"})
        self.assertIn("ask the user", result["providers"]["claude"]["check"])

    def test_attachments(self):
        outside = Path(self.tmp.name) / "renders"
        outside.mkdir()
        image, pdf = outside / "slide.png", outside / "spec.pdf"
        image.write_bytes(b"png")
        pdf.write_bytes(b"pdf")
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--attach", str(image),
                                 "--attach", str(pdf), "--prompt", "Review the render")
        logged = self.logged()
        self.assertIn("--image=" + str(image.resolve()), logged["args"])
        self.assertFalse(any(str(pdf.resolve()) in arg for arg in logged["args"]))
        self.assertIn(str(pdf.resolve()), logged["stdin"])
        self.assertEqual([a["native"] for a in result["attachments"]], [True, False])
        code, result = self.call("ask", "--to", "claude", "--cwd", str(self.cwd),
                                 "--attach", str(image), "--prompt", "Review")
        args = self.logged()["args"]
        self.assertEqual(args[args.index("--add-dir") + 1], str(outside.resolve()))
        code, result = self.call("ask", "--to", "agy", "--cwd", str(self.cwd),
                                 "--attach", str(outside / "missing.png"), "--prompt", "Review")
        self.assertIn("attachment not found", result["error"])

    def test_signal_killed_command_is_a_warning(self):
        with mock.patch.dict(os.environ, {"FAKE_CRASH": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work", "--full",
                                     "--cwd", str(self.cwd), "--prompt", "Render the slide")
        self.assertEqual(result["status"], "ok")
        self.assertIn("code 134", result["permission_warnings"][0])
        self.assertIn("chrome --headless", result["permission_warnings"][0])

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=Peer Tests", "-c", "user.email=tests@example.invalid",
                        *args], cwd=self.cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_work_detects_committed_changes_and_initial_commit(self):
        before = peer.worktree_snapshot(str(self.cwd))
        target = self.cwd / "tracked.txt"
        target.write_text("first")
        self.git("add", ".")
        self.git("commit", "-qm", "First commit")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))
        self.assertTrue(report["worktree_changed"])
        self.assertEqual(report["changed_paths"], ["A tracked.txt"])
        before = peer.worktree_snapshot(str(self.cwd))
        target.write_text("second")
        self.git("add", ".")
        self.git("commit", "-qm", "Child edit")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))
        self.assertTrue(report["worktree_changed"])
        self.assertEqual(report["changed_paths"], ["M tracked.txt"])
        before = peer.worktree_snapshot(str(self.cwd))
        self.git("commit", "--allow-empty", "-qm", "History changed")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))
        self.assertTrue(report["worktree_changed"])
        self.assertEqual(report["changed_paths"], [])
        self.assertIn("HEAD changed", report["worktree_note"])

    def test_dirty_file_in_repository_subdirectory(self):
        folder = self.cwd / "src"
        folder.mkdir()
        target = folder / "tracked.txt"
        target.write_text("first")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        target.write_text("second")
        before = peer.worktree_snapshot(str(folder))
        target.write_text("third")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(folder)))
        self.assertTrue(report["worktree_changed"])
        self.assertEqual(report["changed_paths"], [" M src/tracked.txt"])

    def test_index_and_permissions_change_on_an_already_dirty_file(self):
        target = self.cwd / "tracked.txt"
        target.write_text("first")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        target.write_text("staged one")
        before = peer.worktree_snapshot(str(self.cwd))
        self.git("add", ".")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))
        self.assertTrue(report["worktree_changed"])
        target.write_text("staged two")
        self.git("add", ".")
        target.write_text("working copy")
        before = peer.worktree_snapshot(str(self.cwd))
        target.write_text("staged three")
        self.git("add", ".")
        target.write_text("working copy")
        report = peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))
        self.assertTrue(report["worktree_changed"])
        before = peer.worktree_snapshot(str(self.cwd))
        target.chmod(0o755)
        self.assertTrue(peer.worktree_report(before, peer.worktree_snapshot(str(self.cwd)))["worktree_changed"])

    def test_invalid_config_types_and_nonfinite_timeouts_are_json_errors(self):
        for value in (["high"], {"level": "high"}, 1, True):
            for command in ("doctor", "models"):
                with self.subTest(value=value, command=command):
                    self.config.write_text(json.dumps({"providers": {"codex": {"effort": value}}}))
                    code, result = self.call(command)
                    self.assertEqual(code, 2)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(self.stderr, "")
        for value in (float("nan"), float("inf"), float("-inf"), 10 ** 400, True, 0, "600"):
            with self.subTest(timeout=value):
                self.config.write_text(json.dumps({"timeout_seconds": value}))
                code, result = self.call("doctor")
                self.assertEqual(code, 2)
                self.assertIn("finite positive", result["error"])
        self.assertFalse(self.log.exists())

    def test_argument_errors_are_json(self):
        code, result = self.call("ask", "--to", "unknown")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.stderr, "")

    def test_debate_preflights_both_sides_before_launch(self):
        code, result = self.call("debate", "--a", "codex", "--b", "agy", "--effort-b", "ultra",
                                 "--cwd", str(self.cwd), "--prompt", "Question")
        self.assertEqual(code, 2)
        self.assertFalse(self.log.exists())
        with mock.patch.object(peer.shutil, "which", side_effect=lambda p: "codex" if p == "codex" else None):
            code, result = self.call("debate", "--a", "codex", "--b", "claude",
                                     "--cwd", str(self.cwd), "--prompt", "Question")
        self.assertEqual(code, 2)
        self.assertIn("claude CLI", result["error"])
        self.assertFalse(self.log.exists())

    def test_debate_retains_completed_turn_on_exception_and_without_saving(self):
        first = {"status": "ok", "provider": "codex", "session_id": "resume-me", "response": "Answer"}
        for no_save in (False, True):
            with self.subTest(no_save=no_save), mock.patch.object(peer, "ask", side_effect=[
                    first, ValueError("launch failed after preflight")]):
                extra = ["--no-save"] if no_save else []
                code, result = self.call("debate", "--a", "codex", "--b", "claude", "--cwd", str(self.cwd),
                                         "--prompt", "Question", *extra)
            self.assertEqual(code, 2)
            self.assertEqual(result["sessions"]["codex"], "resume-me")
            self.assertEqual(result["transcript"], [first])
            if not no_save:
                saved = json.loads(Path(result["output_file"]).read_text())
                self.assertEqual(saved["transcript"], [first])

    def test_session_name_write_failure_retains_successful_response(self):
        for command, flags in (("ask", ["--to", "codex"]),
                               ("debate", ["--a", "codex", "--b", "claude"])):
            with self.subTest(command=command), mock.patch.object(peer, "bind_name", side_effect=OSError("read-only state")):
                code, result = self.call(command, *flags, "--new-session", "review", "--cwd", str(self.cwd),
                                         "--prompt", "Question")
            self.assertEqual(code, 2)
            if command == "ask":
                self.assertEqual(result["session_id"], "codex-id")
                self.assertEqual(result["response"], "codex reply")
            else:
                self.assertEqual(result["sessions"]["codex"], "codex-id")
                self.assertEqual(result["transcript"][0]["response"], "codex reply")

    def test_debate_relay_sends_brief_once_per_side(self):
        with mock.patch.object(peer, "ask", wraps=peer.ask) as calls:
            code, result = self.call("debate", "--a", "codex", "--b", "claude", "--rounds", "2",
                                     "--cwd", str(self.cwd), "--prompt", "Unique original brief")
        self.assertEqual(code, 0)
        messages = [call.args[3] for call in calls.call_args_list]
        self.assertTrue(all("Unique original brief" in message for message in messages[:2]))
        self.assertTrue(all("Unique original brief" not in message for message in messages[2:]))
        self.assertIn("claude reply", messages[2])

    def test_fast_runs_have_distinct_files_and_sessions_sort_by_subsecond_time(self):
        with mock.patch.object(peer.time, "strftime", return_value="20261013-001000"):
            _, first = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "First")
            with mock.patch.dict(os.environ, {"FAKE_SESSION": "newer-id"}):
                _, second = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Second")
        self.assertNotEqual(first["output_file"], second["output_file"])
        stamp = 1800000000 * 10 ** 9
        os.utime(first["output_file"], ns=(stamp + 100, stamp + 100))
        os.utime(second["output_file"], ns=(stamp + 200, stamp + 200))
        _, result = self.call("sessions", "--cwd", str(self.cwd))
        self.assertEqual([s["session_id"] for s in result["sessions"]], ["newer-id", "codex-id"])

    def test_session_first_prompt_uses_run_time_not_filename_order(self):
        runs = self.state / "peer" / "runs"
        older, newer = runs / "z-older.json", runs / "a-newer.json"
        self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--output", str(older), "--prompt", "Original")
        self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--output", str(newer),
                  "--session", "codex-id", "--prompt", "Follow-up")
        stamp = 1800000000 * 10 ** 9
        os.utime(older, ns=(stamp, stamp))
        os.utime(newer, ns=(stamp + 10 ** 9, stamp + 10 ** 9))
        _, result = self.call("sessions", "--cwd", str(self.cwd))
        entry = result["sessions"][0]
        self.assertEqual(entry["first_prompt"], "Original")
        self.assertEqual(entry["runs"], 2)
        self.assertEqual(entry["last_used"], time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(1800000001)))

    def test_session_names_do_not_leak_between_workspaces(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--new-session", "here", "--prompt", "First")
        self.call("ask", "--to", "codex", "--cwd", str(other), "--new-session", "there", "--prompt", "Second")
        _, result = self.call("sessions", "--all")
        entries = {entry["cwd"]: entry for entry in result["sessions"]}
        self.assertEqual(entries[str(self.cwd.resolve())]["names"], ["here"])
        self.assertEqual(entries[str(other.resolve())]["names"], ["there"])

    STRUCTURED = ("Not ready. Two blockers:\n1. Stop fill (runner.py:221).\n\n**Questions:**\n"
                  "- Flatten or cancel?\n2. Which account?\n\n---details---\nFull log line one.\nLine two.")

    def test_text_output_is_a_header_then_the_answer(self):
        code, text = self.text("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Hi")
        lines = text.splitlines()
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("peer: ok | codex default model | consult | session codex-id"))
        self.assertTrue(any(line.startswith("file: ") for line in lines))
        self.assertEqual(lines[-2:], ["---", "codex reply"])
        code, text = self.text("ask", "--to", "codex", "--cwd", str(self.cwd), "--new-session", "named",
                               "--prompt", "Hi")
        self.assertIn("| session named codex-id |", text.splitlines()[0])
        self.assertNotIn("file: ", text)
        code, text = self.text("reply", "missing", "--cwd", str(self.cwd), "--prompt", "Hi")
        self.assertEqual(code, 2)
        self.assertTrue(text.startswith("peer: error\nerror: no session named 'missing'"))
        self.assertIn("names here: named (codex)", text)

    def test_reply_contract_splits_answer_questions_and_details(self):
        with mock.patch.dict(os.environ, {"FAKE_REPLY": self.STRUCTURED}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Review")
            _, text = self.text("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertIn("Reply format requested by the Peer bridge", self.logged()["stdin"])
        self.assertIn("at most 400 words", self.logged()["stdin"])
        self.assertNotIn("Full log", result["response"])
        self.assertEqual(result["questions"], ["Flatten or cancel?", "Which account?"])
        self.assertEqual(result["details_chars"], len("Full log line one.\nLine two."))
        saved = json.loads(Path(result["output_file"]).read_text())
        self.assertEqual(saved["details"], "Full log line one.\nLine two.")
        self.assertIn("questions: 2", text)
        self.assertIn("details: 28 characters not shown", text)
        self.assertNotIn("Full log", text)
        with mock.patch.dict(os.environ, {"FAKE_REPLY": self.STRUCTURED}):
            code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--words", "0",
                                     "--prompt", "Review")
        self.assertNotIn("Reply format", self.logged()["stdin"])
        self.assertEqual(result["response"], self.STRUCTURED)
        self.assertNotIn("questions", result)

    def test_split_reply_edge_cases(self):
        self.assertEqual(peer.split_reply("Done.\nQUESTIONS: none", 400)[2], [])
        self.assertEqual(peer.split_reply("Done.\nQuestions: Which one?", 400)[2], ["Which one?"])
        self.assertEqual(peer.split_reply("Intro.\nQUESTIONS:\n- Mid-body?\n\nTrailing prose paragraph.", 400)[2], [])
        self.assertEqual(peer.split_reply("---details---\nOnly details", 400)[:2], ("Only details", ""))
        self.assertEqual(peer.split_reply("Text\n--- details ---\nMore", 400)[:2], ("Text", "More"))
        self.assertEqual(peer.split_reply("Text\n---details---\nMore", 0), ("Text\n---details---\nMore", "", []))
        fenced = "Summary:\n```markdown\nQUESTIONS:\n- Fake?\n---details---\nFenced body\n```\nReal conclusion.\n---details---\nReal details"
        ans, det, qs = peer.split_reply(fenced, 400)
        self.assertIn("Real conclusion.", ans)
        self.assertEqual(det, "Real details")
        self.assertEqual(qs, [])

    def test_reply_inherits_settings_and_runs_in_the_session_workspace(self):
        sub = self.cwd / "src" / "deep"
        sub.mkdir(parents=True)
        self.call("ask", "--to", "codex", "--mode", "work", "--model", "gpt-6-sol", "--effort", "high",
                  "--words", "150", "--new-session", "build", "--cwd", str(self.cwd), "--prompt", "Build it")
        code, result = self.call("reply", "build", "--cwd", str(sub), "--prompt", "Also handle empty input")
        args = self.logged()["args"]
        self.assertEqual(code, 0)
        self.assertEqual(result["cwd"], str(self.cwd.resolve()))
        self.assertEqual((result["mode"], result["model"], result["effort"]), ("work", "gpt-6-sol", "high"))
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertIn("codex-id", args)
        self.assertIn("at most 150 words", self.logged()["stdin"])
        self.call("reply", "build", "--cwd", str(sub), "--effort", "low", "--prompt", "Lower effort now")
        self.call("reply", "build", "--cwd", str(sub), "--prompt", "And keep it")
        self.assertIn('model_reasoning_effort="low"', self.logged()["args"])

    def test_names_resolve_up_and_down_the_tree_but_not_across(self):
        child, sibling = self.cwd / "pkg", Path(self.tmp.name) / "sibling"
        child.mkdir()
        sibling.mkdir()
        self.call("ask", "--to", "claude", "--new-session", "inner", "--cwd", str(child), "--prompt", "Hi")
        code, result = self.call("reply", "inner", "--cwd", str(self.cwd), "--prompt", "From the parent")
        self.assertEqual((code, result["cwd"]), (0, str(child.resolve())))
        code, result = self.call("reply", "inner", "--cwd", str(sibling), "--prompt", "From a sibling")
        self.assertEqual(code, 2)
        _, listing = self.call("sessions", "--cwd", str(self.cwd))
        self.assertEqual([e["names"] for e in listing["sessions"]], [["inner"]])
        _, listing = self.call("sessions", "--cwd", str(sibling))
        self.assertEqual(listing["sessions"], [])

    def test_reply_needs_provider_when_a_name_has_several(self):
        self.call("debate", "--a", "codex", "--b", "agy", "--rounds", "1", "--new-session", "pair",
                  "--cwd", str(self.cwd), "--prompt", "Question")
        code, result = self.call("reply", "pair", "--cwd", str(self.cwd), "--prompt", "Follow up")
        self.assertEqual(code, 2)
        self.assertIn("pass --to PROVIDER", result["error"])
        code, result = self.call("reply", "pair", "--to", "agy", "--cwd", str(self.cwd), "--prompt", "Follow up")
        self.assertEqual((code, result["provider"], result["session_id"]), (0, "agy", "agy-id"))

    def test_show_details_last_and_json(self):
        with mock.patch.dict(os.environ, {"FAKE_REPLY": self.STRUCTURED}):
            self.call("ask", "--to", "codex", "--new-session", "rev", "--cwd", str(self.cwd), "--prompt", "Review")
        self.call("reply", "rev", "--cwd", str(self.cwd), "--prompt", "Recheck")
        code, text = self.text("show", "rev", "--cwd", str(self.cwd), "--details")
        self.assertEqual(code, 0)
        self.assertIn('you: "Review"', text)
        self.assertIn("Full log line one.", text)
        code, text = self.text("show", "rev", "--cwd", str(self.cwd), "--last", "2")
        self.assertIn("peer: last 2 runs for rev", text)
        self.assertIn('you: "Recheck"', text)
        self.assertIn("Flatten or cancel?", text)
        code, text = self.text("show", "--cwd", str(self.cwd))
        self.assertTrue(text.startswith("peer: ok | codex"))
        self.assertTrue(text.rstrip().endswith("codex reply"))
        code, data = self.call("show", "rev", "--cwd", str(self.cwd))
        self.assertEqual(data["prompt"], "Recheck")

    def test_wait_reports_running_then_finished_and_stale(self):
        out = Path(self.tmp.name) / "slow.json"
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "ask", "--to", "codex", "--cwd", str(self.cwd), "--config",
             str(self.config), "--new-session", "slow", "--output", str(out), "--prompt", "Slow"],
            env=dict(os.environ, FAKE_SLEEP="3"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        code, text = self.text("wait", str(out), "--timeout", "0.5")
        self.assertEqual(code, peer.EXIT_RUNNING)
        self.assertTrue(text.startswith("peer: running | codex | session slow"))
        code, text = self.text("wait", "slow", "--cwd", str(self.cwd), "--timeout", "20")
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("peer: ok | codex"))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        stale = Path(self.tmp.name) / "stale.json"
        peer.save_result(stale, {"status": "running", "pid": dead.pid, "providers": ["codex"],
                                 "cwd": str(self.cwd), "started_at": peer.now(), "updated_at": peer.now()})
        code, text = self.text("wait", str(stale), "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertIn("peer: stale", text)

    def test_debate_relays_details_but_prints_answers(self):
        with mock.patch.dict(os.environ, {"FAKE_REPLY": self.STRUCTURED}), \
                mock.patch.object(peer, "ask", wraps=peer.ask) as calls:
            code, result = self.call("debate", "--a", "codex", "--b", "claude", "--rounds", "1",
                                     "--cwd", str(self.cwd), "--prompt", "Question")
        self.assertEqual(code, 0)
        self.assertIn("Full log line one.", calls.call_args_list[1].args[3])
        self.assertTrue(all(call.kwargs["debate"] for call in calls.call_args_list))
        self.assertNotIn("QUESTIONS", self.logged()["stdin"].split("Reply format")[-1])
        self.assertEqual(result["transcript"][0]["details_chars"], 28)
        self.assertNotIn("Full log", json.dumps(result["transcript"]))

    def test_reply_words_config_and_flag_validation(self):
        self.config.write_text(json.dumps({"reply_words": -1}))
        code, result = self.call("doctor")
        self.assertEqual(code, 2)
        self.assertIn("reply_words", result["error"])
        self.config.write_text(json.dumps({"reply_words": 0}))
        self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--prompt", "Hi")
        self.assertNotIn("Reply format", self.logged()["stdin"])
        code, result = self.call("ask", "--to", "codex", "--cwd", str(self.cwd), "--words", "-5", "--prompt", "Hi")
        self.assertEqual(code, 2)
        self.assertIn("--words", result["error"])

    def test_claude_stream_json_heartbeat_and_timeout_recover_session(self):
        records = []
        real_save = peer.save_result

        def capture(path, record):
            records.append(dict(record))
            real_save(path, record)

        with mock.patch.object(peer, "HEARTBEAT_SECONDS", 0.1), \
                mock.patch.object(peer, "save_result", side_effect=capture), \
                mock.patch.dict(os.environ, {"FAKE_SLEEP": "1.5"}):
            code, result = self.call("ask", "--to", "claude", "--cwd", str(self.cwd),
                                     "--timeout", "0.4", "--prompt", "Slow claude")
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["session_id"], "claude-id")
        args = self.logged()["args"]
        self.assertIn("stream-json", args)
        self.assertIn("--verbose", args)
        self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
        beats = [r for r in records if r.get("status") == "running" and r.get("session_id") == "claude-id"]
        self.assertTrue(beats)
        self.assertEqual(beats[-1]["last_activity"], "assistant tool_use Read")

    def test_auto_mode_passes_provider_flags_and_tracks_changes(self):
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "auto",
                                     "--cwd", str(self.cwd), "--prompt", "Fix and test")
        self.assertEqual((code, result["mode"], result["worktree_changed"]), (0, "auto", True))
        args = self.logged()["args"]
        self.assertIn('approval_policy="on-request"', args)
        self.assertIn('approvals_reviewer="auto_review"', args)
        self.assertIn('sandbox_mode="workspace-write"', args)
        code, result = self.call("ask", "--to", "claude", "--mode", "auto",
                                 "--cwd", str(self.cwd), "--prompt", "Fix and test")
        args = self.logged()["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
        self.assertEqual(args[-2:], ["--allowedTools", "Bash(git diff *)"])
        code, result = self.call("ask", "--to", "agy", "--mode", "auto",
                                 "--cwd", str(self.cwd), "--prompt", "Fix and test")
        args = self.logged()["args"]
        self.assertEqual(args[args.index("--mode") + 1], "auto")

    def test_session_forking_for_codex_and_claude_and_agy_error(self):
        self.call("ask", "--to", "codex", "--mode", "work", "--effort", "high",
                  "--new-session", "base", "--cwd", str(self.cwd), "--prompt", "Initial")
        code, forked = self.call("ask", "--fork-from", "base", "--new-session", "branch",
                                 "--cwd", str(self.cwd), "--prompt", "Try alternative")
        self.assertEqual(code, 0)
        self.assertEqual((forked["provider"], forked["mode"], forked["effort"]), ("codex", "work", "high"))
        self.assertEqual((forked["session_id"], forked["forked_from"]), ("codex-fork-id", "codex-id"))
        self.assertEqual(self.logged()["args"][:2], ["exec", "fork"])
        self.assertIn("codex-id", self.logged()["args"])
        # Original session name still points to codex-id; new name points to codex-fork-id.
        _, orig = self.call("reply", "base", "--cwd", str(self.cwd), "--prompt", "Continue base")
        self.assertEqual(orig["session_id"], "codex-id")
        _, br = self.call("reply", "branch", "--cwd", str(self.cwd), "--prompt", "Continue branch")
        self.assertEqual(br["session_id"], "codex-fork-id")
        # Direct --fork-session on Claude
        code, cfork = self.call("ask", "--to", "claude", "--fork-session", "claude-id",
                                "--cwd", str(self.cwd), "--prompt", "Fork claude")
        self.assertEqual((code, cfork["session_id"], cfork["forked_from"]), (0, "claude-fork-id", "claude-id"))
        self.assertIn("--fork-session", self.logged()["args"])
        # Antigravity rejects fork before launch
        code, err = self.call("ask", "--to", "agy", "--fork-session", "agy-id",
                              "--cwd", str(self.cwd), "--prompt", "Fork agy")
        self.assertEqual(code, 2)
        self.assertIn("does not support session forking", err["error"])

    def test_managed_git_worktree_isolates_edits_and_persists_across_replies(self):
        (self.cwd / "tracked.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        with mock.patch.dict(os.environ, {"FAKE_EDIT": "1"}):
            code, result = self.call("ask", "--to", "codex", "--mode", "work",
                                     "--new-session", "iso", "--worktree",
                                     "--cwd", str(self.cwd), "--prompt", "Edit in worktree")
        self.assertEqual(code, 0)
        wt_path = Path(result["worktree"])
        self.assertEqual(result["worktree_branch"], "peer/iso")
        self.assertNotEqual(wt_path, self.cwd.resolve())
        self.assertTrue((wt_path / "edited.txt").exists())
        self.assertFalse((self.cwd / "edited.txt").exists())
        self.assertEqual(result["changed_paths"], ["?? edited.txt"])
        # reply from the main checkout runs in the worktree automatically
        code, follow = self.call("reply", "iso", "--cwd", str(self.cwd), "--prompt", "Follow up")
        self.assertEqual((code, follow["cwd"], follow["worktree"]), (0, str(wt_path), str(wt_path)))
        # show and sessions from the main checkout find the worktree session
        code, shown = self.text("show", "iso", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertIn("worktree: {} (branch peer/iso)".format(wt_path), shown)
        _, listing = self.call("sessions", "--cwd", str(self.cwd))
        self.assertEqual(listing["sessions"][0]["worktree"], str(wt_path))
        # Commit on peer/iso, remove worktree checkout, and verify recreating reattaches without resetting peer/iso
        subprocess.run(["git", "-c", "user.name=Peer Tests", "-c", "user.email=tests@example.invalid",
                        "add", "."], cwd=wt_path, check=True)
        subprocess.run(["git", "-c", "user.name=Peer Tests", "-c", "user.email=tests@example.invalid",
                        "commit", "-qm", "Worktree commit"], cwd=wt_path, check=True)
        shutil.rmtree(wt_path)
        code, reattached = self.call("ask", "--to", "codex", "--mode", "consult",
                                     "--worktree", "iso", "--cwd", str(self.cwd), "--prompt", "Check")
        self.assertEqual(code, 0)
        self.assertTrue((Path(reattached["worktree"]) / "edited.txt").exists())

    def test_show_scopes_to_exact_workspace_over_newer_child_with_same_name(self):
        child = self.cwd / "sub"
        child.mkdir()
        with mock.patch.dict(os.environ, {"FAKE_SESSION": "parent-sid", "FAKE_REPLY": "parent answer"}):
            self.call("ask", "--to", "codex", "--new-session", "same",
                      "--cwd", str(self.cwd), "--prompt", "Parent prompt")
        with mock.patch.dict(os.environ, {"FAKE_SESSION": "child-sid", "FAKE_REPLY": "child answer"}):
            self.call("ask", "--to", "codex", "--new-session", "same",
                      "--cwd", str(child), "--prompt", "Child prompt")
        code, data = self.call("show", "same", "--cwd", str(self.cwd))
        self.assertEqual((code, data["session_id"], data["response"]), (0, "parent-sid", "parent answer"))
        code, cdata = self.call("show", "same", "--cwd", str(child))
        self.assertEqual((code, cdata["session_id"], cdata["response"]), (0, "child-sid", "child answer"))

    def test_show_includes_latest_failed_run_and_json_named_session(self):
        self.call("ask", "--to", "codex", "--new-session", "review.json",
                  "--cwd", str(self.cwd), "--prompt", "Initial")
        code, data = self.call("show", "review.json", "--cwd", str(self.cwd))
        self.assertEqual((code, data["session_name"]), (0, "review.json"))
        with mock.patch.dict(os.environ, {"FAKE_FAIL_PROVIDER": "codex"}):
            fcode, _ = self.call("reply", "review.json", "--cwd", str(self.cwd), "--prompt", "Fail turn")
        self.assertEqual(fcode, 2)
        code, latest = self.call("show", "review.json", "--cwd", str(self.cwd))
        self.assertEqual((code, latest["status"], latest["prompt"]), (0, "error", "Fail turn"))

    def test_saved_worktree_rejects_drifted_branch(self):
        (self.cwd / "tracked.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        _, result = self.call("ask", "--to", "codex", "--new-session", "wtcheck", "--worktree",
                              "--cwd", str(self.cwd), "--prompt", "Start")
        wt_path = Path(result["worktree"])
        subprocess.run(["git", "checkout", "-qb", "other-branch"], cwd=wt_path, check=True)
        code, err = self.call("reply", "wtcheck", "--cwd", str(self.cwd), "--prompt", "Resume")
        self.assertEqual(code, 2)
        self.assertIn("does not match repository", err["error"])
        self.assertIn("on branch peer/wtcheck", err["error"])

    def test_agy_work_and_auto_pass_proceed_in_sandbox_and_worktree_git_dir(self):
        code, _ = self.call("ask", "--to", "agy", "--mode", "consult", "--cwd", str(self.cwd), "--prompt", "Review")
        self.assertEqual(code, 0)
        args = self.logged()["args"]
        self.assertIn("--sandbox", args)
        self.assertIn("--settings", args)
        consult_settings = json.loads(args[args.index("--settings") + 1])
        self.assertNotIn("toolPermission", consult_settings)
        self.assertIn("command(ls)", consult_settings["permissions"]["allow"])
        self.assertIn("command(git status)", consult_settings["permissions"]["allow"])
        self.assertIn("Prefer built-in file read/search tools", self.logged()["stdin"])
        for mode in ("work", "auto"):
            code, _ = self.call("ask", "--to", "agy", "--mode", mode, "--cwd", str(self.cwd), "--prompt", "Edit")
            self.assertEqual(code, 0)
            args = self.logged()["args"]
            self.assertIn("--sandbox", args)
            self.assertIn("--settings", args)
            self.assertEqual(json.loads(args[args.index("--settings") + 1]),
                             {"toolPermission": "proceed-in-sandbox"})
        # Custom --settings in extra args suppresses Peer's default --settings
        code, _ = self.call("ask", "--to", "agy", "--mode", "work", "--cwd", str(self.cwd),
                            "--extra-arg=--settings", '--extra-arg={"toolPermission":"strict"}',
                            "--prompt", "Edit")
        self.assertEqual(code, 0)
        args = self.logged()["args"]
        self.assertEqual(args.count("--settings"), 1)
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"toolPermission": "strict"})
        # Linked Git worktree automatically exposes only .git common dir to agy
        (self.cwd / "tracked.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        code, wt_res = self.call("ask", "--to", "agy", "--mode", "work", "--worktree", "agywt",
                                 "--cwd", str(self.cwd), "--prompt", "Edit in worktree")
        self.assertEqual(code, 0)
        args = self.logged()["args"]
        expected_git = str((self.cwd / ".git").resolve())
        self.assertIn("--add-dir", args)
        self.assertEqual(args[args.index("--add-dir") + 1], expected_git)
        self.assertNotEqual(wt_res["cwd"], str(self.cwd.resolve()))

    def test_extra_args_via_cli_passthrough_config_and_session_persistence(self):
        # Passthrough via -- on agy skips --sandbox and default --settings
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = peer.main(["ask", "--to", "agy", "--mode", "work", "--new-session", "agy-bypass",
                              "--cwd", str(self.cwd), "--config", str(self.config), "--json",
                              "--prompt", "Run anything", "--", "--dangerously-skip-permissions"])
        result = json.loads(out.getvalue())
        self.assertEqual((code, result["status"], result["extra_args"]),
                         (0, "ok", ["--dangerously-skip-permissions"]))
        args = self.logged()["args"]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--sandbox", args)
        self.assertNotIn("--settings", args)
        # reply inherits saved extra_args without duplicating config extra_args
        self.config.write_text(json.dumps({"providers": {
            "agy": {"extra_args": ["--disable-slash-commands"]},
            "claude": {"work_allowed_tools": ["Bash(git diff *)"]},
        }}))
        _, doc = self.call("doctor")
        self.assertEqual(doc["providers"]["agy"]["extra_args"], ["--disable-slash-commands"])
        code, text = self.text("reply", "agy-bypass", "--cwd", str(self.cwd), "--prompt", "Continue")
        self.assertEqual(code, 0)
        self.assertIn("extra args: --disable-slash-commands --dangerously-skip-permissions", text)
        args = self.logged()["args"]
        self.assertEqual(args.count("--disable-slash-commands"), 1)
        self.assertEqual(args.count("--dangerously-skip-permissions"), 1)
        self.assertNotIn("--sandbox", args)
        # Claude bypass omits --permission-mode and puts extra args before variadic --allowedTools
        code, _ = self.call("ask", "--to", "claude", "--mode", "work", "--cwd", str(self.cwd),
                            "--extra-arg=--dangerously-skip-permissions", "--prompt", "Edit")
        self.assertEqual(code, 0)
        args = self.logged()["args"]
        self.assertNotIn("--permission-mode", args)
        self.assertLess(args.index("--dangerously-skip-permissions"), args.index("--allowedTools"))
        # Codex bypass omits default approval_policy/sandbox_mode and puts extra args before -C/-
        code, _ = self.call("ask", "--to", "codex", "--mode", "work", "--cwd", str(self.cwd),
                            "--extra-arg=--dangerously-bypass-approvals-and-sandbox", "--prompt", "Edit")
        self.assertEqual(code, 0)
        args = self.logged()["args"]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertFalse(any("sandbox_mode=" in a for a in args))
        self.assertLess(args.index("--dangerously-bypass-approvals-and-sandbox"), args.index("-C"))
        # Invalid config extra_args type fails cleanly
        self.config.write_text(json.dumps({"providers": {"agy": {"extra_args": "--bad"}}}))
        code, err_res = self.call("doctor")
        self.assertEqual(code, 2)
        self.assertIn("extra_args", err_res["error"])

    def test_debate_extra_args_and_passthrough_errors(self):
        code, result = self.call("debate", "--a", "codex", "--b", "agy", "--rounds", "1",
                                 "--extra-arg-a=--ephemeral", "--extra-arg-b=--dangerously-skip-permissions",
                                 "--cwd", str(self.cwd), "--prompt", "Debate it")
        self.assertEqual(code, 0)
        # Last turn was agy with --dangerously-skip-permissions and no --sandbox
        args = self.logged()["args"]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--sandbox", args)
        # -- on debate is rejected cleanly
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = peer.main(["debate", "--a", "codex", "--b", "agy", "--cwd", str(self.cwd),
                              "--config", str(self.config), "--json", "--prompt", "Q", "--", "--foo"])
        self.assertEqual(code, 2)
        self.assertIn("--extra-arg-a", json.loads(out.getvalue())["error"])
        # -- on show is rejected cleanly
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = peer.main(["show", "--cwd", str(self.cwd), "--config", str(self.config),
                              "--json", "--", "--foo"])
        self.assertEqual(code, 2)
        self.assertIn("ask and reply", json.loads(out.getvalue())["error"])

    def test_modular_split_keeps_files_focused(self):
        paths = [SCRIPT] + [SCRIPT.parent / "peer_lib" / name for name in (
            "__init__.py", "constants.py", "config.py", "workspace.py",
            "providers.py", "storage.py", "render.py",
        )]
        for path in paths:
            self.assertTrue(path.is_file(), "missing module: {}".format(path))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertLess(len(lines), 450, "{} has {} lines".format(path.name, len(lines)))
        root_link = SCRIPT.parents[3] / "peer"
        if root_link.exists():
            proc = subprocess.run([sys.executable, str(root_link), "doctor", "--config", str(self.config)],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(proc.stdout)["status"], "ok")


if __name__ == "__main__":
    unittest.main()
