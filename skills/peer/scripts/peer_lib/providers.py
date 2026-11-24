"""Provider adapters, prompt wrapping, reply splitting, stream parsing, and child execution."""

import json
import os
import signal
import subprocess
import threading
import time

from peer_lib.constants import (
    DENIAL,
    HEARTBEAT_SECONDS,
    NONFATAL_STDERR,
    STOP_GRACE_SECONDS,
    WORK_MODES,
    Terminated,
    ignore_signals,
    peer_attr,
)
from peer_lib.workspace import native_attachment, outside_dirs, worktree_git_dir

__all__ = [
    "prompt_for_mode", "reply_contract",
    "build_command", "json_lines", "parse_codex", "parse_claude", "parse_agy",
    "PARSERS", "last_activity", "matches", "permission_signals",
    "stop_process_group", "pump", "feed", "run_process",
]


def prompt_for_mode(prompt, mode, cwd, attachments=()):
    if mode == "consult":
        lead = ("Peer consultation in {}: analyze and answer. Do not edit files or run "
                "mutating commands.").format(cwd)
    else:
        lead = ("Peer delegated work in {}: you may edit files only in this workspace. "
                "Use absolute paths under this directory with file-edit tools; shell commands "
                "may need separate permission. Report what changed and any blocked actions.").format(cwd)
    lead += "\nDo not invoke the Peer bridge from within this call."
    if attachments:
        lead += "\nAttached files; open them by absolute path:\n" + "\n".join("- " + a for a in attachments)
    return lead + "\n\n" + prompt


def reply_contract(words, mode, debate=False):
    """The reply format Peer asks for, so the caller can read a short answer first."""
    point = "what you changed and how you verified it" if mode in WORK_MODES else "the key points"
    lines = ["", "---",
             "Reply format requested by the Peer bridge (the caller reads only the first part):",
             "- Start with your answer in at most {} words: the conclusion first, then {}.".format(words, point)]
    if not debate:
        lines.append('- Complete all analysis or work that the workspace and instructions allow before '
                     'asking questions. Only if a remaining decision cannot be resolved from the code, '
                     'end that part with a line "QUESTIONS:" and one "- " line per question; otherwise '
                     'leave it out.')
    lines.append("- If more detail would help (full evidence, long lists, logs), put it after a line "
                 "containing only ---details--- . The caller opens it only when needed.")
    return "\n".join(lines)


def _has_flag(extra_args, *flags):
    return any(arg == flag or arg.startswith(flag + "=") for arg in extra_args for flag in flags)


def build_command(provider, mode, cwd, session, model, effort, allowed_tools, prompt,
                  attachments=(), fork=False, extra_args=()):
    extra_args = list(extra_args or ())
    if fork and provider == "agy":
        raise ValueError("agy CLI does not support session forking; use codex or claude")
    if provider == "codex":
        bypass = _has_flag(extra_args, "--dangerously-bypass-approvals-and-sandbox")
        command = ["codex", "exec"]
        if session:
            command.append("fork" if fork else "resume")
        command.extend(["--json", "--skip-git-repo-check"])
        if not bypass:
            if mode == "auto":
                command.extend([
                    "-c", 'approval_policy="on-request"',
                    "-c", 'approvals_reviewer="auto_review"',
                    "-c", 'sandbox_mode="workspace-write"',
                ])
            else:
                command.extend([
                    "-c", 'approval_policy="never"',
                    "-c", 'sandbox_mode="{}"'.format("read-only" if mode == "consult" else "workspace-write"),
                ])
        if model:
            command.extend(["-m", model])
        if effort:
            command.extend(["-c", 'model_reasoning_effort="{}"'.format(effort)])
        # The single-value form keeps a variadic --image from swallowing the "-" below.
        command.extend("--image=" + a for a in attachments if native_attachment(provider, a))
        command.extend(extra_args)
        command.extend([session, "-"] if session else ["-C", cwd, "-"])
        return command, prompt
    if provider == "claude":
        bypass = _has_flag(extra_args, "--dangerously-skip-permissions")
        perm_mode = "plan" if mode == "consult" else "auto" if mode == "auto" else "acceptEdits"
        command = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--permission-prompts", "none"]
        if not bypass and not _has_flag(extra_args, "--permission-mode"):
            command.extend(["--permission-mode", perm_mode])
        if mode == "consult" and not _has_flag(extra_args, "--tools"):
            command.extend(["--tools", "Read,Glob,Grep"])
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        if session:
            command.extend(["--resume", session])
            if fork:
                command.append("--fork-session")
        command.extend(extra_args)
        # Variadic flags last, one value per argument, so values may contain commas.
        extra_dirs = outside_dirs(attachments, cwd)
        if extra_dirs:
            command.append("--add-dir")
            command.extend(extra_dirs)
        if mode in WORK_MODES and allowed_tools:
            command.append("--allowedTools")
            command.extend(allowed_tools)
        return command, prompt
    bypass = _has_flag(extra_args, "--dangerously-skip-permissions")
    agy_mode = "plan" if mode == "consult" else "auto" if mode == "auto" else "accept-edits"
    command = ["agy", "--input-format", "stream-json", "--output-format", "stream-json"]
    if not _has_flag(extra_args, "--mode"):
        command.extend(["--mode", agy_mode])
    if not bypass and not _has_flag(extra_args, "--sandbox"):
        command.append("--sandbox")
    # Headless agy defaults to toolPermission=request-review (denying even sandboxed commands).
    if mode in WORK_MODES and not bypass and not _has_flag(extra_args, "--settings"):
        command.extend(["--settings", json.dumps({"toolPermission": "proceed-in-sandbox"})])
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if session:
        command.extend(["--conversation", session])
    agy_dirs = set(outside_dirs(attachments, cwd))
    wt_git = worktree_git_dir(cwd)
    if wt_git:
        agy_dirs.add(wt_git)
    for directory in sorted(agy_dirs):
        command.extend(["--add-dir", directory])
    command.extend(extra_args)
    event = {"event": "user", "message": {"content": prompt}}
    return command, json.dumps(event, ensure_ascii=False) + "\n"


