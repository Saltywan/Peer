"""Session name registry, managed Git worktrees, attachments, and change detection."""

import contextlib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from peer_lib.config import save_result, state_dir
from peer_lib.constants import (
    FILE_SCAN_LIMIT,
    IMAGE_SUFFIXES,
    NAME_PATTERN,
    SKIP_DIRS,
    now,
    peer_attr,
)

__all__ = [
    "names_file", "read_names", "locked_names", "check_name", "tree_rank",
    "resolve_name", "resolve_target_scope", "bind_name", "session_settings",
    "verify_worktree", "prepare_worktree", "normalize_cwd", "resolve_attachments",
    "outside_dirs", "native_attachment", "git_output", "content_token",
    "git_snapshot", "committed_paths", "file_snapshot", "worktree_snapshot",
    "worktree_report",
]


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
        peer_attr("save_result", save_result)(path, names)


def check_name(name):
    if not NAME_PATTERN.match(name) or name.endswith(".") or ".." in name:
        raise ValueError("session name must be 1-64 letters, digits, '.', '_' or '-' (no '..' or trailing '.'): {}".format(name))


def tree_rank(directory, cwd):
    """How closely a session's workspace relates to cwd: same, ancestor, descendant, or None."""
    if directory == cwd:
        return (0, 0)
    if cwd.startswith(directory.rstrip(os.sep) + os.sep):
        return (1, cwd.count(os.sep) - directory.count(os.sep))
    if directory.startswith(cwd.rstrip(os.sep) + os.sep):
        return (2, directory.count(os.sep) - cwd.count(os.sep))
    return None


def resolve_name(name, cwd, provider=None):
    """Find the session NAME refers to from cwd: its own workspace first, then the nearest
    ancestor, then a unique descendant. Returns provider, workspace, session ID, and settings."""
    check_name(name)
    candidates, nearby = [], set()
    for owner, by_cwd in read_names().items():
        for directory, entries in by_cwd.items():
            rank = tree_rank(directory, cwd)
            if rank is not None:
                nearby.update("{} ({})".format(n, owner) for n in entries)
            for entry_name, entry in entries.items():
                entry_rank = rank
                if entry_rank is None and entry.get("worktree"):
                    entry_rank = tree_rank(entry["worktree"], cwd)
                    if entry_rank is not None:
                        nearby.add("{} ({})".format(entry_name, owner))
                if entry_rank is not None and entry_name == name and (provider is None or owner == provider):
                    candidates.append((entry_rank, owner, directory, entry))
    if not candidates:
        hint = "; names here: " + ", ".join(sorted(nearby)) if nearby else ""
        raise ValueError("no {}session named '{}' for {}{}; start one with ask --new-session {}".format(
            provider + " " if provider else "", name, cwd, hint, name))
    best = min(c[0] for c in candidates)
    top = [c for c in candidates if c[0] == best]
    if len({c[1] for c in top}) > 1:
        raise ValueError("'{}' names sessions with {}; pass --to PROVIDER".format(
            name, " and ".join(sorted({c[1] for c in top}))))
    if len(top) > 1:
        raise ValueError("'{}' names sessions in {}; pass --cwd for the one you mean".format(
            name, " and ".join(sorted(c[2] for c in top))))
    _, owner, directory, entry = top[0]
    if not os.path.isdir(directory):
        raise ValueError("session '{}' belongs to {}, which no longer exists".format(name, directory))
    return {"provider": owner, "cwd": directory, "session_id": entry["session_id"],
            "settings": {key: entry.get(key) for key in ("mode", "model", "effort", "words",
                                                         "worktree", "worktree_branch")}}


def resolve_target_scope(name, cwd, provider=None):
    """Find the best-ranked workspace directories and bound session IDs for NAME from cwd."""
    check_name(name)
    candidates = []
    for owner, by_cwd in read_names().items():
        if provider is not None and owner != provider:
            continue
        for directory, entries in by_cwd.items():
            if name not in entries:
                continue
            entry = entries[name]
            rank = tree_rank(directory, cwd)
            if rank is None and entry.get("worktree"):
                rank = tree_rank(entry["worktree"], cwd)
            if rank is not None:
                candidates.append((rank, owner, directory, entry))
    if not candidates:
        return None, set()
    best = min(c[0] for c in candidates)
    top = [c for c in candidates if c[0] == best]
    if len({c[2] for c in top}) > 1:
        raise ValueError("'{}' names sessions in {}; pass --cwd for the one you mean".format(
            name, " and ".join(sorted({c[2] for c in top}))))
    dirs = {c[2] for c in top} | {c[3]["worktree"] for c in top if c[3].get("worktree")}
    ids = {c[3]["session_id"] for c in top if c[3].get("session_id")}
    return dirs, ids


