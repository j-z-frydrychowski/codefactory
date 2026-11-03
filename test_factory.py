import re
import sys
import tempfile
import threading
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


PLAN2 = '```json\n[{"title": "A", "description": "pierwsze"}, {"title": "B", "description": "drugie"}]\n```'


def fake(builder_code, reviews=(), plan="", shared=False, barrier=None):
    reviews = iter(reviews)
    prompts = []
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def run_agent(engine, prompt, workdir, timeout):
        prompts.append(prompt)
        if "INTAKE" in prompt:
            return True, "## Cel\nx"
        if "PLANNER" in prompt:
            return True, plan
        if "REVIEWER" in prompt:
            return True, next(reviews)
        title = re.search(r"# Zadanie do wykonania teraz\n(.*)", prompt).group(1)
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            if barrier:
                barrier.wait()  # przechodzi tylko, gdy oba zadania działają naraz (inaczej BrokenBarrierError)
            (Path(workdir) / f"test_{title}.py").write_text(builder_code)
            if shared:  # oba zadania dotykają tego samego pliku => konflikt przy drugim scaleniu
                (Path(workdir) / "shared.txt").write_text("resolved" if "Konflikt" in prompt else title)
            return True, "ok"
        finally:
            with lock:
                state["active"] -= 1
    run_agent.prompts, run_agent.state = prompts, state
    return run_agent


class FactoryTest(unittest.TestCase):
    def go(self, builder_code, mode="auto", reviews=None, plan=None, parallel=1, **fake_kw):
        cfg = {**CFG, "limits": {**CFG["limits"], "max_parallel": parallel}}
        if reviews is not None:
            cfg = {**cfg, "roles": {**cfg["roles"], "reviewer": "x"}}
        if plan is not None:
            cfg = {**cfg, "roles": {**cfg["roles"], "planner": "x"}}
        self.agent = fake(builder_code, reviews or (), plan or "", **fake_kw)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(factory, "run_agent", self.agent):
            state, run_dir = factory.run(cfg, "zrób x", mode, Path(d))
            self.files = sorted(p.name for p in (run_dir / "workspace").glob("test_*.py"))
            self.log = factory.git(run_dir / "workspace", "log", "--format=%s")
            self.worktrees = factory.git(run_dir / "workspace", "worktree", "list").count("\n")
            shared = run_dir / "workspace" / "shared.txt"
            self.shared = shared.read_text() if shared.exists() else None
            return state

    def test_planner_tasks_run_in_order_and_merge_into_main(self):
        self.assertEqual(self.go(PASSING, plan=PLAN2), "done")
        self.assertEqual(len(self.files), 2)  # oba zadania wylądowały w głównym workspace
        self.assertEqual(self.log.split(), ["B", "A", "init"])
        self.assertEqual(self.worktrees, 1)  # worktree sprzątnięte po merge

    def test_failed_task_stops_run_and_is_not_merged(self):
        self.assertEqual(self.go(FAILING, plan=PLAN2), "failed: limit iteracji")
        self.assertEqual(self.files, [])
        self.assertEqual(self.log.split(), ["init"])

    def test_independent_tasks_really_run_in_parallel(self):
        plan = '[{"title": "A", "description": "a", "depends_on": []}, {"title": "B", "description": "b", "depends_on": []}]'
        barrier = threading.Barrier(2, timeout=10)
        self.assertEqual(self.go(PASSING, plan=plan, parallel=2, barrier=barrier), "done")
        self.assertEqual(len(self.files), 2)
        self.assertEqual(self.agent.state["max_active"], 2)

    def test_tasks_without_depends_on_stay_sequential_even_with_parallel_limit(self):
        self.assertEqual(self.go(PASSING, plan=PLAN2, parallel=3), "done")
        self.assertEqual(self.agent.state["max_active"], 1)
        self.assertEqual(self.log.split(), ["B", "A", "init"])

    def test_parallel_merge_conflict_is_sent_back_to_builder(self):
        plan = '[{"title": "A", "description": "a", "depends_on": []}, {"title": "B", "description": "b", "depends_on": []}]'
        self.assertEqual(self.go(PASSING, plan=plan, parallel=2, shared=True), "done")
        self.assertEqual(self.shared, "resolved")  # builder rozwiązał konflikt, żaden znacznik nie został
        self.assertEqual(sorted(self.files), ["test_A.py", "test_B.py"])
        self.assertEqual(self.worktrees, 1)

    def test_failed_task_blocks_its_dependents_only_after_running_ones_finish(self):
        plan = '[{"title": "A", "description": "a", "depends_on": []}, {"title": "B", "description": "b", "depends_on": [1]}]'
        self.assertEqual(self.go(FAILING, plan=plan, parallel=2), "failed: limit iteracji")
        self.assertEqual(self.files, [])  # B nigdy nie wystartował

    def test_bad_plan_fails_run(self):
        self.assertEqual(self.go(PASSING, plan="nie json"), "failed: planner")

    def test_plan_gate_can_reject_in_manual_mode(self):
        with mock.patch("builtins.input", side_effect=["t", "n"]):
            self.assertEqual(self.go(PASSING, mode="manual", plan=PLAN2), "rejected: plan")

    def test_parse_plan_depends_on(self):
        plan = factory.parse_plan('[{"title":"a","description":"b"},{"title":"c","description":"d"},'
                                  '{"title":"e","description":"f","depends_on":[]}]', 8)
        self.assertEqual([t["depends_on"] for t in plan], [[], [1], []])  # domyślnie: po poprzednim
        for bad in ('"depends_on": [2]', '"depends_on": [0]', '"depends_on": "1"', '"depends_on": [true]'):
            with self.assertRaises(ValueError):
                factory.parse_plan('[{"title":"a","description":"b"},{"title":"c","description":"d",%s}]' % bad, 8)

    def test_parse_plan_limits(self):
        with self.assertRaises(ValueError):
            factory.parse_plan("[]", 8)
        with self.assertRaises(ValueError):
            factory.parse_plan(PLAN2, 1)

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
            engines.run_agent({"type": "grpc"}, "x", ".")


if __name__ == "__main__":
    unittest.main()
