"""JSON compaction and human-readable text rendering for Peer output."""

import json
import os

from peer_lib.constants import (
    COMPACT_KEYS,
    COMPACT_LIST_LIMIT,
    DETAILS_LINE,
    DIAGNOSTIC_TAIL,
    FENCE_LINE,
    LIST_ITEM,
    QUESTIONS_LINE,
    RELAY_LIMIT,
    peer_attr,
)
from peer_lib.storage import (
    find_runs,
    record_providers,
    run_state,
    seconds_since,
)

__all__ = [
    "split_reply", "full_reply", "compact_turn", "compact", "relay_text", "one_line", "duration_text",
    "model_text", "session_text", "render_turn", "render_debate",
    "render_running", "render", "render_details", "render_history",
    "render_sessions", "show", "emit",
]


def _toggle_fence(fence, line):
    m = FENCE_LINE.match(line)
    if not m:
        return fence, False
    marker = m.group(1)
    if fence is None:
        return (marker[0], len(marker)), True
    if marker[0] == fence[0] and len(marker) >= fence[1] and not line[m.end():].strip():
        return None, True
    return fence, True


def split_reply(text, contract):
    """Split a reply into the answer, details after the marker, and questions for the caller."""
    text = text or ""
    if not contract:
        return text, "", []
    raw_lines = text.splitlines()
    fence, split_at = None, None
    for index, line in enumerate(raw_lines):
        fence, toggled = _toggle_fence(fence, line)
        if not toggled and fence is None and DETAILS_LINE.match(line):
            split_at = index
            break
    if split_at is not None:
        answer = "\n".join(raw_lines[:split_at]).rstrip()
        details = "\n".join(raw_lines[split_at + 1:]).strip()
    else:
        answer, details = text.strip(), ""
    if not answer:
        answer, details = details, ""
    questions, lines, fence, q_index = [], answer.splitlines(), None, None
    for index, line in enumerate(lines):
        fence, toggled = _toggle_fence(fence, line)
        if not toggled and fence is None and QUESTIONS_LINE.match(line):
            q_index = index
    if q_index is not None:
        inline = QUESTIONS_LINE.match(lines[q_index]).group(1).strip()
        if inline and inline.lower().rstrip(".") not in ("none", "n/a", "no"):
            questions.append(inline)
        for follow in lines[q_index + 1:]:
            item = LIST_ITEM.match(follow)
            if item:
                questions.append(item.group(1))
            elif follow.strip():
                questions = []
                break
    return answer, details, questions


def full_reply(result):
    details = result.get("details")
    return result.get("response", "") + ("\n\n" + details if details else "")


def compact_turn(result):
    """The fields a caller needs from a successful call; failures stay complete."""
    if result.get("status") != "ok":
        return result
    # worktree_changed stays even when null: the note beside it explains why.
    short = {key: result[key] for key in COMPACT_KEYS
             if result.get(key) is not None or (key == "worktree_changed" and key in result)}
    if not short.get("questions"):
        short.pop("questions", None)
    if result.get("permission_warnings"):
        short["permission_warnings"] = len(result["permission_warnings"])
    paths = short.get("changed_paths")
    if paths and len(paths) > COMPACT_LIST_LIMIT:
        short["changed_paths"] = paths[:COMPACT_LIST_LIMIT] + [
            "... {} more in output_file".format(len(paths) - COMPACT_LIST_LIMIT)]
    return short


def compact(result):
    if result.get("status") != "ok":
        return result
    if "transcript" not in result:
        return compact_turn(result)
    short = {key: value for key, value in result.items() if key != "transcript"}
    short["transcript"] = [
        dict({"provider": t["provider"], "response": t["response"]},
             **({"details_chars": t["details_chars"]} if t.get("details_chars") else {}))
        if t.get("status") == "ok" else t
        for t in result["transcript"]]
    return short


