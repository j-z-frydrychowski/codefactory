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


def fake(builder_code):
    def run_agent(engine, prompt, workdir, timeout):
        if "INTAKE" in prompt:
            return True, "## Cel\nx"
        (Path(workdir) / "test_x.py").write_text(builder_code)
        return True, "ok"
    return run_agent


class FactoryTest(unittest.TestCase):
    def go(self, builder_code, mode="auto"):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(factory, "run_agent", fake(builder_code)):
            return factory.run(CFG, "zrób x", mode, Path(d))[0]

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
