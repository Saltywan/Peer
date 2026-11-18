"""Saved run records, session history indexing, and process/run waiting."""

import datetime
import json
import os
import time
from pathlib import Path

from peer_lib.config import output_dir, save_result
from peer_lib.constants import STALE_SECONDS, WAIT_POLL_SECONDS, Terminated, now, peer_attr
from peer_lib.workspace import (
    read_names,
    resolve_target_scope,
    tree_rank,
)

__all__ = [
    "output_path", "save_result", "RunFile", "load_runs", "record_cwd",
    "record_in_tree", "record_providers", "record_session_ids",
    "recorded_sessions", "find_runs", "pid_alive", "seconds_since",
    "run_state", "wait",
]


def output_path(args, config, label):
    if args.no_save:
        return None
    name = "{}-{}-{}-{}-{}.json".format(
        time.strftime("%Y%m%d-%H%M%S"), args.command, label, os.getpid(), time.time_ns())
    if args.output:
        path = Path(args.output).expanduser().resolve()
        # Link it from the run directory so names, show, wait, and sessions still find it.
        try:
            output_dir(config).mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.parent != output_dir(config).resolve():
                os.symlink(str(path), str(output_dir(config) / name))
        except OSError:
            pass
        return path
    return output_dir(config) / name


class RunFile:
    """The saved record of one run: a running stub, heartbeats, then the final result."""

    def __init__(self, path, **fields):
        self.path = path
        self.record = dict(fields, status="running", pid=os.getpid(), started_at=now())
        self.update()

    def update(self, **fields):
        self.record.update(fields)
        self.record["updated_at"] = now()
        if self.path is None:
            return
        try:
            peer_attr("save_result", save_result)(self.path, self.record)
        except OSError:
            pass

    def finish(self, result):
        """Save the final result; return an error message if it could not be saved."""
        if self.path is None:
            return "not saved (--no-save)"
        kept = {key: self.record[key] for key in ("command", "providers", "cwd", "origin_cwd", "session_name")
                if self.record.get(key) is not None}
        record = dict(kept, **result)
        record.update(prompt=self.record.get("prompt"), started_at=self.record["started_at"], finished_at=now())
        try:
            peer_attr("save_result", save_result)(self.path, record)
        except OSError as exc:
            return "cannot save result to {}: {}".format(self.path, exc)
        return None


def load_runs(config):
    """Every saved run record, oldest first, as (modified_ns, path, record)."""
    runs = output_dir(config)
    records = []
    for path in runs.glob("*.json") if runs.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            used = path.stat().st_mtime_ns
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append((used, path, record))
    return sorted(records, key=lambda r: r[0])


def record_cwd(record):
    turns = record.get("transcript") or []
    return (record.get("origin_cwd") or record.get("cwd") or
            next((t.get("cwd") for t in turns if isinstance(t, dict) and t.get("cwd")), None))


def record_in_tree(record, cwd):
    turns = record.get("transcript") or []
    dirs = [record.get("origin_cwd"), record.get("cwd"), record.get("worktree")]
    dirs += [t.get("cwd") for t in turns if isinstance(t, dict)]
    return any(d and tree_rank(d, cwd) is not None for d in dirs)


def record_providers(record):
    found = list(record.get("providers") or [])
    found += [record["provider"]] if record.get("provider") else []
    found += list(record.get("sessions") or {})
    return set(found)


def record_session_ids(record):
    ids = {record.get("session_id")} | set((record.get("sessions") or {}).values())
    ids |= {t.get("session_id") for t in record.get("transcript") or [] if isinstance(t, dict)}
    return {i for i in ids if i}


