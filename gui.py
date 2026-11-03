"""Podgląd na żywo runów fabryki (tylko czyta runs/) + zatwierdzanie bramek. Uruchom: python gui.py"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
RUN_ID = re.compile(r"\d{8}-\d{6}")


def text(p):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def jread(p):
    try:
        return json.loads(text(p))
    except ValueError:  # plik w trakcie zapisu albo brak
        return None


def make_server(runs, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def run_dir(self, rid):
            d = runs / rid
            return d if RUN_ID.fullmatch(rid) and d.is_dir() else None  # id z URL nie może wyjść z runs/

        def do_GET(self):
            parts = self.path.strip("/").split("/")
            if self.path == "/":
                return self.reply(200, (ROOT / "gui.html").read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "runs"]:
                ids = sorted((d.name for d in runs.glob("*") if RUN_ID.fullmatch(d.name)), reverse=True)
                return self.reply(200, [{"id": i, "status": jread(runs / i / "status.json") or {"state": "running"}}
                                        for i in ids])
            d = self.run_dir(parts[2]) if len(parts) == 3 and parts[:2] == ["api", "run"] else None
            if not d:
                return self.reply(404, {"error": "nie ma"})
            self.reply(200, {"status": jread(d / "status.json") or {"state": "running"}, "pending": jread(d / "pending.json"),
                             "spec": text(d / "spec.md"), "tasks": text(d / "tasks.md"), "log": text(d / "log.md")})

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            d = self.run_dir(parts[2]) if len(parts) == 4 and parts[:2] == ["api", "run"] and parts[3] == "answer" else None
            if not d:
                return self.reply(404, {"error": "nie ma"})
            origin = self.headers.get("Origin")  # blokuje POST ze stron otwartych w innych zakładkach
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                return self.reply(403, {"error": "zły origin"})
            pending = jread(d / "pending.json")
            if not pending:
                return self.reply(409, {"error": "brak oczekującej bramki"})
            try:
                approve = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["approve"] is True
            except (ValueError, KeyError, TypeError):
                return self.reply(400, {"error": "zły JSON"})
            (d / "answer.json").write_text(json.dumps({"gate": pending["gate"], "approve": approve}), encoding="utf-8")
            self.reply(200, {"ok": True})

        def log_message(self, *a):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)  # tylko localhost, bez uwierzytelniania


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=str(ROOT / "runs"))
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    print(f"http://127.0.0.1:{a.port}")
    make_server(Path(a.runs), a.port).serve_forever()
