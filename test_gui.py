import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import factory
import gui

RID = "20260101-120000"


class GuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        self.run_dir = self.runs / RID
        self.run_dir.mkdir()
        self.server = gui.make_server(self.runs, 0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        r = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_lists_runs_and_serves_run_details(self):
        (self.run_dir / "status.json").write_text(json.dumps({"state": "done", "task": "t"}))
        (self.run_dir / "log.md").write_text("[intake] t\n")
        self.assertEqual(json.loads(self.req("/api/runs")[1])[0]["id"], RID)
        d = json.loads(self.req(f"/api/run/{RID}")[1])
        self.assertEqual((d["status"]["state"], d["log"], d["pending"]), ("done", "[intake] t\n", None))

    def test_missing_status_means_running(self):
        self.assertEqual(json.loads(self.req(f"/api/run/{RID}")[1])["status"]["state"], "running")

    def test_run_id_cannot_escape_runs_dir(self):
        self.assertEqual(self.req("/api/run/..%2f..")[0], 404)
        self.assertEqual(self.req("/api/run/..")[0], 404)

    def test_index_page_served(self):
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"CodeFactory", body)

    def test_answer_requires_pending_gate(self):
        self.assertEqual(self.req(f"/api/run/{RID}/answer", {"approve": True})[0], 409)

    def test_answer_writes_file_for_pending_gate(self):
        (self.run_dir / "pending.json").write_text(json.dumps({"gate": "spec"}))
        self.assertEqual(self.req(f"/api/run/{RID}/answer", {"approve": True})[0], 200)
        self.assertEqual(json.loads((self.run_dir / "answer.json").read_text()), {"gate": "spec", "approve": True})

    def test_answer_rejects_foreign_origin_and_bad_body(self):
        (self.run_dir / "pending.json").write_text(json.dumps({"gate": "spec"}))
        self.assertEqual(self.req(f"/api/run/{RID}/answer", {"approve": True}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req(f"/api/run/{RID}/answer", {"nie": 1})[0], 400)
        self.assertFalse((self.run_dir / "answer.json").exists())

    def test_start_validates_input_and_launches(self):
        with mock.patch.object(gui, "start_run", return_value=None) as start:
            self.assertEqual(self.req("/api/start", {"task": "  zrób x ", "mode": "auto"})[0], 200)
            start.assert_called_once_with("zrób x", "auto", None)
            for bad in ({"task": "x", "mode": "yolo"}, {"task": "  ", "mode": "auto"}, {"task": "x" * 5001, "mode": "auto"}, {"mode": "auto"}):
                self.assertEqual(self.req("/api/start", bad)[0], 400)
            self.assertEqual(self.req("/api/start", {"task": "x", "mode": "auto"}, {"Origin": "http://evil.example"})[0], 403)
            self.assertEqual(start.call_count, 1)

    def test_start_accepts_only_known_verify_presets(self):
        with mock.patch.object(gui, "presets", return_value=["python", "node"]),                 mock.patch.object(gui, "start_run", return_value=None) as start:
            self.assertEqual(self.req("/api/start", {"task": "x", "mode": "auto", "verify": "node"})[0], 200)
            start.assert_called_once_with("x", "auto", "node")
            self.assertEqual(self.req("/api/start", {"task": "x", "mode": "auto", "verify": "rm -rf /"})[0], 400)
            self.assertEqual(json.loads(self.req("/api/presets")[1]), ["python", "node"])

    def test_start_run_passes_verify_preset_to_factory(self):
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = gui.subprocess.TimeoutExpired("x", 1)
            gui.start_run("zadanie", "auto", "node")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[argv.index("--verify") + 1], "node")
        self.assertEqual(argv[-2:], ["--", "zadanie"])

    def test_start_reports_launch_failure(self):
        with mock.patch.object(gui, "start_run", return_value="nie wystartował"):
            code, body = self.req("/api/start", {"task": "x", "mode": "auto"})
        self.assertEqual((code, json.loads(body)["error"]), (500, "nie wystartował"))

    def test_start_run_passes_task_after_double_dash(self):
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = gui.subprocess.TimeoutExpired("x", 1)
            self.assertIsNone(gui.start_run("--help", "guided"))
        argv = popen.call_args[0][0]
        self.assertEqual(argv[-2:], ["--", "--help"])  # zadanie nie może zostać wzięte za flagę
        self.assertIn("gui", argv)

    def test_resume_only_for_failed_or_rejected_runs(self):
        with mock.patch.object(gui, "resume_run", return_value=None) as resume:
            for state, code in (("running", 409), ("done", 409), ("failed: limit iteracji", 200), ("rejected: plan", 200)):
                (self.run_dir / "status.json").write_text(json.dumps({"state": state}))
                self.assertEqual(self.req(f"/api/run/{RID}/resume", {})[0], code, state)
            self.assertEqual(resume.call_count, 2)
            resume.assert_called_with(RID)

    def test_resume_run_launches_factory_with_resume_flag(self):
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            popen.return_value.wait.side_effect = gui.subprocess.TimeoutExpired("x", 1)
            self.assertIsNone(gui.resume_run(RID))
        self.assertEqual(popen.call_args[0][0][-2:], ["--resume", RID])

    def test_factory_approve_waits_for_gui_answer(self):
        def human():
            while not (self.run_dir / "pending.json").exists():
                time.sleep(0.01)
            self.req(f"/api/run/{RID}/answer", {"approve": False})
        threading.Thread(target=human, daemon=True).start()
        with mock.patch.object(factory, "POLL", 0.01):
            self.assertFalse(factory.approve("spec", "guided", self.run_dir, "gui"))
        self.assertFalse((self.run_dir / "pending.json").exists())  # posprzątane

    def test_factory_approve_skips_gates_not_in_mode(self):
        self.assertTrue(factory.approve("spec", "auto", self.run_dir, "gui"))  # nie blokuje


if __name__ == "__main__":
    unittest.main()
