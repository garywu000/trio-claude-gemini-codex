#!/usr/bin/env bash
# Show a Trio run live in Bridge (the codex-bridge plugin's desktop app, companion and Claude Code
# pane) when Bridge is installed. Bridge's own flow.sh writes its events, so Bridge's buttons and
# rules apply as they do to its own workflows. Without Bridge every command prints BRIDGE=off and
# succeeds, so Trio runs exactly as before.
#
#   bridge.sh <task-dir> start <title> [--summary <request>]
#   bridge.sh <task-dir> review <plan|push|merge|deploy> <revision> <title> [--summary <text>]
#   bridge.sh <task-dir> step <id> <lane> <state> <title> [flow.sh options]   (flow.py emit calls this)
#   bridge.sh <task-dir> end [--summary <text>]
#
# Finds Bridge's flow.sh from $BRIDGE_FLOW_SH, else the newest one in the usual Claude Code plugin
# folders. TRIO_BRIDGE=off turns the mirror off. Lines flow.sh prints (STEER=, GATE=, TEXT=) are
# passed on: a STEER= line is the user's own note from Bridge.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

task="${1:?usage: bridge.sh <task-dir> <start|review|step|end> ...}"
command="${2:?usage: bridge.sh <task-dir> <start|review|step|end> ...}"
shift 2
[ -d "$task" ] || die "not a task directory: $task"
task="$(cd "$task" && pwd)"

off() { echo "BRIDGE=off${1:+ ($1)}"; exit 0; }
case "${TRIO_BRIDGE:-on}" in off|0|false) off "TRIO_BRIDGE=off";; esac

find_flow_sh() {
  if [ -n "${BRIDGE_FLOW_SH:-}" ]; then
    [ -f "$BRIDGE_FLOW_SH" ] && { echo "$BRIDGE_FLOW_SH"; return 0; }
    return 1
  fi
  local found
  found="$(ls -t "$HOME"/.claude/plugins/marketplaces/*/skills/codex-bridge/flow.sh \
    "$HOME"/.claude/plugins/cache/*/codex-bridge/*/skills/codex-bridge/flow.sh 2>/dev/null | head -1)"
  [ -n "$found" ] && echo "$found"
}
flow_sh="$(find_flow_sh)" || off "Bridge is not installed"
command -v node >/dev/null || off "Bridge needs Node.js"

# One Bridge flow per Trio task, kept beside the task's own record.
id_file="$task/bridge-flow"
# the project the task belongs to: <project>/.multimodel/tasks/<task>
case "$task" in */.multimodel/tasks/*) root="${task%/.multimodel/tasks/*}";; *) root="$(repo_root)";; esac
run() { BRIDGE_WORKDIR="$root" bash "$flow_sh" "$@"; }
flow_id() { [ -f "$id_file" ] && tr -d '[:space:]' <"$id_file"; }

if [ "$command" = start ]; then
  title="${1:?usage: bridge.sh <task-dir> start <title> [--summary <request>]}"; shift
  summary="$title"; [ "${1:-}" = --summary ] && summary="${2:-$title}"
  out="$(run start "$title" --size large)"; rc=$?
  printf '%s\n' "$out"
  [ "$rc" -eq 0 ] || exit "$rc"
  id="$(printf '%s\n' "$out" | sed -n 's/^FLOW=//p' | tail -1)"
  [ -n "$id" ] || die "Bridge did not name the flow"
  printf '%s\n' "$id" >"$id_file"
  BRIDGE_FLOW_ID="$id" run step ask you done "asked" --summary "$summary" --phase 0
  exit $?
fi

id="$(flow_id)"
[ -n "$id" ] || off "no Bridge flow for this task: run bridge.sh <task-dir> start first"
export BRIDGE_FLOW_ID="$id"

case "$command" in
  step|end) run "$command" "$@"; exit $?;;
  review) ;;
  *) die "unknown command: $command (start, review, step or end)";;
esac

# A review in Bridge for one of Trio's gates. Trio's gate must already be open for this exact
# revision (flow.py open). The user's answer in Bridge resolves that gate, bound to the revision
# the user saw; an answer to an older revision changes nothing.
gate="${1:?usage: bridge.sh <task-dir> review <plan|push|merge|deploy> <revision> <title> [--summary <text>]}"
revision="${2:?revision}"; title="${3:?title}"; shift 3
case "$gate" in
  plan) phase=3; after=trio-p3-claude; timing=(); done_title="approved the plan";;
  push) phase=6; after=trio-p6-claude; timing=(--no-auto); done_title="approved the push";;
  merge) phase=6; after=trio-p8-codex; timing=(--no-auto); done_title="approved the merge";;
  deploy) phase=6; after=trio-p8-claude; timing=(--no-auto); done_title="approved the deploy";;
  *) die "gate must be plan, push, merge or deploy";;
esac
flow_py="$SKILL_DIR/scripts/flow.py"
current="$(python3 "$flow_py" status "$task" "$gate")" || die "open Trio's $gate gate first (flow.py open)"
gate_id="$(printf '%s' "$current" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e["gate_id"])')"
current_revision="$(printf '%s' "$current" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e["revision"])')"
[ "$current_revision" = "$revision" ] || die "Trio's $gate gate is for another revision; open it for this one first"
review_id="trio-$gate-$(printf '%s' "$revision" | tr -c 'A-Za-z0-9' '-' | cut -c1-40)"

out="$(run gate "$review_id" "$title" --phase "$phase" --after "$after" --done-title "$done_title" ${timing[@]+"${timing[@]}"} "$@")"; rc=$?
printf '%s\n' "$out"
[ "$rc" -eq 0 ] || exit "$rc"
answer="$(printf '%s\n' "$out" | sed -n 's/^GATE=//p' | tail -1)"
resolve() { python3 "$flow_py" resolve "$task" "$gate" "$gate_id" "$1" >/dev/null || die "could not record the answer in Trio"; }
case "$answer" in
  approve) resolve approve;;
  decline) resolve decline;;
  # a change asked for, or a pause: the plan stays held (no countdown) until a new review
  steer|waiting) resolve hold;;
  # nobody answered: Trio's own 30-second deadline carries the plan on (flow.py check)
  auto) ;;
  *) die "unexpected answer from Bridge: ${answer:-none}";;
esac
echo "TRIO=$(python3 "$flow_py" status "$task" "$gate" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
