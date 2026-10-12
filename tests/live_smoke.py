"""Opt-in live provider checks. Not included in unittest discovery; may incur model usage."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "peer" / "scripts" / "peer.py"
spec = importlib.util.spec_from_file_location("peer_live", SCRIPT)
peer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(peer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, choices=peer.PROVIDERS)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--config")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--output-dir", help="parent directory for retained fixtures and JSON evidence")
    args = parser.parse_args()
    peer.validate_timeout(args.timeout)
    artifacts = Path(tempfile.mkdtemp(prefix="peer-live-", dir=args.output_dir)).resolve()
    workspace = artifacts / "workspace"
    workspace.mkdir()
    fixture = workspace / "fixture.txt"
    fixture.write_text("peer consultation fixture\n", encoding="utf-8")
    for command in (["init", "-q"], ["add", "."], ["commit", "-qm", "Smoke fixture"]):
        subprocess.run(["git", "-c", "user.name=Peer Smoke", "-c", "user.email=smoke@example.invalid",
                        *command], cwd=workspace, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Keep Peer names/results isolated. Provider-native sessions still use their normal stores.
    os.environ["XDG_STATE_HOME"] = str(artifacts / "state")
    nonce = "peer-" + uuid.uuid4().hex
    baseline = peer.worktree_snapshot(str(workspace))
    summary = {"status": "running", "provider": args.to, "artifacts": str(artifacts), "checks": []}
    summary_path = artifacts / "summary.json"

    def save():
        peer.save_result(summary_path, summary)

    def call(label, mode, prompt, *session):
        flags = ["ask", "--to", args.to, "--mode", mode, "--cwd", str(workspace),
                 "--timeout", str(args.timeout), "--output", str(artifacts / (label + ".json")),
                 "--prompt", prompt, *session]
        for option in ("config", "model", "effort"):
            if getattr(args, option) is not None:
                flags.extend(["--" + option, getattr(args, option)])
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = peer.main(flags)
        result = json.loads(captured.getvalue())
        summary["checks"].append({"check": label, "status": result["status"],
                                  "session_id": result.get("session_id"), "output_file": result.get("output_file")})
        summary["model"] = result.get("model")
        summary["effort"] = result.get("effort")
        save()
        if code != 0:
            raise ValueError("{}: {}: {}".format(label, result["status"], result.get("error") or result.get("response")))
        return result

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    save()
    try:
        fresh = call("fresh", "consult", "Remember the token {} for a follow-up. Read {} and reply with "
                     "the exact fixture text and then the token. Keep it brief.".format(nonce, fixture),
                     "--new-session", "smoke")
        require(nonce in fresh["response"] and fixture.read_text().strip() in fresh["response"],
                "fresh consultation did not return the fixture and token")
        resumed = call("resume", "consult", "What token did I give you in the previous turn? "
                       "Reply only with the token. Do not read files.", "--session-name", "smoke")
        require(resumed["session_id"] == fresh["session_id"] and resumed["response"].strip() == nonce,
                "resume did not retain the same session and recover the token")
        call("consult", "consult", "Change {} to 'changed by consultation' if your current mode allows it; "
             "otherwise explain the restriction in one sentence.".format(fixture), "--session-name", "smoke")
        require(peer.worktree_report(baseline, peer.worktree_snapshot(str(workspace)))["worktree_changed"] is False
                and fixture.read_text() == "peer consultation fixture\n",
                "consultation modified the fixture repository")
        marker = workspace / "generated.txt"
        work = call("work", "work", "Create {} containing exactly peer-work-marker followed by one newline. "
                    "Use a file-edit tool and change no other files. Reply in one sentence.".format(marker),
                    "--new-session", "worker")
        require(marker.is_file() and marker.read_text() == "peer-work-marker\n", "delegated file content is incorrect")
        require(work.get("worktree_changed") is True and work.get("changed_paths") == ["?? generated.txt"],
                "work result did not report the verified file change")
        require(fixture.read_text() == "peer consultation fixture\n", "work unexpectedly changed the fixture")
        summary.update(status="ok", session_id=fresh["session_id"], work_session_id=work["session_id"],
                       changed_paths=work["changed_paths"],
                       note="Verified session memory, unchanged consultation files, and actual work output. "
                            "Consultation obedience does not establish OS sandbox enforcement or command-denial handling.")
    except (ValueError, OSError, KeyboardInterrupt, peer.Terminated) as exc:
        summary.update(status="error", error=str(exc) or "interrupted")
    save()
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "ok" else 2


if __name__ == "__main__":
    peer.install_signal_handlers()
    raise SystemExit(main())
