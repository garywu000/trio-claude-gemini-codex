#!/usr/bin/env python3
"""Record Trio progress and manage revision-bound workflow gates."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


GATE_PHASES = {"plan": 3, "push": 7, "merge": 8, "deploy": 8}
PLAN_WAIT_MS = 30_000
DECISIONS = {"approve": "approved", "hold": "held", "decline": "declined"}

# Bridge (the codex-bridge plugin) shows a run in seven steps: ask, two plans, merge, decide,
# build, review, check & ship. Trio's phases map onto them as in references/workflow-ui.md.
BRIDGE_PHASES = {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 4, 6: 5, 7: 6, 8: 6}
# What each worker's box says in Bridge: while it runs, and once it is done.
STEP_TITLES = {
    (0, "claude"): ("checking the setup", "checked the setup"),
    (1, "claude"): ("reading the code", "read the code"),
    (1, "gemini"): ("scouting the code", "scouted the code"),
    (2, "claude"): ("writing a plan", "wrote a plan"),
    (2, "gemini"): ("writing its own plan", "wrote its own plan"),
    (3, "claude"): ("merging the plans", "merged the plans"),
    (4, "claude"): ("designing the screens and words", "designed the screens and words"),
    (4, "gemini"): ("reviewing the design", "reviewed the design"),
    (5, "claude"): ("building the frontend", "built the frontend"),
    (5, "gemini"): ("writing acceptance tests", "wrote acceptance tests"),
    (5, "codex"): ("building the backend", "built the backend"),
    (6, "claude"): ("integrating and testing", "integrated and tested"),
    (6, "gemini"): ("checking the screenshots", "checked the screenshots"),
    (7, "claude"): ("pushing and opening the PR", "opened the PR"),
    (8, "codex"): ("reviewing the PR", "reviewed the PR"),
    (8, "claude"): ("merging and checking it live", "merged and checked it live"),
}


def append_event(log, event):
    log.write(json.dumps(event, separators=(",", ":")) + "\n")
    log.flush()
    return event


def latest_gate(log, gate):
    log.seek(0)
    current = None
    for line in log:
        event = json.loads(line)
        if event["type"] == "gate" and event["gate"] == gate:
            current = event
    return current


def expire_plan(log, event, now):
    deadline = event["deadline_ms"]
    if event["gate"] == "plan" and event["status"] == "waiting" and deadline is not None and now >= deadline:
        return append_event(log, dict(event, timestamp_ms=now, agent="workflow", status="continued_after_timeout"))
    return event


def apply_command(log, args, base):
    now = base["timestamp_ms"]
    if args.command == "emit":
        event = dict(base, type="phase", phase=args.phase, agent=args.agent, status=args.status)
        return append_event(log, event), 0

    current = latest_gate(log, args.gate)
    if args.command == "open":
        if current and current["revision"] == args.revision:
            return expire_plan(log, current, now), 0
        event = dict(
            base,
            type="gate",
            phase=GATE_PHASES[args.gate],
            agent="claude",
            status="waiting",
            gate=args.gate,
            gate_id=uuid.uuid4().hex,
            revision=args.revision,
            deadline_ms=now + PLAN_WAIT_MS if args.timed else None,
        )
        return append_event(log, event), 0

    if current is None:
        raise ValueError(f"no {args.gate} gate exists")

    if args.command == "resolve":
        if current["gate_id"] != args.gate_id:
            raise ValueError(f"this {args.gate} gate has been superseded")
        current = expire_plan(log, current, now)
        status = DECISIONS[args.decision]
        if current["status"] == status:
            return current, 0
        if current["status"] not in ("waiting", "held"):
            raise ValueError(f"this {args.gate} gate is already {current['status']}")
        event = dict(current, timestamp_ms=now, agent="user", status=status, deadline_ms=None)
        return append_event(log, event), 0

    current = expire_plan(log, current, now)
    if args.command == "status":
        return current, 0

    can_proceed = current["status"] == "approved" or (
        args.gate == "plan" and current["status"] == "continued_after_timeout"
    )
    return current, 0 if can_proceed and current["revision"] == args.revision else 1


def bridge_step(task_dir, event):
    """Show a phase event as a step in Bridge through scripts/bridge.sh, when Bridge is installed.

    Best effort: Trio's own record is already written, and a missing or failing Bridge never
    changes this command's result. Bridge's lines go to stderr with a "bridge: " prefix; a
    "bridge: STEER=<text>" line is the user's own note from Bridge.
    """
    phase, agent, status = event["phase"], event["agent"], event["status"]
    running, done = STEP_TITLES.get((phase, agent), (f"working on phase {phase}", f"finished phase {phase}"))
    state, title, summary = {
        "running": ("running", running, None),
        "succeeded": ("done", done, None),
        "failed": ("failed", running, "failed"),
        "skipped": ("done", f"skipped: {running}", None),
        "degraded": ("done", done, "finished with a fallback"),
    }[status]
    # Lines from the workers of the latest earlier phase, or from the request.
    earlier = {}
    with (task_dir / "events.jsonl").open(encoding="utf-8") as log:
        for line in log:
            record = json.loads(line)
            if record.get("type") == "phase" and record["phase"] < phase:
                earlier.setdefault(record["phase"], [])
                if record["agent"] not in earlier[record["phase"]]:
                    earlier[record["phase"]].append(record["agent"])
    after = [f"trio-p{max(earlier)}-{name}" for name in earlier[max(earlier)]] if earlier else ["ask"]
    args = [
        "bash", str(Path(__file__).with_name("bridge.sh")), str(task_dir), "step",
        f"trio-p{phase}-{agent}", agent, state, title,
        "--phase", str(BRIDGE_PHASES[phase]), "--after", ",".join(after),
    ]
    if summary:
        args += ["--summary", summary]
    env = dict(os.environ)
    if state != "running":
        # a finished step is reported at once; a pause in Bridge holds the next step that starts
        env["BRIDGE_FLOW_SKIP_CHECK"] = "1"
    try:
        result = subprocess.run(args, capture_output=True, text=True, env=env)
    except OSError as exc:
        print(f"bridge: {exc}", file=sys.stderr)
        return
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip() and not line.startswith("BRIDGE=off"):
            print(f"bridge: {line}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("open", "status", "resolve", "check", "emit"):
        sub = commands.add_parser(command)
        sub.add_argument("task_dir", type=Path)
        if command == "emit":
            sub.add_argument("phase", type=int, choices=range(9))
            sub.add_argument("agent", choices=("claude", "gemini", "codex"))
            sub.add_argument("status", choices=("running", "succeeded", "failed", "skipped", "degraded"))
            continue
        sub.add_argument("gate", choices=GATE_PHASES)
        if command in ("open", "check"):
            sub.add_argument("revision")
        if command == "open":
            sub.add_argument("--timed", action="store_true", help="allow the plan to proceed after 30 seconds")
        if command == "resolve":
            sub.add_argument("gate_id")
            sub.add_argument("decision", choices=DECISIONS)

    args = parser.parse_args(argv)
    if args.command == "open" and args.timed and args.gate != "plan":
        parser.error("only the plan gate supports a deadline")
    task_dir = args.task_dir.resolve()
    try:
        with (task_dir / "events.jsonl").open("a+", encoding="utf-8") as log:
            # Read, decide, and append under one lock so a timeout and a click cannot both win.
            fcntl.flock(log, fcntl.LOCK_EX)
            base = {
                "schema_version": 1,
                "run_id": task_dir.name,
                "timestamp_ms": time.time_ns() // 1_000_000,
            }
            event, code = apply_command(log, args, base)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(event))
    sys.stdout.flush()
    if args.command == "emit":
        # after the lock is released: a pause in Bridge can hold this for a while
        bridge_step(task_dir, event)
    return code


if __name__ == "__main__":
    sys.exit(main())
