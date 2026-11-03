import argparse
import json
import subprocess
import time
from pathlib import Path

from engines import run_agent

ROOT = Path(__file__).parent
# Bramki zależne od trybu. Merge zatwierdza człowiek zawsze (jeszcze nie zaimplementowany).
GATES = {"manual": {"spec", "plan"}, "guided": {"spec"}, "auto": set()}


def agent(cfg, role, prompt, workdir):
    engine = cfg["engines"][cfg["roles"][role]]
    template = (ROOT / "agents" / f"{role}.md").read_text(encoding="utf-8")
    return run_agent(engine, f"{template}\n\n{prompt}", workdir, cfg["limits"]["timeout_seconds"])


def approve(name, mode):
    if name not in GATES[mode]:
        return True
    return input(f"Zatwierdzić {name}? [t/n] ").strip().lower() == "t"


def verify(cmd, workdir):
    p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode == 0, (p.stdout + p.stderr)[-4000:]


def run(cfg, task, mode=None, base=None):
    """Intake -> bramka spec -> build/verify w pętli. Zwraca (stan, katalog runa)."""
    mode = mode or cfg["mode"]
    run_dir = (base or ROOT / "runs") / time.strftime("%Y%m%d-%H%M%S")
    work = run_dir / "workspace"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)

    def finish(state):
        (run_dir / "status.json").write_text(
            json.dumps({"state": state, "mode": mode, "task": task}, ensure_ascii=False), encoding="utf-8")
        return state, run_dir

    def log(text):
        print(text)
        with open(run_dir / "log.md", "a", encoding="utf-8") as f:
            f.write(text + "\n")

    log(f"[intake] {task}")
    ok, spec = agent(cfg, "intake", f"# Zadanie\n{task}", work)
    if not ok:
        log(spec)
        return finish("failed: intake")
    (run_dir / "spec.md").write_text(spec, encoding="utf-8")
    if not approve("spec", mode):
        return finish("rejected: spec")

    feedback = ""
    for i in range(1, cfg["limits"]["max_iterations"] + 1):
        log(f"[build {i}]")
        ok, out = agent(cfg, "builder", f"# Specyfikacja\n{spec}\n\n# Wynik poprzedniej weryfikacji\n{feedback}", work)
        if not ok:
            log(out)
            return finish("failed: builder")
        passed, feedback = verify(cfg["verify"], work)
        log(f"[verify {i}] {'OK' if passed else 'BŁĄD'}\n{feedback}")
        if passed:
            return finish("done")
    return finish("failed: limit iteracji")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CodeFactory")
    ap.add_argument("task", help="zadanie w języku naturalnym")
    ap.add_argument("--mode", choices=GATES)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()
    state, d = run(json.loads(Path(a.config).read_text(encoding="utf-8")), a.task, a.mode)
    print(f"{state} -> {d}")