def bind_name(provider, cwd, name, session_id, settings=None):
    """Point a name at a session and remember the call's settings; return the session it
    pointed to before, if different."""
    with locked_names() as names:
        slot = names.setdefault(provider, {}).setdefault(cwd, {})
        previous = (slot.get(name) or {}).get("session_id")
        entry = {"session_id": session_id, "updated_at": now()}
        entry.update({key: value for key, value in (settings or {}).items() if value is not None})
        slot[name] = entry
    return previous if previous != session_id else None


def session_settings(result):
    return {"mode": result.get("mode"), "model": result.get("model"), "effort": result.get("effort"),
            "words": result.get("reply_words"), "worktree": result.get("worktree"),
            "worktree_branch": result.get("worktree_branch")}


def verify_worktree(origin_cwd, worktree_path, expected_branch=None):
    """Verify that worktree_path is an active Git worktree for origin_cwd on expected_branch."""
    if not os.path.isdir(worktree_path):
        raise ValueError("worktree {} no longer exists".format(worktree_path))
    root_raw = git_output(origin_cwd, "rev-parse", "--show-toplevel")
    wt_top_raw = git_output(worktree_path, "rev-parse", "--show-toplevel")
    if root_raw is None or wt_top_raw is None:
        raise ValueError("worktree {} is not a valid Git checkout".format(worktree_path))
    root = os.path.realpath(os.fsdecode(root_raw.rstrip(b"\n")))
    wt_root = os.path.realpath(os.fsdecode(wt_top_raw.rstrip(b"\n")))
    real_target = os.path.realpath(worktree_path)
    if os.path.commonpath([real_target, wt_root]) != wt_root:
        raise ValueError("worktree path {} escapes worktree root {}".format(worktree_path, wt_root))
    common_raw = git_output(root, "rev-parse", "--git-common-dir")
    wt_common_raw = git_output(wt_root, "rev-parse", "--git-common-dir")
    root_common = os.path.realpath(os.path.join(root, os.fsdecode(common_raw.rstrip(b"\n")))) if common_raw else None
    wt_common = os.path.realpath(os.path.join(wt_root, os.fsdecode(wt_common_raw.rstrip(b"\n")))) if wt_common_raw else None
    wt_branch_raw = git_output(wt_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    wt_branch = os.fsdecode(wt_branch_raw.rstrip(b"\n")) if wt_branch_raw else None
    if wt_common != root_common or (expected_branch and wt_branch != expected_branch):
        raise ValueError("worktree {} does not match repository {} on branch {}".format(
            worktree_path, root, expected_branch or wt_branch))
    return real_target


def prepare_worktree(cwd, name):
    """Create or reuse an isolated Git worktree for cwd under state_dir()/worktrees/."""
    check_name(name)
    root_raw = git_output(cwd, "rev-parse", "--show-toplevel")
    if root_raw is None:
        raise ValueError("--worktree requires a Git repository: {}".format(cwd))
    root = os.path.realpath(os.fsdecode(root_raw.rstrip(b"\n")))
    if git_output(root, "rev-parse", "--verify", "HEAD") is None:
        raise ValueError("--worktree requires a Git repository with at least one commit: {}".format(root))
    branch = "peer/" + name
    if subprocess.run(["git", "check-ref-format", "--branch", branch], cwd=root,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False).returncode != 0:
        raise ValueError("invalid Git branch name for worktree: {}".format(branch))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(root)).strip("-") or "repo"
    short_hash = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
    wt_root = state_dir() / "worktrees" / "{}-{}-{}".format(slug, short_hash, name)
    if (wt_root / ".git").exists():
        verify_worktree(root, str(wt_root), branch)
    else:
        wt_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        subprocess.run(["git", "worktree", "prune"], cwd=root,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
        has_branch = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch],
            cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False).returncode == 0
        cmd = (["git", "worktree", "add", str(wt_root), branch] if has_branch
               else ["git", "worktree", "add", "-b", branch, str(wt_root), "HEAD"])
        proc = subprocess.run(
            cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
        if proc.returncode != 0:
            raise ValueError("cannot create Git worktree {}: {}".format(
                wt_root, (proc.stderr or proc.stdout).strip() or "git worktree add failed"))
    rel = os.path.relpath(os.path.realpath(cwd), root)
    target = wt_root if rel == "." else wt_root / rel
    target.mkdir(parents=True, exist_ok=True)
    return verify_worktree(root, str(target), branch), branch


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


def outside_dirs(attachments, cwd):
    dirs = set()
    for attachment in attachments:
        parent = os.path.dirname(attachment)
        if parent != cwd and not parent.startswith(cwd + os.sep):
            dirs.add(parent)
    return sorted(dirs)


def native_attachment(provider, path):
    return provider == "codex" and Path(path).suffix.lower() in IMAGE_SUFFIXES


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
        return "{:o}:{}".format(os.stat(path).st_mode & 0o777, digest.hexdigest())
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def git_snapshot(cwd):
    """Dirty content and index state, with paths relative to the repository root."""
    root_raw = git_output(cwd, "rev-parse", "--show-toplevel")
    if root_raw is None:
        return None
    root = os.fsdecode(root_raw.rstrip(b"\n"))
    raw = git_output(root, "status", "--porcelain", "-z", "--untracked-files=all")
    staged = git_output(root, "ls-files", "--stage", "-z")
    if raw is None or staged is None:
        return {"method": "git", "files": None, "root": root,
                "note": "Git status or index could not be read; changes not compared"}
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
    index_entries = {}
    for entry in staged.split(b"\0"):
        if b"\t" in entry:
            metadata, name = entry.split(b"\t", 1)
            name = os.fsdecode(name)
            if name in codes:
                index_entries.setdefault(name, []).append(os.fsdecode(metadata))
    files = {name: content_token(os.path.join(root, name)) for name in codes}
    head = git_output(root, "rev-parse", "--verify", "HEAD")
    return {"method": "git", "root": root, "head": head.strip().decode("ascii") if head else None,
            "files": files, "codes": codes, "index": index_entries}


def committed_paths(before, after):
    """Paths changed between the two HEAD trees, including the first commit."""
    old, new = before["head"], after["head"]
    root = after["root"]
    if old and new:
        raw = git_output(root, "diff", "--no-renames", "--name-status", "-z", old, new, "--")
        if raw is None:
            return None
        entries = raw.rstrip(b"\0").split(b"\0") if raw else []
        return {os.fsdecode(entries[i + 1]): os.fsdecode(entries[i])
                for i in range(0, len(entries), 2)}
    raw = git_output(root, "ls-tree", "-r", "--name-only", "-z", new or old)
    if raw is None:
        return None
    return {os.fsdecode(name): "A" if new else "D" for name in raw.split(b"\0") if name}


def file_snapshot(cwd):
    """Size and modification time of each file, for workspaces outside Git."""
    limit = peer_attr("FILE_SCAN_LIMIT", FILE_SCAN_LIMIT)
    files = {}
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if len(files) >= limit:
                return {"method": "files", "files": None,
                        "note": "not a Git repository and more than {} files; changes not compared".format(limit)}
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
        return {"worktree_changed": None, "worktree_note": before.get("note") or after.get("note")}
    if before["method"] != after["method"]:
        return {"worktree_changed": None, "worktree_note": "the workspace switched between Git and non-Git during the call"}
    is_git = before["method"] == "git"
    if is_git and before["root"] != after["root"]:
        return {"worktree_changed": None, "worktree_note": "the Git repository root changed during the call"}
    old, new = before["files"], after["files"]
    changed = {}
    for name in sorted(set(old) | set(new)):
        if (old.get(name) != new.get(name) or
                (is_git and (before["codes"].get(name) != after["codes"].get(name) or
                             before["index"].get(name) != after["index"].get(name)))):
            if is_git:
                code = after["codes"].get(name, "clean")
            else:
                code = "A" if name not in old else "D" if name not in new else "M"
            changed[name] = code
    head_changed = is_git and before["head"] != after["head"]
    note = None
    if head_changed:
        committed = committed_paths(before, after)
        if committed is None:
            note = "HEAD changed, but committed paths could not be compared"
        else:
            for name, code in committed.items():
                changed[name] = after["codes"].get(name, code)
            note = "HEAD changed; compared committed paths as well as dirty content and index state"
    report = {"worktree_changed": bool(changed) or head_changed,
              "changed_paths": ["{} {}".format(changed[name], name) for name in sorted(changed)]}
    if note:
        report["worktree_note"] = note
    if not is_git:
        report["worktree_note"] = "not a Git repository; compared file sizes and modification times"
    return report
