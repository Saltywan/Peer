"""Shared constants, compiled patterns, and signal helpers for Peer."""

import argparse
import re
import signal
import sys
import time

PROVIDERS = ("codex", "claude", "agy")
MODES = ("consult", "work", "auto")
WORK_MODES = ("work", "auto")
EFFORTS = {
    "codex": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "claude": {"low", "medium", "high", "xhigh", "max"},
    "agy": {"low", "medium", "high", "xhigh", "max"},
}
CONFIG_KEYS = {"timeout_seconds", "output_dir", "reply_words", "providers"}
PROVIDER_KEYS = {"model", "effort", "efforts", "aliases", "work_allowed_tools"}
DENIAL = re.compile(
    r"soft[- ]denied|permission denied|operation not permitted|read[- ]only (?:database|file system)|permission[^\n]*(?:denied|not granted|required)|"
    r"approval[^\n]*(?:denied|required|unavailable|cannot)|"
    r"(?:tool|command)[^\n]*(?:denied|not allowed|requires approval)",
    re.IGNORECASE,
)
NONFATAL_STDERR = re.compile(
    r"^\s*warning:\s*(?:proceeding,\s*even though we could not create path aliases|"
    r"failed to clean up stale arg0 temp dirs):",
    re.IGNORECASE,
)
FENCE_LINE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache"}
DEFAULT_TIMEOUT = 600
DEFAULT_REPLY_WORDS = 400
WAIT_SECONDS = 540
WAIT_POLL_SECONDS = 2
STALE_SECONDS = 300
DIAGNOSTIC_TAIL = 1500
DETAILS_LINE = re.compile(r"^[ \t]*-{3,}[ \t]*details[ \t]*-{3,}[ \t]*$", re.IGNORECASE | re.MULTILINE)
QUESTIONS_LINE = re.compile(r"^[ \t#>*_]*questions[ \t*_]*:[ \t*_]*(.*?)[ \t*_]*$", re.IGNORECASE)
LIST_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.*\S)")
STOP_GRACE_SECONDS = 5
HEARTBEAT_SECONDS = 15
RELAY_LIMIT = 12000
FILE_SCAN_LIMIT = 20000
COMPACT_LIST_LIMIT = 30
COMPACT_KEYS = (
    "status", "provider", "mode", "cwd", "origin_cwd", "worktree", "worktree_branch",
    "session_id", "session_name", "forked_from", "replaced_session_id",
    "model", "model_alias", "effort", "response", "response_chars", "response_words",
    "questions", "details_chars", "worktree_changed", "worktree_note", "changed_paths",
    "attachments", "output_file", "duration_seconds",
)
EXIT_RUNNING = 3

__all__ = [
    "PROVIDERS", "MODES", "WORK_MODES", "EFFORTS", "CONFIG_KEYS", "PROVIDER_KEYS",
    "DENIAL", "NONFATAL_STDERR", "FENCE_LINE", "NAME_PATTERN", "IMAGE_SUFFIXES",
    "SKIP_DIRS", "DEFAULT_TIMEOUT", "DEFAULT_REPLY_WORDS", "WAIT_SECONDS",
    "WAIT_POLL_SECONDS", "STALE_SECONDS", "DIAGNOSTIC_TAIL", "DETAILS_LINE",
    "QUESTIONS_LINE", "LIST_ITEM", "STOP_GRACE_SECONDS", "HEARTBEAT_SECONDS",
    "RELAY_LIMIT", "FILE_SCAN_LIMIT", "COMPACT_LIST_LIMIT", "COMPACT_KEYS",
    "EXIT_RUNNING", "Terminated", "JsonArgumentParser", "raise_terminated",
    "install_signal_handlers", "ignore_signals", "now", "register_peer", "peer_attr",
]

_PEER_GLOBALS = None


class Terminated(Exception):
    """Raised in place of the default exit when Peer receives SIGTERM or SIGHUP."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


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


def register_peer(namespace):
    """Record the top-level peer module globals so patched attributes resolve across modules."""
    global _PEER_GLOBALS
    _PEER_GLOBALS = namespace


def peer_attr(name, default):
    if _PEER_GLOBALS is not None and name in _PEER_GLOBALS:
        return _PEER_GLOBALS[name]
    mod = sys.modules.get("peer")
    return getattr(mod, name, default) if mod is not None else default
