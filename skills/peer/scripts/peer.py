#!/usr/bin/env python3
"""Small, local bridge between the Codex, Claude Code, and Antigravity CLIs."""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


PROVIDERS = ("codex", "claude", "agy")
EFFORTS = {
    "codex": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "agy": {"low", "medium", "high"},
}
CONFIG_KEYS = {"timeout_seconds", "output_dir", "providers"}
PROVIDER_KEYS = {"model", "effort", "efforts", "aliases", "work_allowed_tools"}
DENIAL = re.compile(
    r"soft[- ]denied|permission denied|operation not permitted|read[- ]only (?:database|file system)|permission[^\n]*(?:denied|not granted|required)|"
    r"approval[^\n]*(?:denied|required|unavailable|cannot)|"
    r"(?:tool|command)[^\n]*(?:denied|not allowed|requires approval)",
    re.IGNORECASE,
)
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"}
DEFAULT_TIMEOUT = 600
STOP_GRACE_SECONDS = 5
HEARTBEAT_SECONDS = 15
RELAY_LIMIT = 12000
FILE_SCAN_LIMIT = 20000
COMPACT_LIST_LIMIT = 30
COMPACT_KEYS = ("status", "provider", "mode", "cwd", "session_id", "session_name", "replaced_session_id",
                "model", "model_alias", "effort", "response", "response_chars", "worktree_changed",
                "worktree_note", "changed_paths", "attachments", "output_file", "duration_seconds")


class Terminated(Exception):
    """Raised in place of the default exit when Peer receives SIGTERM or SIGHUP."""


def raise_terminated(signum, frame):
    raise Terminated(signal.Signals(signum).name)


def install_signal_handlers():
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), raise_terminated)