def relay_text(response):
    limit = peer_attr("RELAY_LIMIT", RELAY_LIMIT)
    if len(response) <= limit:
        return response
    return response[:limit] + "\n[truncated: first {} of {} characters shown]".format(
        limit, len(response))


def one_line(text, limit=300):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def duration_text(seconds):
    if seconds is None:
        return None
    seconds = int(round(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "{}h {}m".format(hours, minutes)
    return "{}m {}s".format(minutes, seconds) if minutes else "{}s".format(seconds)


def model_text(result):
    model = result.get("model") or "default model"
    if result.get("model_alias"):
        model += " ({})".format(result["model_alias"])
    return model + ("/" + result["effort"] if result.get("effort") else "")


def session_text(result):
    name, sid = result.get("session_name"), result.get("session_id")
    if name and sid:
        return "session {} {}".format(name, sid)
    if name or sid:
        return "session " + (name or sid)
    return None


def render_turn(result, full, path=None):
    """Text view of one call: a header, lines that need attention, then the answer."""
    status = result.get("status", "error")
    parts = ["peer: " + status]
    if result.get("provider"):
        parts.append("{} {}".format(result["provider"], model_text(result)))
    parts += [x for x in (result.get("mode"), session_text(result),
                          duration_text(result.get("duration_seconds"))) if x]
    lines = [" | ".join(parts)]
    if status != "ok" and result.get("error"):
        lines.append("error: " + one_line(result["error"], 1000))
    notices = result.get("permission_notices") or []
    shown = notices if full else notices[:5]
    lines += ["blocked by: " + one_line(n) for n in shown]
    if len(notices) > len(shown):
        lines.append("blocked by: {} more in the saved file".format(len(notices) - len(shown)))
    if result.get("questions"):
        lines.append("questions: {} (at the end of the answer)".format(len(result["questions"])))
    if "worktree_changed" in result:
        changed, paths = result["worktree_changed"], result.get("changed_paths") or []
        note = result.get("worktree_note")
        if changed is None:
            lines.append("changed: unknown ({})".format(note or "not compared"))
        elif not changed:
            lines.append("changed: nothing")
        else:
            lines.append("changed: {} path{}".format(len(paths), "" if len(paths) == 1 else "s"))
            listed = paths if full else paths[:COMPACT_LIST_LIMIT]
            lines += ["  " + p.strip() for p in listed]
            if len(paths) > len(listed):
                lines.append("  ... {} more in the saved file".format(len(paths) - len(listed)))
        if note and changed is not None:
            lines.append("note: " + note)
    warnings = result.get("permission_warnings") or []
    if isinstance(warnings, list) and full:
        lines += ["warning: " + one_line(w) for w in warnings]
    elif warnings:
        count = warnings if isinstance(warnings, int) else len(warnings)
        lines.append("warnings: {} in the saved file (denial-like text or signal-killed commands)".format(count))
    limit, used = result.get("reply_words"), result.get("response_words")
    if limit and used and used > limit * 1.25:
        lines.append("answer: {} words, over the {} asked for".format(used, limit))
    details = result.get("details") or ""
    if result.get("details_chars") and not full:
        target = result.get("session_name") or (str(path) if path else "RUN_FILE")
        lines.append("details: {:,} characters not shown (show {} --details)".format(result["details_chars"], target))
    if result.get("attachments"):
        lines.append("attached: " + ", ".join(
            "{}{}".format(a["path"], " (native)" if a.get("native") else "") for a in result["attachments"]))
    if result.get("worktree"):
        branch = " (branch {})".format(result["worktree_branch"]) if result.get("worktree_branch") else ""
        lines.append("worktree: {}{}".format(result["worktree"], branch))
    if result.get("extra_args"):
        lines.append("extra args: " + " ".join(result["extra_args"]))
    if result.get("forked_from"):
        lines.append("forked from: {}".format(result["forked_from"]))
    if result.get("replaced_session_id"):
        lines.append("replaced: the name pointed to {} before".format(result["replaced_session_id"]))
    diagnostics = result.get("diagnostics") or ""
    if status != "ok" and diagnostics:
        tail = diagnostics if full else diagnostics[-DIAGNOSTIC_TAIL:]
        # Lines already shown as "blocked by" would only repeat themselves.
        extra = [line for line in tail.splitlines() if line.strip() and line.strip() not in notices]
        if extra:
            lines.append("diagnostics{}:".format("" if tail == diagnostics else " (last {} characters)".format(len(tail))))
            lines += ["  " + line for line in extra]
    # A named session is found with "show NAME"; others need the file path.
    if path and (full or status != "ok" or not result.get("session_name")):
        lines.append("file: {}".format(path))
    elif result.get("output_error"):
        lines.append("file: not saved ({})".format(result["output_error"]))
    if result.get("response") or status == "ok":
        lines.append("---")
        lines.append(result.get("response") or "(no answer)")
    if full and details:
        lines += ["---details---", details]
    return "\n".join(lines)


def render_debate(result, full, path=None):
    turns = result.get("transcript") or []
    sessions = result.get("sessions") or {}
    sides = list(sessions) or sorted(record_providers(result))
    total = sum(t.get("duration_seconds") or 0 for t in turns if isinstance(t, dict))
    parts = ["peer: " + result.get("status", "error"), "debate " + " vs ".join(sides),
             "{} turn{}".format(len(turns), "" if len(turns) == 1 else "s")]
    if result.get("session_name"):
        parts.append("name " + result["session_name"])
    parts.append(duration_text(total))
    lines = [" | ".join(parts)]
    if result.get("status") != "ok" and result.get("error"):
        lines.append("error: " + one_line(result["error"], 1000))
    lines.append("sessions: " + ", ".join("{} {}".format(p, sid or "(none)") for p, sid in sessions.items()))
    if path:
        lines.append("file: {}".format(path))
    elif result.get("output_error"):
        lines.append("file: not saved ({})".format(result["output_error"]))
    for number, turn in enumerate(turns, 1):
        head = "=== {} · {} {}".format(number, turn.get("provider"), model_text(turn))
        if turn.get("status") != "ok":
            head += " · " + str(turn.get("status"))
        lines.append(head + " ===")
        if turn.get("status") != "ok":
            lines.append("error: " + one_line(turn.get("error") or "", 1000))
        lines.append(turn.get("response") or "(no answer)")
        if turn.get("details") and full:
            lines += ["---details---", turn["details"]]
        elif turn.get("details_chars"):
            lines.append("(details: {:,} characters not shown)".format(turn["details_chars"]))
    return "\n".join(lines)


def render_running(record, path, state):
    current = record.get("current") or record
    provider = current.get("provider") or ", ".join(record.get("providers") or [])
    parts = ["peer: " + state, provider]
    session = session_text({"session_name": record.get("session_name"), "session_id": current.get("session_id")})
    if session:
        parts.append(session)
    elapsed = current.get("elapsed_seconds")
    if elapsed is None:
        elapsed = seconds_since(record.get("started_at"))
    parts.append("{} elapsed".format(duration_text(elapsed)))
    lines = [" | ".join(x for x in parts if x)]
    if current.get("last_activity"):
        lines.append("last activity: " + one_line(current["last_activity"], 200))
    if state == "stale":
        lines.append("error: Peer process {} stopped updating this run (last update {}); it was killed "
                     "without finishing".format(record.get("pid"), record.get("updated_at")))
    if record.get("transcript"):
        lines.append("finished turns: {}".format(len(record["transcript"])))
    lines.append("file: {}".format(path))
    return "\n".join(lines)


def render(result, full=False, path=None):
    state = run_state(result)
    if state in ("running", "stale"):
        return render_running(result, path, state)
    if "transcript" in result:
        return render_debate(result, full, path)
    return render_turn(result, full, path)


def render_details(record, path):
    turns = record.get("transcript") or [record]
    when = (record.get("finished_at") or record.get("started_at") or "")[:16].replace("T", " ")
    blocks = ["from the run at {} · you: \"{}\"".format(when, one_line(record.get("prompt") or "", 100))]
    for number, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        label = "{} {}".format(turn.get("provider"), model_text(turn))
        if len(turns) > 1:
            label = "{} · {}".format(number, label)
        details = turn.get("details") or ""
        blocks.append("=== details · {} · {:,} characters ===\n{}".format(
            label, len(details), details or "(none: the reply had no details part)"))
    return "file: {}\n{}".format(path, "\n".join(blocks))


def render_history(runs, label):
    lines = ["peer: last {} run{} for {}".format(len(runs), "" if len(runs) == 1 else "s", label)]
    for _, path, record in runs:
        asked = one_line(record.get("prompt") or "", 100)
        when = (record.get("finished_at") or record.get("started_at") or "")[:16].replace("T", " ")
        lines.append("=== {} · {} · you: \"{}\" ===".format(when, run_state(record), asked))
        if "transcript" in record:
            for turn in record["transcript"]:
                lines.append("[{}] {}".format(turn.get("provider"), turn.get("response") or "(no answer)"))
        else:
            lines.append(record.get("response") or record.get("error") or "(no answer)")
        if any(t.get("details_chars") for t in (record.get("transcript") or [record]) if isinstance(t, dict)):
            lines.append("(details not shown: show {} --details)".format(path))
    return "\n".join(lines)


def render_sessions(result, cwd):
    sessions = result["sessions"]
    scope = "in every workspace" if result.get("cwd") is None else "for {}".format(cwd)
    lines = ["peer: {} session{} {} (newest first)".format(len(sessions), "" if len(sessions) == 1 else "s", scope)]
    for entry in sessions:
        bits = [", ".join(entry["names"]) or "(unnamed)", entry["provider"]]
        settings = " ".join(x for x in (entry.get("mode"), entry.get("model"), entry.get("effort")) if x)
        if settings:
            bits.append(settings)
        bits.append("{} run{}".format(entry["runs"], "" if entry["runs"] == 1 else "s"))
        bits.append("last " + entry["last_used"][:16].replace("T", " "))
        directory = entry.get("cwd") or ""
        if result.get("cwd") is not None and directory != cwd:
            bits.append(os.path.relpath(directory, cwd))
        elif result.get("cwd") is None:
            bits.append(directory)
        lines.append("- " + " · ".join(bits))
        lines.append("  id {} · first: \"{}\"".format(entry["session_id"], entry["first_prompt"]))
    if not sessions:
        lines.append("(none; start one with ask --new-session NAME)")
    return "\n".join(lines)


def show(args, config, cwd):
    runs = find_runs(config, cwd, args.target, args.to)
    if not runs:
        raise ValueError("no saved runs for {}".format(args.target or cwd))
    if args.last:
        selected = runs[-args.last:]
        if args.json:
            return [dict(r[2], output_file=str(r[1])) for r in selected], None
        return None, render_history(selected, args.target or cwd)
    _, path, record = runs[-1]
    if args.details:
        with_details = [r for r in runs if any(
            isinstance(t, dict) and t.get("details") for t in (r[2].get("transcript") or [r[2]]))]
        if with_details:
            _, path, record = with_details[-1]
        if not args.json:
            return None, render_details(record, path)
    if args.json:
        return dict(record, output_file=str(path), status=run_state(record)), None
    return None, render(record, args.full, path)


def emit(result, run=None, fmt="text", full=False):
    save_error = run.finish(result) if run else None
    path = None
    if run and not save_error:
        path = run.path
        result["output_file"] = str(path)
    elif run and run.path is not None:
        result["output_error"] = save_error
    # Without a saved file there is nowhere else to read the rest, so print everything.
    complete = full or path is None
    if fmt == "json":
        print(json.dumps(result if complete else compact(result), ensure_ascii=False))
    else:
        print(render(result, complete, path))
    return 0 if result.get("status") == "ok" else 2