def json_lines(stdout):
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def parse_codex(stdout):
    session, answer, error = None, None, None
    for event in json_lines(stdout):
        kind = event.get("type")
        if kind == "thread.started":
            session = event.get("thread_id") or session
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = item.get("text")
        elif kind in ("turn.failed", "error"):
            error = str(event.get("error") or event.get("message") or kind)
    return session, answer, error


def parse_claude(stdout):
    events = list(json_lines(stdout))
    if not events:
        try:
            parsed = json.loads(stdout)
        except ValueError as exc:
            return None, None, "invalid Claude JSON: {}".format(exc)
        if not isinstance(parsed, dict):
            return None, None, "Claude result is not an object"
        events = [parsed]
    session, answer, error = None, None, None
    for event in events:
        session = event.get("session_id") or session
        if event.get("type") in (None, "result") and ("result" in event or "is_error" in event or "errors" in event):
            answer = event.get("result")
            if event.get("is_error"):
                errs = event.get("errors")
                fallback = "; ".join(str(e) for e in errs if e) if isinstance(errs, list) else None
                error = str(answer or fallback or event.get("error") or "Claude reported an error")
            else:
                error = None
    return session, answer, error


def parse_agy(stdout):
    session, answer, error, status = None, None, None, None
    for event in json_lines(stdout):
        if event.get("event") == "init":
            session = event.get("conversation_id") or session
        elif event.get("event") == "result":
            result = event.get("result") or {}
            session = result.get("conversation_id") or session
            answer = result.get("response")
            status = result.get("status")
            reason = result.get("finished_reason")
            if status != "SUCCESS":
                error = str(result.get("error") or "Antigravity status: {}".format(status))
            elif reason:
                error = str(result.get("error") or "Antigravity turn ended early: {}".format(reason))
    if status is None:
        error = "Antigravity did not return a result event"
    return session, answer, error


PARSERS = {"codex": parse_codex, "claude": parse_claude, "agy": parse_agy}


