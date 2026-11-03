import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path


API_ENABLED = False  # wyłączony do czasu testu na prawdziwym modelu (README: kolejność prac)


def run_agent(engine, prompt, workdir, timeout=900):
    """Uruchamia agenta w workdir. Zwraca (ok, tekst)."""
    if engine["type"] == "cli":
        return _cli(engine, prompt, workdir, timeout)
    if engine["type"] == "api":
        if not API_ENABLED:
            return False, "silnik 'api' jest wyłączony (engines.API_ENABLED), użyj silnika 'cli'"
        return _api(engine, prompt, workdir, timeout)
    raise NotImplementedError(f"silnik '{engine['type']}' nie istnieje")


def _cli(engine, prompt, workdir, timeout):
    argv = shlex.split(engine["command"])
    argv[0] = shutil.which(argv[0]) or argv[0]  # Windows: claude.cmd itp.
    try:
        p = subprocess.run(argv, input=prompt, cwd=workdir, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, p.stdout if p.returncode == 0 else p.stdout + p.stderr


# --- silnik api: serwer OpenAI-compatible + własna pętla narzędzi ---

def _tool(name, description, **params):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {k: {"type": "string"} for k in params},
                       "required": list(params)}}}


def _tool_defs(engine):
    tools = [_tool("list_files", "Lista plików w katalogu roboczym."),
             _tool("read_file", "Zwraca zawartość pliku.", path="ścieżka względna")]
    if not engine.get("readonly"):
        tools.append(_tool("write_file", "Zapisuje plik (nadpisuje).", path="ścieżka względna", content="treść"))
        if engine.get("allowed_commands"):
            tools.append(_tool("run_command", "Uruchamia komendę z listy dozwolonych.", command="np. python -m unittest"))
    return tools


def _safe_path(root, rel):
    p = (root / rel).resolve()
    p.relative_to(root)  # ValueError, gdy poza katalogiem roboczym
    if ".git" in p.relative_to(root).parts:
        raise ValueError(".git jest zablokowany")
    return p


def _exec_tool(root, engine, name, args):
    """Wykonuje narzędzie w root. Błędy wracają do modelu jako tekst, nie wyjątek."""
    allowed = {t["function"]["name"] for t in _tool_defs(engine)}
    try:
        if name not in allowed:
            return f"błąd: narzędzie '{name}' niedostępne"
        if name == "list_files":
            files = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                           if p.is_file() and ".git" not in p.relative_to(root).parts)
            return "\n".join(files[:500])
        if name == "read_file":
            return _safe_path(root, args["path"]).read_text(encoding="utf-8", errors="replace")[:20000]
        if name == "write_file":
            p = _safe_path(root, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return "zapisano"
        argv = shlex.split(args["command"])
        if not argv or argv[0] not in engine["allowed_commands"]:
            return f"błąd: komenda spoza listy dozwolonych {engine['allowed_commands']}"
        argv[0] = shutil.which(argv[0]) or argv[0]
        p = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        return f"kod wyjścia {p.returncode}\n{(p.stdout + p.stderr)[-4000:]}"
    except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
        return f"błąd: {type(e).__name__}: {e}"


def _api(engine, prompt, workdir, timeout):
    # shortcut: allowed_commands to lista prefiksów, nie sandbox ("python" uruchomi dowolny kod); izolacja = kontener, przed produkcją
    root = Path(workdir).resolve()
    headers = {"Content-Type": "application/json"}
    if engine.get("key_env"):
        if engine["key_env"] not in os.environ:
            return False, f"brak zmiennej środowiskowej {engine['key_env']}"
        headers["Authorization"] = f"Bearer {os.environ[engine['key_env']]}"
    url = engine["base_url"].rstrip("/") + "/chat/completions"
    messages = [{"role": "user", "content": prompt}]
    tools = _tool_defs(engine)
    deadline = time.monotonic() + timeout
    for _ in range(engine.get("max_steps", 30)):
        left = deadline - time.monotonic()
        if left <= 0:
            return False, "przekroczono limit czasu"
        body = json.dumps({"model": engine["model"], "messages": messages, "tools": tools}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=left) as r:
                msg = json.load(r)["choices"][0]["message"]
        except (OSError, ValueError, KeyError, IndexError) as e:
            return False, f"{type(e).__name__}: {e}"
        messages.append(msg)
        calls = msg.get("tool_calls")
        if not calls:
            return True, msg.get("content") or ""
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
                result = _exec_tool(root, engine, c["function"]["name"], args)
            except (KeyError, TypeError, ValueError) as e:
                result = f"błąd: zły format wywołania ({type(e).__name__})"
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": result})
    return False, "limit kroków silnika"
