import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import engines


def call(name, **args):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class FakeLLM:
    """Lokalny serwer OpenAI-compatible, odpowiadający kolejnymi wiadomościami ze skryptu."""

    def __init__(self, script):
        self.requests, self.headers = [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                outer.headers.append(dict(self.headers))
                code, out = (200, {"choices": [{"message": script.pop(0)}]}) if script else (500, {})
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


class ApiEngineTest(unittest.TestCase):
    def run_api(self, script, **engine):
        with FakeLLM(script) as llm, tempfile.TemporaryDirectory() as d:
            e = {"type": "api", "base_url": llm.url, "model": "m", **engine}
            self.result = engines.run_agent(e, "zrób", d, timeout=30)
            self.llm, self.dir = llm, Path(d)
            self.files = sorted(p.name for p in Path(d).rglob("*") if p.is_file())
            return self.result

    def tool_reply(self):  # odpowiedź narzędzia widziana przez model w 2. zapytaniu
        return self.llm.requests[1]["messages"][-1]["content"]

    def test_write_tool_then_final_answer(self):
        with mock.patch.dict(os.environ, {"K": "sekret"}):
            r = self.run_api([call("write_file", path="a/hello.py", content="print(1)"),
                              {"role": "assistant", "content": "gotowe"}], key_env="K")
        self.assertEqual(r, (True, "gotowe"))
        self.assertEqual(self.files, ["hello.py"])
        self.assertEqual(self.llm.headers[0]["Authorization"], "Bearer sekret")
        self.assertEqual(self.tool_reply(), "zapisano")

    def test_path_escape_blocked(self):
        self.run_api([call("write_file", path="../evil.txt", content="x"), {"content": "k"}])
        self.assertFalse((self.dir.parent / "evil.txt").exists())
        self.assertIn("błąd", self.tool_reply())

    def test_git_dir_blocked(self):
        self.run_api([call("write_file", path=".git/hooks/pre-commit", content="x"), {"content": "k"}])
        self.assertEqual(self.files, [])
        self.assertIn("błąd", self.tool_reply())

    def test_readonly_hides_write_tools(self):
        self.run_api([call("write_file", path="a", content="x"), {"content": "k"}], readonly=True, allowed_commands=["python"])
        names = {t["function"]["name"] for t in self.llm.requests[0]["tools"]}
        self.assertEqual(names, {"list_files", "read_file"})
        self.assertEqual(self.files, [])
        self.assertIn("niedostępne", self.tool_reply())

    def test_command_allowlist(self):
        self.run_api([call("run_command", command="rm -rf x"), {"content": "k"}], allowed_commands=["python"])
        self.assertIn("spoza listy", self.tool_reply())
        self.run_api([call("run_command", command='python -c "print(42)"'), {"content": "k"}], allowed_commands=["python"])
        self.assertIn("42", self.tool_reply())

    def test_run_command_not_offered_without_allowlist(self):
        self.run_api([{"content": "k"}])
        names = {t["function"]["name"] for t in self.llm.requests[0]["tools"]}
        self.assertEqual(names, {"list_files", "read_file", "write_file"})

    def test_http_error_returns_failure(self):
        ok, text = self.run_api([])
        self.assertFalse(ok)
        self.assertIn("500", text)

    def test_step_limit(self):
        ok, text = self.run_api([call("list_files")] * 5, max_steps=2)
        self.assertEqual((ok, text), (False, "limit kroków silnika"))

    def test_missing_key_env(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("BRAK_KLUCZA", None)
            ok, text = engines.run_agent({"type": "api", "base_url": "http://x", "model": "m", "key_env": "BRAK_KLUCZA"}, "x", ".")
        self.assertFalse(ok)
        self.assertIn("BRAK_KLUCZA", text)


if __name__ == "__main__":
    unittest.main()