def ignore_signals():
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            except ValueError:
                pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def config_path(explicit):
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("PEER_CONFIG"):
        return Path(os.environ["PEER_CONFIG"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "peer" / "config.json"


def load_config(explicit):
    path = config_path(explicit)
    if not path.exists():
        if explicit or os.environ.get("PEER_CONFIG"):
            raise ValueError("configuration file does not exist: {}".format(path))
        return {}, path
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read configuration {}: {}".format(path, exc))
    if not isinstance(config, dict):
        raise ValueError("configuration must be a JSON object")
    for key in config:
        if key not in CONFIG_KEYS:
            raise ValueError("unknown configuration key: {}".format(key))
    output_dir = config.get("output_dir")
    if output_dir is not None and (not isinstance(output_dir, str) or not output_dir.strip()):
        raise ValueError("output_dir must be a nonempty string")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be a JSON object")
    for provider, provider_config in providers.items():
        if provider not in PROVIDERS:
            raise ValueError("unknown provider in configuration: {}".format(provider))
        if not isinstance(provider_config, dict):
            raise ValueError("providers.{} must be a JSON object".format(provider))
        for key in provider_config:
            if key not in PROVIDER_KEYS or (key == "work_allowed_tools" and provider != "claude"):
                raise ValueError("unknown configuration key: providers.{}.{}".format(provider, key))
        aliases = provider_config.get("aliases", {})
        if not isinstance(aliases, dict) or any(
                not isinstance(v, str) or not v.strip() for v in aliases.values()):
            raise ValueError("providers.{}.aliases must map names to model strings".format(provider))
    return config, path


def provider_settings(config, provider):
    return config.get("providers", {}).get(provider, {})


def effort_levels(provider_config, provider):
    levels = provider_config.get("efforts")
    if levels is None:
        return EFFORTS[provider]
    if not isinstance(levels, list) or not levels or any(not isinstance(x, str) or not x for x in levels):
        raise ValueError("providers.{}.efforts must be a nonempty list of strings".format(provider))
    return set(levels)


def preferences(config, provider, model=None, effort=None, timeout=None):
    provider_config = provider_settings(config, provider)
    chosen_model = model if model is not None else provider_config.get("model")
    chosen_effort = effort if effort is not None else provider_config.get("effort")
    chosen_timeout = timeout if timeout is not None else config.get("timeout_seconds", DEFAULT_TIMEOUT)
    levels = effort_levels(provider_config, provider)
    if chosen_model is not None and (not isinstance(chosen_model, str) or not chosen_model.strip()):
        raise ValueError("{} model must be a nonempty string".format(provider))
    alias = None
    aliases = {k.lower(): v for k, v in provider_config.get("aliases", {}).items()}
    if chosen_model is not None and chosen_model.lower() in aliases:
        alias, chosen_model = chosen_model, aliases[chosen_model.lower()]
    if chosen_effort is not None and chosen_effort not in levels:
        raise ValueError("{} effort must be one of: {}".format(provider, ", ".join(sorted(levels))))
    if isinstance(chosen_timeout, bool) or not isinstance(chosen_timeout, (int, float)) or chosen_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    allowed_tools = provider_config.get("work_allowed_tools", [])
    if not isinstance(allowed_tools, list) or any(not isinstance(x, str) or not x for x in allowed_tools):
        raise ValueError("providers.{}.work_allowed_tools must be a list of strings".format(provider))
    return {"model": chosen_model, "model_alias": alias, "effort": chosen_effort,
            "timeout": chosen_timeout, "allowed_tools": allowed_tools}


def state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state"))
    return base / "peer"


def output_dir(config):
    if config.get("output_dir"):
        return Path(config["output_dir"]).expanduser()
    return state_dir() / "runs"


def output_path(args, config):
    if args.no_save:
        return None
    if args.output:
        return Path(args.output).expanduser().resolve()
    label = args.to if args.command == "ask" else "{}-{}".format(args.a, args.b)
    name = "{}-{}-{}-{}.json".format(time.strftime("%Y%m%d-%H%M%S"), args.command, label, os.getpid())
    return output_dir(config) / name


def save_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(str(tmp), str(path))


class RunFile:
    """The saved record of one run: a running stub, heartbeats, then the final result."""

    def __init__(self, path, **fields):
        self.path = path
        self.record = dict(fields, status="running", pid=os.getpid(), started_at=now())
        self.update()

    def update(self, **fields):
        if self.path is None:
            return
        self.record.update(fields)
        self.record["updated_at"] = now()
        try:
            save_result(self.path, self.record)
        except OSError:
            pass

    def finish(self, result):
        """Save the final result; return an error message if it could not be saved."""
        if self.path is None:
            return "not saved (--no-save)"
        record = dict(result, prompt=self.record.get("prompt"),
                      started_at=self.record["started_at"], finished_at=now())
        try:
            save_result(self.path, record)
        except OSError as exc:
            return "cannot save result to {}: {}".format(self.path, exc)
        return None


def names_file():
    return state_dir() / "session-names.json"


def read_names():
    path = names_file()
    if not path.exists():
        return {}
    try:
        names = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read session names {}: {}".format(path, exc))
    return names if isinstance(names, dict) else {}


@contextlib.contextmanager
def locked_names():
    path = names_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(str(path) + ".lock", "a") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:
            pass
        names = read_names()
        yield names
        save_result(path, names)


def check_name(name):
    if not NAME_PATTERN.match(name):
        raise ValueError("session name must be 1-64 letters, digits, '.', '_' or '-': {}".format(name))


def lookup_name(provider, cwd, name):
    check_name(name)
    entry = read_names().get(provider, {}).get(cwd, {}).get(name)
    if not entry:
        raise ValueError("no {} session named '{}' for {}; start one with --new-session {}".format(
            provider, name, cwd, name))
    return entry["session_id"]


def bind_name(provider, cwd, name, session_id):
    """Point a name at a session; return the session it pointed to before, if different."""
    with locked_names() as names:
        slot = names.setdefault(provider, {}).setdefault(cwd, {})
        previous = (slot.get(name) or {}).get("session_id")
        slot[name] = {"session_id": session_id, "updated_at": now()}
    return previous if previous != session_id else None


def normalize_cwd(value):
    path = Path(value or os.getcwd()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("working directory does not exist: {}".format(path))
    return str(path)


def resolve_attachments(values):
    paths = []
    for value in values or []:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError("attachment not found: {}".format(path))
        paths.append(str(path))
    return paths


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


def outside_dirs(attachments, cwd):
    dirs = set()
    for attachment in attachments:
        parent = os.path.dirname(attachment)
        if parent != cwd and not parent.startswith(cwd + os.sep):
            dirs.add(parent)
    return sorted(dirs)


def native_attachment(provider, path):
    return provider == "codex" and Path(path).suffix.lower() in IMAGE_SUFFIXES


def build_command(provider, mode, cwd, session, model, effort, allowed_tools, prompt, attachments=()):
    if provider == "codex":
        command = ["codex", "exec"]
        if session:
            command.append("resume")
        command.extend([
            "--json", "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="{}"'.format("read-only" if mode == "consult" else "workspace-write"),
        ])
        if model:
            command.extend(["-m", model])
        if effort:
            command.extend(["-c", 'model_reasoning_effort="{}"'.format(effort)])
        # The single-value form keeps a variadic --image from swallowing the "-" below.
        command.extend("--image=" + a for a in attachments if native_attachment(provider, a))
        if session:
            command.extend([session, "-"])
        else:
            command.extend(["-C", cwd, "-"])
        return command, prompt
    if provider == "claude":
        command = ["claude", "-p", "--output-format", "json", "--permission-mode",
                   "plan" if mode == "consult" else "acceptEdits"]
        if mode == "consult":
            command.extend(["--tools", "Read,Glob,Grep"])
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])
        if session:
            command.extend(["--resume", session])
        # Variadic flags last, one value per argument, so values may contain commas.
        extra_dirs = outside_dirs(attachments, cwd)
        if extra_dirs:
            command.append("--add-dir")
            command.extend(extra_dirs)
        if mode == "work" and allowed_tools:
            command.append("--allowedTools")
            command.extend(allowed_tools)
        return command, prompt
    command = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
               "--mode", "plan" if mode == "consult" else "accept-edits", "--sandbox"]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    if session:
        command.extend(["--conversation", session])
    for directory in outside_dirs(attachments, cwd):
        command.extend(["--add-dir", directory])
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
    try:
        result = json.loads(stdout)
    except ValueError as exc:
        return None, None, "invalid Claude JSON: {}".format(exc)
    if not isinstance(result, dict):
        return None, None, "Claude result is not an object"
    error = str(result.get("result") or "Claude reported an error") if result.get("is_error") else None
    return result.get("session_id"), result.get("result"), error


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
            if status != "SUCCESS":
                error = str(result.get("error") or "Antigravity status: {}".format(status))
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
        update = event.get("step_update") or {}
        return " ".join(x for x in (event.get("event"), update.get("step_type")) if x)
    return None


