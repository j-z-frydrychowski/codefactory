import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engines
import factory

CFG = {
    "mode": "auto",
    "engines": {"x": {"type": "cli", "command": "x"}},
    "roles": {"intake": "x", "builder": "x"},
    "verify": f'"{sys.executable}" -m unittest discover',
    "limits": {"max_iterations": 2, "timeout_seconds": 60},
}
PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test(self): self.assertTrue(True)\n"
FAILING = PASSING.replace("True)", "False)")


def fake(builder_code, reviews=()):
    reviews = iter(reviews)
    prompts = []

    def run_agent(engine, prompt, workdir, timeout):
        prompts.append(prompt)
        if "INTAKE" in prompt:
            return True, "## Cel\nx"
        if "REVIEWER" in prompt:
            return True, next(reviews)
        (Path(workdir) / "test_x.py").write_text(builder_code)
        return True, "ok"
    run_agent.prompts = prompts
    return run_agent


class FactoryTest(unittest.TestCase):
    def go(self, builder_code, mode="auto", reviews=None):
        cfg = CFG if reviews is None else {**CFG, "roles": {**CFG["roles"], "reviewer": "x"}}
        self.agent = fake(builder_code, reviews or ())
        with tempfile.TemporaryDirectory() as d, mock.patch.object(factory, "run_agent", self.agent):
            return factory.run(cfg, "zrób x", mode, Path(d))[0]

    def test_reviewer_fail_sends_feedback_to_builder_then_pass(self):
        self.assertEqual(self.go(PASSING, reviews=["VERDICT: FAIL\n1. brak testu", "VERDICT: PASS\nok"]), "done")
        self.assertIn("brak testu", self.agent.prompts[3])  # drugi build widzi uwagi recenzenta

    def test_reviewer_fail_every_time_hits_limit(self):
        self.assertEqual(self.go(PASSING, reviews=["VERDICT: FAIL\nx"] * 2), "failed: limit iteracji")

    def test_reviewer_skipped_when_verify_fails(self):
        self.assertEqual(self.go(FAILING, reviews=[]), "failed: limit iteracji")  # next(reviews) would raise

    def test_done_when_verify_passes(self):
        self.assertEqual(self.go(PASSING), "done")

    def test_stops_at_iteration_limit(self):
        self.assertEqual(self.go(FAILING), "failed: limit iteracji")

    def test_spec_gate_can_reject(self):
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(self.go(PASSING, "guided"), "rejected: spec")

    def test_cli_engine_pipes_prompt_via_stdin(self):
        e = {"type": "cli", "command": f'"{sys.executable}" -c "import sys;print(sys.stdin.read().upper())"'}
        ok, out = engines.run_agent(e, "abc", ".")
        self.assertTrue(ok)
        self.assertEqual(out.strip(), "ABC")

    def test_unknown_engine_type(self):
        with self.assertRaises(NotImplementedError):
            engines.run_agent({"type": "api"}, "x", ".")


if __name__ == "__main__":
    unittest.main()
