import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SLUG = "trio-claude-gemini-codex"
SCRIPTS = REPO / "plugins" / SLUG / "skills" / SLUG / "scripts"

# Stands in for Bridge's flow.sh: records each call and answers like it.
FAKE_FLOW_SH = r"""#!/usr/bin/env bash
printf '%s|FLOW=%s|WD=%s\n' "$*" "${BRIDGE_FLOW_ID-unset}" "${BRIDGE_WORKDIR-}" >>"$FAKE_LOG"
case "$1" in
  start) echo "FLOW=f-test";;
  gate) echo "GATE=${FAKE_GATE:-approve}"; [ "${FAKE_GATE:-}" = steer ] && echo "TEXT=shorter please";;
  step) [ -n "${FAKE_STEER:-}" ] && echo "STEER=$FAKE_STEER";;
esac
exit 0
"""


class BridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.task = self.project / ".multimodel" / "tasks" / "t1"
        self.task.mkdir(parents=True)
        fake = self.project / "fake-flow.sh"
        fake.write_text(FAKE_FLOW_SH)
        self.log = self.project / "calls.log"
        # Bridge's flow.sh needs Node.js; the stand-in does not, so a stand-in node keeps the test
        # independent of whether Node is installed
        bin_dir = self.project / "bin"
        bin_dir.mkdir()
        (bin_dir / "node").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "node").chmod(0o755)
        path = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        self.env = dict(os.environ, PATH=path, BRIDGE_FLOW_SH=str(fake), FAKE_LOG=str(self.log))
        self.env.pop("TRIO_BRIDGE", None)

    def run_script(self, *args, env=None, expected=0):
        result = subprocess.run(args, cwd=self.project, env=env or self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def bridge(self, *args, env=None, expected=0):
        return self.run_script("bash", str(SCRIPTS / "bridge.sh"), str(self.task), *args, env=env, expected=expected)

    def flow(self, *args, env=None, expected=0):
        return self.run_script(sys.executable, str(SCRIPTS / "flow.py"), *args, env=env, expected=expected)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_without_bridge_nothing_changes(self):
        env = dict(self.env, BRIDGE_FLOW_SH=str(self.project / "missing.sh"))
        self.assertIn("BRIDGE=off", self.bridge("start", "add search", env=env).stdout)
        emitted = self.flow("emit", str(self.task), "2", "gemini", "running", env=env)
        self.assertEqual(json.loads(emitted.stdout)["status"], "running")
        self.assertNotIn("bridge:", emitted.stderr)
        self.assertEqual(self.calls(), [])
        off = dict(self.env, TRIO_BRIDGE="off")
        self.assertIn("BRIDGE=off", self.bridge("start", "add search", env=off).stdout)
        self.assertEqual(self.calls(), [])

    def test_start_and_phases_show_as_steps_in_their_lanes(self):
        started = self.bridge("start", "add search", "--summary", "search box on the home page")
        self.assertIn("FLOW=f-test", started.stdout)
        self.assertEqual((self.task / "bridge-flow").read_text().strip(), "f-test")
        self.flow("emit", str(self.task), "2", "gemini", "running")
        self.flow("emit", str(self.task), "2", "claude", "succeeded")
        self.flow("emit", str(self.task), "3", "claude", "running")
        self.flow("emit", str(self.task), "5", "codex", "degraded")
        calls = self.calls()
        self.assertTrue(calls[0].startswith("start add search --size large|"))
        self.assertTrue(calls[1].startswith("step ask you done asked --summary search box on the home page --phase 0|FLOW=f-test"))
        self.assertTrue(calls[2].startswith("step trio-p2-gemini gemini running writing its own plan --phase 1 --after ask|FLOW=f-test"))
        self.assertTrue(calls[3].startswith("step trio-p2-claude claude done wrote a plan --phase 1 --after ask|"))
        self.assertTrue(calls[4].startswith("step trio-p3-claude claude running merging the plans --phase 2 --after trio-p2-gemini,trio-p2-claude|"))
        self.assertTrue(calls[5].startswith("step trio-p5-codex codex done built the backend --phase 4 --after trio-p3-claude --summary finished with a fallback|"))
        self.assertTrue(all(call.endswith(f"|WD={self.project}") for call in calls))

    def test_a_note_from_bridge_reaches_claude(self):
        self.bridge("start", "add search")
        env = dict(self.env, FAKE_STEER="use the existing index")
        emitted = self.flow("emit", str(self.task), "5", "claude", "running", env=env)
        self.assertIn("bridge: STEER=use the existing index", emitted.stderr)
        self.assertEqual(json.loads(emitted.stdout)["agent"], "claude")

    def test_approving_in_bridge_approves_trio_gate_for_that_revision(self):
        self.bridge("start", "add search")
        self.flow("open", str(self.task), "plan", "rev-1", "--timed")
        answered = self.bridge("review", "plan", "rev-1", "review the plan", "--summary", "4 points")
        self.assertIn("GATE=approve", answered.stdout)
        self.assertIn("TRIO=approved", answered.stdout)
        self.assertTrue(self.calls()[-1].startswith("gate trio-plan-rev-1 review the plan --phase 3 --after trio-p3-claude --done-title approved the plan --summary 4 points|"))
        self.flow("check", str(self.task), "plan", "rev-1")

    def test_a_review_for_an_older_revision_is_refused(self):
        self.bridge("start", "add search")
        self.flow("open", str(self.task), "plan", "rev-2", "--timed")
        refused = self.bridge("review", "plan", "rev-1", "review the plan", expected=1)
        self.assertIn("another revision", refused.stderr)
        self.assertEqual(json.loads(self.flow("status", str(self.task), "plan").stdout)["status"], "waiting")

    def test_a_requested_change_holds_the_plan(self):
        self.bridge("start", "add search")
        self.flow("open", str(self.task), "plan", "rev-1", "--timed")
        held = self.bridge("review", "plan", "rev-1", "review the plan", env=dict(self.env, FAKE_GATE="steer"))
        self.assertIn("TEXT=shorter please", held.stdout)
        self.assertIn("TRIO=held", held.stdout)

    def test_push_review_never_carries_on_and_a_decline_blocks_it(self):
        self.bridge("start", "add search")
        self.flow("open", str(self.task), "push", "push-1")
        declined = self.bridge("review", "push", "push-1", "push feature/search", env=dict(self.env, FAKE_GATE="decline"))
        self.assertIn("TRIO=declined", declined.stdout)
        self.assertIn("--no-auto", self.calls()[-1])
        self.flow("check", str(self.task), "push", "push-1", expected=1)

    def test_nobody_answering_leaves_the_plan_to_trios_own_deadline(self):
        self.bridge("start", "add search")
        self.flow("open", str(self.task), "plan", "rev-1", "--timed")
        timed_out = self.bridge("review", "plan", "rev-1", "review the plan", env=dict(self.env, FAKE_GATE="auto"))
        self.assertIn("TRIO=waiting", timed_out.stdout)


if __name__ == "__main__":
    unittest.main()