def recorded_sessions(config, cwd, show_all):
    """Sessions seen in saved run files, newest first, with any names and settings bound to them."""
    names = read_names()
    bound = {}
    for provider, by_cwd in names.items():
        for directory, entries in by_cwd.items():
            for name, entry in entries.items():
                bound.setdefault((provider, entry.get("session_id"), directory), []).append((name, entry))
                if entry.get("worktree"):
                    bound.setdefault((provider, entry.get("session_id"), entry["worktree"]), []).append((name, entry))
    found = {}
    for used, _, record in load_runs(config):
        turns = record.get("transcript") or [record]
        for turn in turns:
            if not isinstance(turn, dict) or not turn.get("session_id"):
                continue
            directory = record.get("origin_cwd") or turn.get("cwd") or record_cwd(record)
            if not show_all and not record_in_tree(record, cwd):
                continue
            key = (turn.get("provider"), turn["session_id"], directory)
            named = bound.get(key) or bound.get((turn.get("provider"), turn["session_id"], turn.get("cwd")), [])
            entry = found.setdefault(key, {
                "provider": key[0], "session_id": key[1], "cwd": directory,
                "names": [n for n, _ in named], "runs": 0,
                "first_prompt": (record.get("prompt") or "").strip().split("\n")[0][:100]})
            for _, settings in named[:1]:
                entry.update({k: settings.get(k) for k in ("mode", "model", "effort", "worktree", "worktree_branch")
                              if settings.get(k)})
            entry["runs"] += 1
            entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(used / 1e9))
            entry["last_used_ns"] = used
    newest = sorted(found.values(), key=lambda e: e["last_used_ns"], reverse=True)[:20]
    return [{key: value for key, value in entry.items() if key != "last_used_ns"} for entry in newest]


def _read_run_file(target):
    path = Path(target).expanduser().resolve()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read run file {}: {}".format(path, exc))
    return [(path.stat().st_mtime_ns, path, record)]


def find_runs(config, cwd, target=None, provider=None):
    """Saved runs for a run file, a session name, or (with no target) this workspace tree."""
    if target and os.sep in target:
        return _read_run_file(target)
    runs = [r for r in load_runs(config)
            if record_in_tree(r[2], cwd) and (provider is None or provider in record_providers(r[2]))]
    if not target:
        return runs
    if target.endswith(".json") and Path(target).expanduser().is_file():
        return _read_run_file(target)
    try:
        scope_dirs, ids = resolve_target_scope(target, cwd, provider)
    except ValueError:
        if target.endswith(".json"):
            return _read_run_file(target)
        raise
    if scope_dirs is not None:
        return [r for r in runs
                if ({r[2].get("origin_cwd"), r[2].get("cwd"), r[2].get("worktree"), record_cwd(r[2])} & scope_dirs)
                and (record_session_ids(r[2]) & ids or r[2].get("session_name") == target)]
    named = [r for r in runs if r[2].get("session_name") == target]
    if not named:
        if target.endswith(".json"):
            return _read_run_file(target)
        return []
    ranked = [(tree_rank(record_cwd(r[2]), cwd) or (3, 0), r) for r in named]
    best = min(item[0] for item in ranked)
    return [item[1] for item in ranked if item[0] == best]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, TypeError):
        return True
    return True


def seconds_since(stamp):
    try:
        then = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return (datetime.datetime.now(then.tzinfo) - then).total_seconds()


def run_state(record):
    """A record's status, with abandoned running records reported as stale."""
    if record.get("status") != "running":
        return record.get("status") or "error"
    quiet = seconds_since(record.get("updated_at"))
    if not pid_alive(record.get("pid")) or (quiet is not None and quiet > STALE_SECONDS):
        return "stale"
    return "running"


def wait(args, config, cwd):
    # A run started a moment ago may not have written its file yet.
    appear_by = time.monotonic() + min(args.timeout, 10)
    while True:
        try:
            runs = find_runs(config, cwd, args.target, args.to)
        except ValueError:
            if time.monotonic() >= appear_by:
                raise
            runs = None
        if runs or not args.target or time.monotonic() >= appear_by:
            break
        time.sleep(0.25)
    runs = runs or []
    active = [r for r in runs if run_state(r[2]) == "running"]
    if not args.target and len(active) > 1:
        raise ValueError("{} runs are in progress here; pass a session name or run file:\n{}".format(
            len(active), "\n".join(str(r[1]) for r in active)))
    chosen = (active or runs)[-1:] if not args.target else runs[-1:]
    if not chosen:
        raise ValueError("no saved runs for {}".format(args.target or cwd))
    path = chosen[0][1]
    deadline = time.monotonic() + args.timeout
    record = chosen[0][2]
    try:
        while run_state(record) == "running" and time.monotonic() < deadline:
            time.sleep(min(WAIT_POLL_SECONDS, max(0, deadline - time.monotonic())))
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass  # The file is replaced atomically; retry on the next poll.
    except (KeyboardInterrupt, Terminated):
        pass  # Stop waiting; the run itself continues.
    return path, record
