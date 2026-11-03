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
