import shlex
import shutil
import subprocess


def run_agent(engine, prompt, workdir, timeout=900):
    """Uruchamia agenta w workdir. Zwraca (ok, tekst)."""
    if engine["type"] == "cli":
        return _cli(engine, prompt, workdir, timeout)
    raise NotImplementedError(f"silnik '{engine['type']}' jeszcze nie istnieje")


def _cli(engine, prompt, workdir, timeout):
    argv = shlex.split(engine["command"])
    argv[0] = shutil.which(argv[0]) or argv[0]  # Windows: claude.cmd itp.
    try:
        p = subprocess.run(argv, input=prompt, cwd=workdir, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{type(e).__name__}: {e}"
    return p.returncode == 0, p.stdout if p.returncode == 0 else p.stdout + p.stderr
