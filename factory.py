import argparse
import json
import subprocess
import time
from pathlib import Path

from engines import run_agent

ROOT = Path(__file__).parent
# Bramki zależne od trybu. Merge do repo docelowego zatwierdza człowiek zawsze (jeszcze nie zaimplementowany).
GATES = {"manual": {"spec", "plan"}, "guided": {"spec"}, "auto": set()}


def agent(cfg, role, prompt, workdir):
    engine = cfg["engines"][cfg["roles"][role]]
    template = (ROOT / "agents" / f"{role}.md").read_text(encoding="utf-8")
    return run_agent(engine, f"{template}\n\n{prompt}", workdir, cfg["limits"]["timeout_seconds"])


POLL = 0.5  # sekundy między sprawdzeniami odpowiedzi z GUI


def approve(name, mode, run_dir=None, how="terminal"):
    """Bramka dla człowieka. how="gui": pending.json -> czeka na answer.json zapisany przez gui.py."""
    if name not in GATES[mode]:
        return True
    if how != "gui":
        return input(f"Zatwierdzić {name}? [t/n] ").strip().lower() == "t"
    pending, answer = run_dir / "pending.json", run_dir / "answer.json"
    answer.unlink(missing_ok=True)
    pending.write_text(json.dumps({"gate": name}), encoding="utf-8")
    print(f"[gate] czekam na decyzję w GUI: {name}")
    while True:
        try:
            a = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            a = {}
        if a.get("gate") == name:
            break
        time.sleep(POLL)
    pending.unlink()
    answer.unlink()
    return a.get("approve") is True


def verify(cmd, workdir):
    p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode == 0, (p.stdout + p.stderr)[-4000:]


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=codefactory", "-c", "user.email=factory@localhost", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def parse_plan(text, max_tasks):
    """Wyciąga z odpowiedzi plannera listę zadań [{title, description}]. ValueError przy złym formacie."""
    try:
        tasks = json.loads(text[text.index("["): text.rindex("]") + 1])
    except ValueError as e:
        raise ValueError(f"planner nie zwrócił listy JSON: {e}")
    if not isinstance(tasks, list) or not 0 < len(tasks) <= max_tasks:
        raise ValueError(f"liczba zadań poza zakresem 1..{max_tasks}")
    for t in tasks:
        if not (isinstance(t, dict) and isinstance(t.get("title"), str) and isinstance(t.get("description"), str)):
            raise ValueError("zadanie bez title/description")
    return tasks


def build_task(cfg, log, spec, task, work, tid):
    """Jedno zadanie w osobnym worktree. Po zielonym verify+review ff-merge do głównej gałęzi.
    Zwraca None przy sukcesie albo tekst stanu błędu (worktree zostaje do oględzin)."""
    wt = work.parent / "wt" / tid
    git(work, "worktree", "add", "-q", "-b", f"task/{tid}", str(wt))
    brief = f"# Specyfikacja\n{spec}\n\n# Zadanie do wykonania teraz\n{task['title']}\n{task['description']}"
    feedback = ""
    for i in range(1, cfg["limits"]["max_iterations"] + 1):
        log(f"[{tid} build {i}]")
        ok, out = agent(cfg, "builder", f"{brief}\n\n# Wynik poprzedniej weryfikacji\n{feedback}", wt)
        if not ok:
            log(out)
            return "failed: builder"
        passed, feedback = verify(cfg["verify"], wt)
        log(f"[{tid} verify {i}] {'OK' if passed else 'BŁĄD'}\n{feedback}")
        if passed and "reviewer" in cfg["roles"]:
            ok, review = agent(cfg, "reviewer", brief, wt)
            if not ok:
                log(review)
                return "failed: reviewer"
            passed = review.lstrip().upper().startswith("VERDICT: PASS")
            feedback = f"Uwagi recenzenta:\n{review}"
            log(f"[{tid} review {i}] {'PASS' if passed else 'FAIL'}\n{review}")
        if passed:
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "--allow-empty", "-m", task["title"])
            git(work, "merge", "-q", "--ff-only", f"task/{tid}")
            git(work, "worktree", "remove", "--force", str(wt))
            git(work, "branch", "-q", "-d", f"task/{tid}")
            return None
    return "failed: limit iteracji"


def run(cfg, task, mode=None, base=None):
    """Intake -> spec -> (planner -> plan) -> zadania po kolei, każde w worktree. Zwraca (stan, katalog runa)."""
    mode = mode or cfg["mode"]
    run_dir = (base or ROOT / "runs") / time.strftime("%Y%m%d-%H%M%S")
    work = run_dir / "workspace"
    work.mkdir(parents=True)
    git(work, "init", "-q")
    git(work, "commit", "-q", "--allow-empty", "-m", "init")  # worktree wymaga commita
    with open(work / ".git" / "info" / "exclude", "a") as f:
        f.write("__pycache__/\n")

    def set_state(state):
        (run_dir / "status.json").write_text(
            json.dumps({"state": state, "mode": mode, "task": task}, ensure_ascii=False), encoding="utf-8")

    def finish(state):
        set_state(state)
        return state, run_dir

    how = cfg.get("approval", "terminal")
    set_state("running")

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
    if not approve("spec", mode, run_dir, how):
        return finish("rejected: spec")

    tasks = [{"title": "Całość", "description": "Zaimplementuj całą specyfikację."}]
    if "planner" in cfg["roles"]:
        ok, out = agent(cfg, "planner", f"# Specyfikacja\n{spec}", work)
        try:
            if not ok:
                raise ValueError(out)
            tasks = parse_plan(out, cfg["limits"].get("max_tasks", 8))
        except ValueError as e:
            log(f"[plan] {e}")
            return finish("failed: planner")
        plan = "\n".join(f"{n}. **{t['title']}**: {t['description']}" for n, t in enumerate(tasks, 1))
        (run_dir / "tasks.md").write_text(plan, encoding="utf-8")
        log(f"[plan]\n{plan}")
        if not approve("plan", mode, run_dir, how):
            return finish("rejected: plan")

    for n, t in enumerate(tasks, 1):
        err = build_task(cfg, log, spec, t, work, f"t{n}")
        if err:
            return finish(err)
    return finish("done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CodeFactory")
    ap.add_argument("task", help="zadanie w języku naturalnym")
    ap.add_argument("--mode", choices=GATES)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()
    state, d = run(json.loads(Path(a.config).read_text(encoding="utf-8")), a.task, a.mode)
    print(f"{state} -> {d}")
