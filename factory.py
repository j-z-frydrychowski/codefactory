import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from engines import run_agent

ROOT = Path(__file__).parent
LOCK = threading.Lock()  # operacje na wspólnym repo: worktree add/remove i merge do głównej gałęzi
GIT = ["git", "-c", "user.name=codefactory", "-c", "user.email=factory@localhost"]
# Bramki zależne od trybu. Merge do repo docelowego zatwierdza człowiek zawsze (jeszcze nie zaimplementowany).
GATES = {"manual": {"spec", "plan"}, "guided": {"spec"}, "auto": set()}


def agent(cfg, role, prompt, workdir):
    engine = cfg["engines"][cfg["roles"][role]]
    template = (ROOT / "agents" / f"{role}.md").read_text(encoding="utf-8")
    note = f"# Komenda weryfikacji projektu\n`{cfg['verify']}` (uruchamiana w katalogu projektu; testy i kryteria muszą się z nią zgadzać)"
    return run_agent(engine, f"{template}\n\n{prompt}\n\n{note}", workdir, cfg["limits"]["timeout_seconds"])


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


def verify_is_meaningful(cmd):
    """Komenda weryfikacji nie może przechodzić na pustym projekcie (np. `node --test` bez testów kończy się kodem 0)."""
    with tempfile.TemporaryDirectory() as empty:
        return not verify(cmd, empty)[0]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8").stdout


def git_try(cwd, *args):
    """Jak git(), ale bez wyjątku: zwraca (kod wyjścia, wyjście)."""
    p = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


def parse_plan(text, max_tasks):
    """Wyciąga z odpowiedzi plannera listę zadań [{title, description, depends_on}]. ValueError przy złym formacie.
    depends_on to numery wcześniejszych zadań (od 1); brak pola = zależy od poprzedniego (bezpieczne, sekwencyjne)."""
    try:
        tasks = json.loads(text[text.index("["): text.rindex("]") + 1])
    except ValueError as e:
        raise ValueError(f"planner nie zwrócił listy JSON: {e}")
    if not isinstance(tasks, list) or not 0 < len(tasks) <= max_tasks:
        raise ValueError(f"liczba zadań poza zakresem 1..{max_tasks}")
    for n, t in enumerate(tasks, 1):
        if not (isinstance(t, dict) and isinstance(t.get("title"), str) and isinstance(t.get("description"), str)):
            raise ValueError("zadanie bez title/description")
        deps = t.get("depends_on", [n - 1] if n > 1 else [])
        if not (isinstance(deps, list) and all(type(d) is int and 1 <= d < n for d in deps)):
            raise ValueError(f"zadanie {n}: depends_on musi wskazywać wcześniejsze zadania")
        t["depends_on"] = sorted(set(deps))
    return tasks


def build_task(cfg, log, spec, task, work, tid, base):
    """Jedno zadanie w osobnym worktree. Po zielonym verify+review ff-merge do gałęzi base.
    Gdy base poszła do przodu (równoległe zadanie), dociąga ją do worktree i weryfikuje całość od nowa;
    konflikt wraca do buildera. Zwraca None przy sukcesie albo tekst stanu błędu (worktree zostaje do oględzin)."""
    wt = work.parent / "wt" / tid
    branch = f"task/{tid}"
    with LOCK:
        # po przerwanym runie zostają worktree i gałąź z poprzedniej próby: zadanie startuje od zera
        git_try(work, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
        git_try(work, "worktree", "prune")
        git_try(work, "branch", "-D", branch)
        git(work, "worktree", "add", "-q", "-b", branch, str(wt))
    brief = f"# Specyfikacja\n{spec}\n\n# Zadanie do wykonania teraz\n{task['title']}\n{task['description']}"
    feedback, rebuild = "", True
    for i in range(1, cfg["limits"]["max_iterations"] + 1):
        if rebuild:
            log(f"[{tid} build {i}]")
            ok, out = agent(cfg, "builder", f"{brief}\n\n# Wynik poprzedniej weryfikacji\n{feedback}", wt)
            if not ok:
                log(out)
                return "failed: builder"
        passed, feedback = verify(cfg["verify"], wt)
        log(f"[{tid} verify {i}] {'OK' if passed else 'BŁĄD'}\n{feedback}")
        if passed and git_try(wt, "grep", "-q", "--untracked", "^<<<<<<< ")[0] == 0:
            passed, feedback = False, "W plikach zostały znaczniki konfliktu <<<<<<<. Usuń je, zachowując sens obu stron."
        if passed and "reviewer" in cfg["roles"]:
            ok, review = agent(cfg, "reviewer", brief, wt)
            if not ok:
                log(review)
                return "failed: reviewer"
            passed = review.lstrip().upper().startswith("VERDICT: PASS")
            feedback = f"Uwagi recenzenta:\n{review}"
            log(f"[{tid} review {i}] {'PASS' if passed else 'FAIL'}\n{review}")
        if not passed:
            rebuild = True
            continue
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "--allow-empty", "-m", task["title"])
        with LOCK:
            if git_try(work, "merge", "-q", "--ff-only", branch)[0] == 0:
                # zadanie jest już scalone: sprzątanie jest best-effort (na Windowsie pozostały po przerwanym runie proces
                # potrafi trzymać katalog), a jego porażka nie może unieważnić wyniku
                git_try(work, "worktree", "remove", "--force", str(wt))
                shutil.rmtree(wt, ignore_errors=True)
                git_try(work, "worktree", "prune")
                git_try(work, "branch", "-q", "-D", branch)
                return None
        if git_try(wt, "merge", "--no-edit", base)[0] == 0:
            rebuild, feedback = False, ""
            log(f"[{tid} merge] dociągnięto zmiany z {base}, ponowna weryfikacja")
        else:
            rebuild = True
            feedback = ("Konflikt scalania ze zmianami innych zadań w plikach:\n"
                        + git(wt, "diff", "--name-only", "--diff-filter=U")
                        + "Rozwiąż konflikty, zachowaj działanie obu stron i dopilnuj, żeby testy przechodziły.")
            log(f"[{tid} merge] konflikt\n{feedback}")
    return "failed: limit iteracji"