def matches(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value)
    return text[:500] if DENIAL.search(text) else None


def permission_signals(provider, mode, stdout, stderr, response):
    """Return (notices, warnings).

    Notices come from structured denials, failed commands, and stderr; they mark the call
    blocked. Warnings are denial-like text in successful tool output or in the reply, such
    as a grep hit, and commands killed by a signal, which often means the sandbox stopped
    a browser or other local app; they do not change status.
    """
    notices = [line.strip() for line in stderr.splitlines() if DENIAL.search(line)]
    warnings = []
    if provider == "claude":
        try:
            data = json.loads(stdout)
        except ValueError:
            data = {}
        for denial in (data.get("permission_denials") or []) if isinstance(data, dict) else []:
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
            found = matches(update.get("error"))
            if found:
                notices.append(found)
            for candidate in (update.get("message"), update.get("output")):
                found = matches(candidate)
                if found:
                    warnings.append(found)
    if mode == "work":
        found = matches(response)
        if found:
            warnings.append(found)
    notices = list(dict.fromkeys(notices))
    warnings = [w for w in dict.fromkeys(warnings) if w not in notices]
    return notices, warnings


def git_output(cwd, *args):
    try:
        result = subprocess.run(
            ["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def content_token(path):
    try:
        if os.path.islink(path):
            return "link:" + os.readlink(path)
        if os.path.isdir(path):
            return "directory"
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def git_snapshot(cwd):
    """Content hashes of every path Git reports as dirty or untracked, or None outside Git."""
    raw = git_output(cwd, "status", "--porcelain", "-z", "--untracked-files=all")
    if raw is None:
        return None
    codes = {}
    entries = raw.split(b"\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code = entry[:2].decode("ascii", errors="replace")
        codes[os.fsdecode(entry[3:])] = code
        if code[0] in "RC":
            index += 1  # -z puts a rename's original path in the next entry
    files = {name: content_token(os.path.join(cwd, name)) for name in codes}
    return {"method": "git", "files": files, "codes": codes}


def file_snapshot(cwd):
    """Size and modification time of each file, for workspaces outside Git."""
    files = {}
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if len(files) >= FILE_SCAN_LIMIT:
                return {"method": "files", "files": None,
                        "note": "not a Git repository and more than {} files; changes not compared".format(
                            FILE_SCAN_LIMIT)}
            path = os.path.join(root, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            files[os.path.relpath(path, cwd)] = [info.st_size, info.st_mtime_ns]
    return {"method": "files", "files": files, "codes": {}}


def worktree_snapshot(cwd):
    return git_snapshot(cwd) or file_snapshot(cwd)


def worktree_report(before, after):
    if before.get("files") is None or after.get("files") is None:
        return {"worktree_changed": None,
                "worktree_note": before.get("note") or after.get("note")}
    if before["method"] != after["method"]:
        return {"worktree_changed": None,
                "worktree_note": "the workspace switched between Git and non-Git during the call"}
    old, new = before["files"], after["files"]
    changed = []
    for name in sorted(set(old) | set(new)):
        if old.get(name) != new.get(name):
            if before["method"] == "git":
                code = after["codes"].get(name, "clean")
            else:
                code = "A" if name not in old else "D" if name not in new else "M"
            changed.append("{} {}".format(code, name))
    report = {"worktree_changed": bool(changed), "changed_paths": changed}
    if before["method"] == "files":
        report["worktree_note"] = "not a Git repository; compared file sizes and modification times"
    return report


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
                proc.wait(timeout=min(remaining, HEARTBEAT_SECONDS))
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


def ask(provider, mode, cwd, prompt, config, session=None, model=None, effort=None,
        timeout=None, attachments=(), progress=None):
    if int(os.environ.get("PEER_BRIDGE_DEPTH", "0")) >= 1:
        raise ValueError("nested Peer calls are disabled; use the outer caller to coordinate turns")
    if not prompt.strip():
        raise ValueError("prompt is empty")
    prefs = preferences(config, provider, model, effort, timeout)
    if not shutil.which(provider):
        raise ValueError("{} CLI is not installed or not on PATH".format(provider))
    prepared_prompt = prompt_for_mode(prompt, mode, cwd, attachments)
    command, payload = build_command(provider, mode, cwd, session, prefs["model"], prefs["effort"],
                                     prefs["allowed_tools"], prepared_prompt, attachments)
    parser = PARSERS[provider]
    before = worktree_snapshot(cwd) if mode == "work" else None

    def on_progress(elapsed, lines):
        if progress:
            progress({"elapsed_seconds": round(elapsed), "output_events": len(lines),
                      "session_id": parser("".join(lines))[0] or session,
                      "last_activity": last_activity(provider, lines)})

    try:
        exit_code, stdout, stderr, duration, stopped = run_process(
            command, payload, cwd, prefs["timeout"], on_progress)
    except OSError as exc:
        raise ValueError("cannot launch {}: {}".format(provider, exc))
    parsed_session, response, parse_error = parser(stdout)
    notices, warnings = permission_signals(provider, mode, stdout, stderr, response)
    error = parse_error
    if stopped == "timeout":
        error = "timed out after {} seconds; the child was stopped".format(prefs["timeout"])
    elif stopped:
        error = "Peer received {}; the child was stopped".format(stopped)
    elif exit_code != 0 and not error:
        error = "{} exited with code {}".format(provider, exit_code)
    elif not (parsed_session or session) and not error:
        error = "{} returned no session ID; the conversation cannot be resumed".format(provider)
    elif response is None and not error and not notices:
        error = "{} returned no response".format(provider)
    if stopped == "timeout":
        status = "timeout"
    elif stopped:
        status = "interrupted"
    else:
        status = "blocked" if notices else "error" if error else "ok"
    if status == "blocked" and not (parsed_session or session):
        error = "{} was blocked before returning a session ID".format(provider)
    result = {
        "status": status,
        "provider": provider,
        "mode": mode,
        "cwd": cwd,
        "session_id": parsed_session or session,
        "model": prefs["model"],
        "model_alias": prefs["model_alias"],
        "effort": prefs["effort"],
        "response": response or "",
        "response_chars": len(response or ""),
        "error": error,
        "permission_notices": notices,
        "permission_warnings": warnings,
        "exit_code": exit_code,
        "duration_seconds": round(duration, 2),
    }
    if attachments:
        result["attachments"] = [{"path": a, "native": native_attachment(provider, a)} for a in attachments]
    if stderr.strip():
        result["diagnostics"] = stderr.strip()[-4000:]
    if mode == "work":
        result.update(worktree_report(before, worktree_snapshot(cwd)))
    return result


def compact_turn(result):
    """The fields a caller needs from a successful call; failures stay complete."""
    if result.get("status") != "ok":
        return result
    # worktree_changed stays even when null: the note beside it explains why.
    short = {key: result[key] for key in COMPACT_KEYS
             if result.get(key) is not None or (key == "worktree_changed" and key in result)}
    if result.get("permission_warnings"):
        short["permission_warnings"] = len(result["permission_warnings"])
    paths = short.get("changed_paths")
    if paths and len(paths) > COMPACT_LIST_LIMIT:
        short["changed_paths"] = paths[:COMPACT_LIST_LIMIT] + [
            "... {} more in output_file".format(len(paths) - COMPACT_LIST_LIMIT)]
    return short


def compact(result):
    if "transcript" not in result:
        return compact_turn(result)
    short = {key: value for key, value in result.items() if key != "transcript"}
    short["transcript"] = [
        {"provider": t["provider"], "response": t["response"]} if t.get("status") == "ok" else t
        for t in result["transcript"]]
    return short


def relay_text(response):
    if len(response) <= RELAY_LIMIT:
        return response
    return response[:RELAY_LIMIT] + "\n[truncated: first {} of {} characters shown]".format(
        RELAY_LIMIT, len(response))


def debate(args, cwd, prompt, config, run, attachments):
    if args.a == args.b:
        raise ValueError("debate requires two different providers")
    if not 1 <= args.rounds <= 10:
        raise ValueError("rounds must be between 1 and 10")
    name = args.session_name or args.new_session
    if name and (args.session_a or args.session_b):
        raise ValueError("use either session names or --session-a/--session-b, not both")
    sessions = {args.a: args.session_a, args.b: args.session_b}
    if args.session_name:
        sessions = {p: lookup_name(p, cwd, args.session_name) for p in (args.a, args.b)}
    elif args.new_session:
        check_name(args.new_session)
    side = {args.a: (args.model_a, args.effort_a), args.b: (args.model_b, args.effort_b)}
    transcript = []
    relay = prompt
    for _ in range(args.rounds):
        for provider in (args.a, args.b):
            message = prompt if not transcript else (
                "Original question:\n{}\n\n{} replied:\n{}\n\nRespond to their reasoning."
                .format(prompt, transcript[-1]["provider"], relay)
            )
            first_turn = all(t["provider"] != provider for t in transcript)
            result = ask(provider, "consult", cwd, message, config, session=sessions[provider],
                         model=side[provider][0], effort=side[provider][1], timeout=args.timeout,
                         attachments=attachments if first_turn else (),
                         progress=lambda info, p=provider: run.update(current=dict(info, provider=p)))
            sessions[provider] = result["session_id"]
            if name and result["session_id"]:
                bind_name(provider, cwd, name, result["session_id"])
            transcript.append(result)
            summary = {"sessions": sessions, "transcript": transcript}
            if name:
                summary["session_name"] = name
            if result["status"] != "ok":
                return dict(summary, status=result["status"], error=result.get("error"))
            run.update(current=None, **summary)
            relay = relay_text(result["response"])
    return dict(summary, status="ok")


def recorded_sessions(config, cwd, show_all):
    """Sessions seen in saved run files, newest first, with any names bound to them."""
    names = read_names()
    bound = {}
    for provider, by_cwd in names.items():
        for directory, entries in by_cwd.items():
            for name, entry in entries.items():
                bound.setdefault((provider, entry.get("session_id")), []).append(name)
    found = {}
    runs = output_dir(config)
    for path in sorted(runs.glob("*.json")) if runs.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            used = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        turns = record.get("transcript") or [record]
        for turn in turns:
            if not isinstance(turn, dict) or not turn.get("session_id"):
                continue
            directory = turn.get("cwd") or record.get("cwd")
            if not show_all and directory != cwd:
                continue
            key = (turn.get("provider"), turn["session_id"])
            entry = found.setdefault(key, {
                "provider": key[0], "session_id": key[1], "cwd": directory,
                "names": bound.get(key, []), "runs": 0,
                "first_prompt": (record.get("prompt") or "").strip().split("\n")[0][:100]})
            entry["runs"] += 1
            entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(used))
    return sorted(found.values(), key=lambda e: e["last_used"], reverse=True)[:20]


def model_listing(config):
    listing = {}
    for provider in PROVIDERS:
        settings = provider_settings(config, provider)
        entry = {"configured_model": settings.get("model"), "aliases": settings.get("aliases", {})}
        if provider == "agy" and shutil.which("agy"):
            try:
                result = subprocess.run(["agy", "models"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, timeout=60)
                entry["live"] = [line.split("\t")[0] for line in result.stdout.splitlines() if "\t" in line]
            except (OSError, subprocess.TimeoutExpired) as exc:
                entry["live_error"] = str(exc)
        elif provider == "agy":
            entry["live_error"] = "agy CLI is not installed or not on PATH"
        else:
            entry["check"] = ("{} has no command that lists models; ask the user, who can open /model "
                              "in an interactive session.").format("Codex" if provider == "codex" else "Claude Code")
        listing[provider] = entry
    return listing


def read_prompt(explicit):
    if explicit is not None:
        return explicit
    if sys.stdin.isatty():
        raise ValueError("pass --prompt or pipe a prompt on stdin")
    return sys.stdin.read()


def emit(result, run=None, full=False):
    save_error = run.finish(result) if run else None
    if run and not save_error:
        result["output_file"] = str(run.path)
    elif run and run.path is not None:
        result["output_error"] = save_error
    printed = result if full or not result.get("output_file") else compact(result)
    print(json.dumps(printed, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


def add_run_arguments(parser):
    parser.add_argument("--cwd")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--config")
    parser.add_argument("--prompt")
    parser.add_argument("--attach", action="append", metavar="FILE",
                        help="file to give the target; images go to Codex natively, others by path")
    parser.add_argument("--full", action="store_true", help="print every field, not the compact result")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output", help="save the JSON result to this file")
    group.add_argument("--no-save", action="store_true", help="print the full result without saving a file")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consult or delegate work to a local agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    ask_parser = sub.add_parser("ask", help="send one turn, optionally resuming a session")
    ask_parser.add_argument("--to", required=True, choices=PROVIDERS)
    ask_parser.add_argument("--mode", choices=("consult", "work"), default="consult")
    ask_parser.add_argument("--model")
    ask_parser.add_argument("--effort")
    resume = ask_parser.add_mutually_exclusive_group()
    resume.add_argument("--session", help="resume this native session ID")
    resume.add_argument("--session-name", help="resume the session saved under this name")
    resume.add_argument("--new-session", metavar="NAME", help="start a session and save it under NAME")
    add_run_arguments(ask_parser)

    debate_parser = sub.add_parser("debate", help="alternate two consult sessions for bounded rounds")
    debate_parser.add_argument("--a", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--b", required=True, choices=PROVIDERS)
    debate_parser.add_argument("--rounds", type=int, default=2)
    for side in ("a", "b"):
        debate_parser.add_argument("--session-" + side)
        debate_parser.add_argument("--model-" + side)
        debate_parser.add_argument("--effort-" + side)
    names = debate_parser.add_mutually_exclusive_group()
    names.add_argument("--session-name", help="resume both sessions saved under this name")
    names.add_argument("--new-session", metavar="NAME", help="start both sessions and save them under NAME")
    add_run_arguments(debate_parser)

    sessions_parser = sub.add_parser("sessions", help="list recorded sessions and their names")
    sessions_parser.add_argument("--cwd")
    sessions_parser.add_argument("--all", action="store_true", help="include every directory")
    sessions_parser.add_argument("--config")

    for name, text in (("doctor", "show CLI availability and configured defaults"),
                       ("models", "show configured models and aliases, and list Antigravity models live")):
        sub.add_parser(name, help=text).add_argument("--config")

    args = parser.parse_args(argv)
    run = None
    try:
        config, config_file = load_config(args.config)
        if args.command == "doctor":
            providers = {}
            for provider in PROVIDERS:
                prefs = preferences(config, provider)
                providers[provider] = {
                    "executable": shutil.which(provider), "model": prefs["model"],
                    "model_alias": prefs["model_alias"], "effort": prefs["effort"],
                    "efforts": sorted(effort_levels(provider_settings(config, provider), provider)),
                    "timeout_seconds": prefs["timeout"],
                    "work_allowed_tools": prefs["allowed_tools"] if provider == "claude" else None,
                }
            return emit({"status": "ok", "config_path": str(config_file),
                         "output_dir": str(output_dir(config)), "providers": providers,
                         "note": "Effective host and target permissions require live probes."})
        if args.command == "models":
            return emit({"status": "ok", "providers": model_listing(config)})
        cwd = normalize_cwd(args.cwd)
        if args.command == "sessions":
            return emit({"status": "ok", "cwd": None if args.all else cwd,
                         "sessions": recorded_sessions(config, cwd, args.all)})
        prompt = read_prompt(args.prompt)
        attachments = resolve_attachments(args.attach)
        providers = [args.to] if args.command == "ask" else [args.a, args.b]
        run = RunFile(output_path(args, config), command=args.command, providers=providers,
                      cwd=cwd, prompt=prompt)
        if args.command == "debate":
            return emit(debate(args, cwd, prompt, config, run, attachments), run, args.full)
        session = args.session
        if args.session_name:
            session = lookup_name(args.to, cwd, args.session_name)
        elif args.new_session:
            check_name(args.new_session)
        result = ask(args.to, args.mode, cwd, prompt, config, session=session, model=args.model,
                     effort=args.effort, timeout=args.timeout, attachments=attachments,
                     progress=lambda info: run.update(**info))
        name = args.session_name or args.new_session
        if name and result["session_id"]:
            result["session_name"] = name
            previous = bind_name(args.to, cwd, name, result["session_id"])
            if previous and args.new_session:
                result["replaced_session_id"] = previous
        return emit(result, run, args.full)
    except (ValueError, KeyboardInterrupt, Terminated) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "interrupted"
        return emit({"status": "error", "error": message or "interrupted"}, run)


if __name__ == "__main__":
    install_signal_handlers()
    sys.exit(main())
