"""Podgląd na żywo runów fabryki (tylko czyta runs/) + zatwierdzanie bramek. Uruchom: python gui.py"""
import argparse
import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from factory import GATES, run_process_alive

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


def status(d):
    """status.json z wykryciem "przerwany": run "running"/"needs human", a proces z pliku pid nie żyje.
    Run bez pliku pid (stary albo świeżo startujący) zostaje "running": bez pewności nie sugerujemy wznowienia."""
    st = jread(d / "status.json") or {"state": "running"}
    if st["state"] == "running" or st["state"].startswith("needs human"):
        if run_process_alive(d) is False:
            return {**st, "state": "przerwany"}
    return st


def spawn(*args):
    """Odpala factory.py jako osobny proces z bramkami przez GUI. Zwraca tekst błędu albo None."""
    (ROOT / "runs").mkdir(exist_ok=True)
    with open(ROOT / "runs" / "_gui.log", "ab") as log:
        p = subprocess.Popen([sys.executable, str(ROOT / "factory.py"), "--approval", "gui", *args],
                             cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    try:
        return f"factory.py zakończył się kodem {p.wait(timeout=1)} (zobacz runs/_gui.log)"
    except subprocess.TimeoutExpired:
        return None  # nadal działa = wystartował


def presets():
    """Nazwy presetów weryfikacji z config.json (GUI nie przyjmuje surowych komend: to byłoby uruchamianie dowolnego kodu z przeglądarki)."""
    return list((jread(ROOT / "config.json") or {}).get("verify_presets", {}))


def start_run(task, mode, verify=None):
    return spawn("--mode", mode, *(["--verify", verify] if verify else []), "--", task)


def resume_run(rid):
    return spawn("--resume", rid)


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
            if parts == ["api", "presets"]:
                return self.reply(200, presets())
            if parts == ["api", "runs"]:
                ids = sorted((d.name for d in runs.glob("*") if RUN_ID.fullmatch(d.name)), reverse=True)
                return self.reply(200, [{"id": i, "status": status(runs / i)}
                                        for i in ids])
            d = self.run_dir(parts[2]) if len(parts) == 3 and parts[:2] == ["api", "run"] else None
            if not d:
                return self.reply(404, {"error": "nie ma"})
            st = status(d)
            self.reply(200, {"status": st, "pending": None if st["state"] == "przerwany" else jread(d / "pending.json"),
                             "spec": text(d / "spec.md"), "tasks": text(d / "tasks.md"), "log": text(d / "log.md")})

        def origin_ok(self):  # blokuje POST ze stron otwartych w innych zakładkach
            o = self.headers.get("Origin")
            return not o or o in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")

        def body(self):
            try:
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return None

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if not self.origin_ok():
                return self.reply(403, {"error": "zły origin"})
            if parts == ["api", "start"]:
                b = self.body()
                task, mode, verify = (b.get("task"), b.get("mode"), b.get("verify")) if isinstance(b, dict) else (None, None, None)
                if not (isinstance(task, str) and 0 < len(task.strip()) <= 5000 and mode in GATES
                        and (verify is None or verify in presets())):
                    return self.reply(400, {"error": "potrzebne: task (1-5000 znaków), mode i opcjonalnie verify (preset z configu)"})
                err = start_run(task.strip(), mode, verify)
                return self.reply(500 if err else 200, {"error": err} if err else {"ok": True})
            d = self.run_dir(parts[2]) if len(parts) == 4 and parts[:2] == ["api", "run"] and parts[3] in ("answer", "resume") else None
            if not d:
                return self.reply(404, {"error": "nie ma"})
            if parts[3] == "resume":
                if not status(d)["state"].startswith(("failed", "rejected", "przerwany")):  # "running" = żywy proces albo brak pewności: nie wznawiamy
                    return self.reply(409, {"error": "wznowić można tylko run zakończony błędem, odrzucony lub przerwany (bez żywego procesu)"})
                err = resume_run(d.name)
                return self.reply(500 if err else 200, {"error": err} if err else {"ok": True})
            pending = jread(d / "pending.json")
            if not pending:
                return self.reply(409, {"error": "brak oczekującej bramki"})
            b = self.body()
            if not isinstance(b, dict) or "approve" not in b:
                return self.reply(400, {"error": "zły JSON"})
            (d / "answer.json").write_text(json.dumps({"gate": pending["gate"], "approve": b["approve"] is True}), encoding="utf-8")
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