def last_activity(provider, lines):
    """A short description of the child's latest streamed event, for heartbeats."""
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if provider == "codex":
            item = event.get("item") or {}
            detail = item.get("command") or ""
            return " ".join(x for x in (event.get("type"), item.get("type"), detail[:80]) if x)
        if provider == "claude":
            kind = event.get("type")
            if kind == "user":
                continue
            if kind == "assistant":
                content = (event.get("message") or {}).get("content") or []
                tools = [b.get("name") for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")]
                if tools:
                    return "assistant tool_use " + ", ".join(tools)
            return " ".join(x for x in (kind, event.get("subtype")) if x) or None
        update = event.get("step_update") or {}
        return " ".join(x for x in (event.get("event"), update.get("step_type")) if x)
    return None


def matches(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value)
    found = DENIAL.search(text)
    if not found:
        return None
    # Keep the matching phrase in view; command output can be long.
    start = max(0, found.start() - 200)
    return ("..." if start else "") + text[start:start + 500]


def permission_signals(provider, mode, stdout, stderr, response):
    """Return (notices, warnings).

    Notices come from structured denials, failed commands, and stderr; they mark the call
    blocked. Warnings are denial-like text in successful tool output or in the reply, such
    as a grep hit, and commands killed by a signal, which often means the sandbox stopped
    a browser or other local app; they do not change status.
    """
    notices, warnings = [], []
    for line in stderr.splitlines():
        if NONFATAL_STDERR.search(line):
            warnings.append(line.strip())
        elif DENIAL.search(line):
            notices.append(line.strip())
    if provider == "claude":
        for event in json_lines(stdout):
            for denial in event.get("permission_denials") or []:
                notices.append(str(denial))
    elif provider == "codex":
        for event in json_lines(stdout):
            item = event.get("item") or {}
            if event.get("type") in ("turn.failed", "error"):
                found = matches(event.get("error") or event.get("message"))
                if found:
                    notices.append(found)
            elif item.get("type") == "error":
                found = matches(item.get("message"))
                if found:
                    notices.append(found)
            elif item.get("type") == "command_execution" and event.get("type") == "item.completed":
                found = matches(item.get("aggregated_output"))
                code = item.get("exit_code")
                failed = item.get("status") in ("failed", "declined") or code not in (None, 0)
                if found:
                    (notices if failed else warnings).append(found)
                elif isinstance(code, int) and code >= 128:
                    warnings.append("command exited with code {} (killed by a signal; the sandbox may "
                                    "have stopped it): {}".format(code, str(item.get("command"))[:200]))
    else:
        for event in json_lines(stdout):
            if event.get("event") == "result":
                result = event.get("result") or {}
                for denial in result.get("denied_actions") or []:
                    notices.append("Antigravity denied action: {}".format(denial))
            update = event.get("step_update") or {}
            if update.get("step_type") in ("user_input", "agent_response"):
                continue
            tool_info = update.get("tool_info") or {}
            for err_candidate in (update.get("error"), tool_info.get("error")):
                found = matches(err_candidate)
                if found:
                    notices.append(found)
            for candidate in (update.get("message"), update.get("output"),
                              tool_info.get("output"), tool_info.get("result")):
                found = matches(candidate)
                if found:
                    warnings.append(found)
    if mode in WORK_MODES:
        found = matches(response)
        if found:
            warnings.append(found)
    notices = list(dict.fromkeys(notices))
    warnings = [w for w in dict.fromkeys(warnings) if w not in notices]
    return notices, warnings


def stop_process_group(proc):
    """Ask the child's process group to exit, then kill whatever remains."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (AttributeError, OSError):
        proc.kill()


def pump(stream, sink):
    for line in iter(stream.readline, ""):
        sink.append(line)
    stream.close()


def feed(stream, payload):
    try:
        stream.write(payload)
        stream.close()
    except (OSError, ValueError):
        pass


def run_process(command, payload, cwd, timeout, on_progress=None):
    env = os.environ.copy()
    env["PEER_BRIDGE_DEPTH"] = str(int(env.get("PEER_BRIDGE_DEPTH", "0")) + 1)
    start = time.monotonic()
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    out, err = [], []
    readers = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for thread in readers + [threading.Thread(target=feed, args=(proc.stdin, payload), daemon=True)]:
        thread.start()
    stopped = None
    try:
        while True:
            remaining = start + timeout - time.monotonic()
            if remaining <= 0:
                stopped = "timeout"
                break
            try:
                proc.wait(timeout=min(remaining, peer_attr("HEARTBEAT_SECONDS", HEARTBEAT_SECONDS)))
                break
            except subprocess.TimeoutExpired:
                if on_progress:
                    on_progress(time.monotonic() - start, list(out))
    except KeyboardInterrupt:
        stopped = "SIGINT"
    except Terminated as exc:
        stopped = str(exc)
    if stopped and stopped != "timeout":
        # Peer is exiting; a second signal must not skip stopping the child.
        ignore_signals()
    if stopped:
        stop_process_group(proc)
    for thread in readers:
        thread.join(timeout=STOP_GRACE_SECONDS)
    if any(thread.is_alive() for thread in readers):
        # A leftover grandchild is holding the output pipes open.
        stop_process_group(proc)
    return proc.returncode, "".join(out), "".join(err), time.monotonic() - start, stopped