def run_tasks(tasks, run_one, max_parallel, done=()):
    """Startuje zadanie, gdy wszystkie jego depends_on są gotowe, maks. max_parallel naraz.
    `done` to zadania już wykonane (wznowienie). Po pierwszym błędzie nie startuje nowych (trwające dokańcza).
    Zwraca pierwszy błąd albo None."""
    done, started, running, err = set(done), set(done), {}, None
    with ThreadPoolExecutor(max_parallel) as ex:
        while True:
            for n, t in enumerate(tasks, 1):
                if err or len(running) >= max_parallel:
                    break
                if n not in started and set(t["depends_on"]) <= done:
                    started.add(n)
                    running[ex.submit(run_one, n, t)] = n
            if not running:
                return err
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in finished:
                n = running.pop(f)
                if f.result():
                    err = err or f.result()
                else:
                    done.add(n)


def jload(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run(cfg, task, mode=None, base=None):
    """Nowy run: katalog + repo, potem execute(). Zwraca (stan, katalog runa)."""
    run_dir = (base or ROOT / "runs") / time.strftime("%Y%m%d-%H%M%S")
    work = run_dir / "workspace"
    work.mkdir(parents=True)
    git(work, "init", "-q")
    git(work, "commit", "-q", "--allow-empty", "-m", "init")  # worktree wymaga commita
    with open(work / ".git" / "info" / "exclude", "a") as f:
        f.write("__pycache__/\n")
    return execute(cfg, run_dir, task, mode or cfg["mode"])


def resume(cfg, run_dir, mode=None):
    """Wznawia przerwany lub nieudany run od miejsca, w którym stanął: pomija zatwierdzone etapy i gotowe zadania,
    zadania nieukończone robi od zera. Nie wznawiaj runu, który jeszcze działa (nie ma na to zabezpieczenia)."""
    st = jload(run_dir / "status.json")
    if not st or not (run_dir / "workspace" / ".git").exists():
        raise ValueError(f"{run_dir} nie wygląda na run fabryki")
    if st["state"] == "done":
        return "done", run_dir
    return execute(cfg, run_dir, st["task"], mode or st["mode"])


def execute(cfg, run_dir, task, mode):
    """Intake -> spec -> (planner -> plan) -> zadania wg zależności, każde w worktree.
    Postęp (zatwierdzenia, plan, gotowe zadania) w progress.json umożliwia wznowienie."""
    work = run_dir / "workspace"
    progress = jload(run_dir / "progress.json") or {}
    cfg = {**cfg, "verify": progress.get("verify", cfg["verify"])}  # wznowienie używa tej samej komendy co pierwszy start
    progress_lock = threading.Lock()

    def save(**kw):
        with progress_lock:
            progress.update(kw)
            (run_dir / "progress.json").write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")

    def set_state(state):
        (run_dir / "status.json").write_text(
            json.dumps({"state": state, "mode": mode, "task": task}, ensure_ascii=False), encoding="utf-8")

    def finish(state):
        set_state(state)
        return state, run_dir

    how = cfg.get("approval", "terminal")
    set_state("running")

    log_lock = threading.Lock()

    def log(text):
        with log_lock:
            try:
                print(text)
            except UnicodeEncodeError:  # konsola/plik w cp1250: znak spoza strony kodowej nie może przerwać runu
                enc = sys.stdout.encoding or "ascii"
                print(text.encode(enc, "replace").decode(enc))
            with open(run_dir / "log.md", "a", encoding="utf-8") as f:
                f.write(text + "\n")

    if cfg.get("verify_canary", True) and not verify_is_meaningful(cfg["verify"]):
        log(f"[verify] komenda `{cfg['verify']}` przechodzi na pustym projekcie, więc niczego nie sprawdza. Popraw ją.")
        return finish("failed: verify")
    save(verify=cfg["verify"])
    spec_file = run_dir / "spec.md"
    if spec_file.exists():
        spec = spec_file.read_text(encoding="utf-8")
        log("[wznowienie] spec z poprzedniej próby")
    else:
        log(f"[intake] {task}")
        ok, spec = agent(cfg, "intake", f"# Zadanie\n{task}", work)
        if not ok:
            log(spec)
            return finish("failed: intake")
        if "spec_check" in cfg["roles"]:  # jedna poprawka: kontroler szuka sprzeczności, intake poprawia
            ok, check = agent(cfg, "spec_check", f"# Zadanie\n{task}\n\n# Specyfikacja\n{spec}", work)
            if not ok:
                log(check)
                return finish("failed: spec_check")
            passed = check.lstrip().upper().startswith("VERDICT: PASS")
            log(f"[spec_check] {'PASS' if passed else 'FAIL'}\n{check}")
            if not passed:
                ok, spec = agent(cfg, "intake", f"# Zadanie\n{task}\n\n# Poprzednia specyfikacja\n{spec}\n\n"
                                 f"# Problemy do usunięcia\n{check}", work)
                if not ok:
                    log(spec)
                    return finish("failed: intake")
        spec_file.write_text(spec, encoding="utf-8")
    if not progress.get("spec_ok"):
        if not approve("spec", mode, run_dir, how):
            return finish("rejected: spec")
        save(spec_ok=True)

    tasks = [{"title": "Całość", "description": "Zaimplementuj całą specyfikację.", "depends_on": []}]
    if "planner" in cfg["roles"]:
        if "plan" in progress:
            tasks = progress["plan"]
        else:
            ok, out = agent(cfg, "planner", f"# Specyfikacja\n{spec}", work)
            try:
                if not ok:
                    raise ValueError(out)
                tasks = parse_plan(out, cfg["limits"].get("max_tasks", 8))
            except ValueError as e:
                log(f"[plan] {e}")
                return finish("failed: planner")
            plan = "\n".join(
                f"{n}. **{t['title']}**{' (po: ' + ', '.join(map(str, t['depends_on'])) + ')' if t['depends_on'] else ''}: {t['description']}"
                for n, t in enumerate(tasks, 1))
            (run_dir / "tasks.md").write_text(plan, encoding="utf-8")
            log(f"[plan]\n{plan}")
            save(plan=tasks)
        if not progress.get("plan_ok"):
            if not approve("plan", mode, run_dir, how):
                return finish("rejected: plan")
            save(plan_ok=True)

    base = git(work, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def run_one(n, t):
        try:
            err = build_task(cfg, log, spec, t, work, f"t{n}", base)
        except Exception as e:  # np. błąd gita: run ma się zakończyć stanem, nie zawisnąć na "running"
            log(f"[t{n}] {type(e).__name__}: {e}")
            return "failed: wyjątek"
        if not err:
            save(done=sorted({*progress.get("done", []), n}))
        return err

    return finish(run_tasks(tasks, run_one, cfg["limits"].get("max_parallel", 1), progress.get("done", [])) or "done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CodeFactory")
    ap.add_argument("task", nargs="?", help="zadanie w języku naturalnym")
    ap.add_argument("--resume", metavar="RUN", help="wznów run (id z runs/ albo ścieżka) zamiast zaczynać nowy")
    ap.add_argument("--mode", choices=GATES)
    ap.add_argument("--approval", choices=["terminal", "gui"], help="gdzie zatwierdzać bramki (domyślnie z configu)")
    ap.add_argument("--verify", help="komenda weryfikacji albo nazwa presetu z configu (verify_presets); dla nowego runu")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()
    if bool(a.task) == bool(a.resume):
        ap.error("podaj zadanie albo --resume RUN (jedno z dwóch)")
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    cfg["approval"] = a.approval or cfg.get("approval", "terminal")
    if a.verify:
        cfg["verify"] = cfg.get("verify_presets", {}).get(a.verify, a.verify).replace("{root}", ROOT.as_posix())
    if a.resume:
        state, d = resume(cfg, Path(a.resume) if Path(a.resume).is_dir() else ROOT / "runs" / a.resume, a.mode)
    else:
        state, d = run(cfg, a.task, a.mode)
    print(f"{state} -> {d}")
