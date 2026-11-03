import json
import re
import shutil
import subprocess
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
            code = builder_code(title) if callable(builder_code) else builder_code
            (Path(workdir) / f"test_{title}.py").write_text(code)
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

    def roles(self, **extra):
        return {**CFG, "roles": {**CFG["roles"], **extra}}

    def test_resume_redoes_only_unfinished_tasks(self):
        cfg = self.roles(planner="x")
        first = fake(lambda title: PASSING if title == "A" else FAILING, plan=PLAN2)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(factory, "run_agent", first):
                state, run_dir = factory.run(cfg, "zrób x", "auto", Path(d))
            self.assertEqual(state, "failed: limit iteracji")
            self.assertEqual(factory.jload(run_dir / "progress.json")["done"], [1])
            second = fake(PASSING)
            with mock.patch.object(factory, "run_agent", second):
                state, _ = factory.resume(cfg, run_dir)
            work = run_dir / "workspace"
            self.assertEqual(state, "done")
            self.assertEqual(sorted(p.name for p in work.glob("test_*.py")), ["test_A.py", "test_B.py"])
            self.assertEqual(factory.git(work, "log", "--format=%s").split(), ["B", "A", "init"])
            self.assertEqual(factory.git(work, "worktree", "list").count("\n"), 1)  # zostawiony worktree po porażce sprzątnięty
        titles = [p.split("wykonania teraz\n")[1].split("\n")[0] for p in second.prompts if "BUILDER" in p]
        self.assertEqual(titles, ["B"])  # A (gotowe) nie wraca do buildera
        self.assertFalse(any("INTAKE" in p or "PLANNER" in p for p in second.prompts))  # spec i plan z poprzedniej próby

    def test_resume_asks_again_for_unapproved_gate_without_rerunning_intake(self):
        with tempfile.TemporaryDirectory() as d:
            first = fake(PASSING)
            with mock.patch.object(factory, "run_agent", first), mock.patch("builtins.input", return_value="n"):
                state, run_dir = factory.run(CFG, "zrób x", "guided", Path(d))
            self.assertEqual(state, "rejected: spec")
            second = fake(PASSING)
            with mock.patch.object(factory, "run_agent", second), mock.patch("builtins.input", return_value="t"):
                state, _ = factory.resume(CFG, run_dir)
        self.assertEqual(state, "done")
        self.assertFalse(any("INTAKE" in p for p in second.prompts))

    def test_resume_of_finished_run_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(factory, "run_agent", fake(PASSING)):
                _, run_dir = factory.run(CFG, "zrób x", "auto", Path(d))
            never = mock.Mock(side_effect=AssertionError("agent nie powinien być wołany"))
            with mock.patch.object(factory, "run_agent", never):
                self.assertEqual(factory.resume(CFG, run_dir)[0], "done")

    def test_resume_rejects_non_run_dir(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            factory.resume(CFG, Path(d))

    def test_unencodable_console_char_does_not_crash_run(self):
        import io
        plan = '[{"title": "A", "description": "a ≈ b"}]'
        cp1250 = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")  # jak konsola/plik przy przekierowaniu na Windows
        with mock.patch.object(sys, "stdout", cp1250):
            self.assertEqual(self.go(PASSING, plan=plan), "done")

    def test_hollow_verify_command_fails_before_any_agent_runs(self):
        cfg = {**CFG, "verify": f'"{sys.executable}" -c "pass"'}  # przechodzi zawsze, też na pustym projekcie
        agent = mock.Mock(side_effect=AssertionError("agent nie powinien być wołany"))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(factory, "run_agent", agent):
            self.assertEqual(factory.run(cfg, "zrób x", "auto", Path(d))[0], "failed: verify")

    def test_canary_can_be_disabled(self):
        cfg = {**CFG, "verify": f'"{sys.executable}" -c "pass"', "verify_canary": False}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(factory, "run_agent", fake(PASSING)):
            self.assertEqual(factory.run(cfg, "zrób x", "auto", Path(d))[0], "done")

    @unittest.skipUnless(shutil.which("node"), "brak node")
    def test_node_test_without_tests_is_caught_as_hollow(self):
        self.assertFalse(factory.verify_is_meaningful("node --test"))  # kod 0 na pustym katalogu
        self.assertTrue(factory.verify_is_meaningful(f'"{sys.executable}" -m unittest discover'))

    def nonempty(self, code):
        script = str(Path(factory.ROOT) / "tools" / "nonempty.py")
        return subprocess.run([sys.executable, script, sys.executable, "-c", code], capture_output=True, text=True).returncode

    def test_nonempty_wrapper_fails_when_no_test_ran_and_keeps_real_exit_codes(self):
        self.assertEqual(self.nonempty("print('ℹ tests 0')"), 1)
        self.assertEqual(self.nonempty("print('Ran 0 tests in 0.0s')"), 1)
        self.assertEqual(self.nonempty("print('ℹ tests 3')"), 0)
        self.assertEqual(self.nonempty("import sys; print('x'); sys.exit(3)"), 3)

    @unittest.skipUnless(shutil.which("node"), "brak node")
    def test_node_preset_is_meaningful_and_passes_with_a_test(self):
        cmd = json.loads((Path(factory.ROOT) / "config.json").read_text(encoding="utf-8"))["verify_presets"]["node"]
        cmd = cmd.replace("{root}", Path(factory.ROOT).as_posix())
        self.assertTrue(factory.verify_is_meaningful(cmd))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.test.js").write_text("require('node:test')('x', () => {})")
            self.assertTrue(factory.verify(cmd, d)[0])

    def test_agents_see_verify_command_and_resume_keeps_it(self):
        cfg = {**CFG, "verify": f'"{sys.executable}" -m unittest discover -v'}
        first = fake(FAILING)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(factory, "run_agent", first):
                _, run_dir = factory.run(cfg, "zrób x", "auto", Path(d))
            self.assertTrue(all("unittest discover -v" in p for p in first.prompts))
            second = fake(PASSING)
            with mock.patch.object(factory, "run_agent", second):  # resume z configiem o innej komendzie
                state, _ = factory.resume({**CFG, "verify": "false"}, run_dir)
        self.assertEqual(state, "done")
        self.assertTrue(all("unittest discover -v" in p for p in second.prompts))

    def test_cleanup_failure_after_merge_does_not_fail_the_task(self):
        real_try, real = factory.git_try, factory.git

        def flaky_try(cwd, *args):
            if args[:2] == ("worktree", "remove"):
                return 255, "Directory not empty"  # jak na Windowsie, gdy proces trzyma katalog
            return real_try(cwd, *args)

        def flaky(cwd, *args):
            if args[:2] == ("worktree", "remove"):
                raise subprocess.CalledProcessError(255, "git")
            return real(cwd, *args)
        with mock.patch.object(factory, "git_try", flaky_try), mock.patch.object(factory, "git", flaky):
            self.assertEqual(self.go(PASSING, plan=PLAN2), "done")
        self.assertEqual(self.log.split(), ["B", "A", "init"])  # oba zadania scalone
        self.assertEqual(self.worktrees, 1)

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
