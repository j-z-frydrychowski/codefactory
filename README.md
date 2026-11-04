# CodeFactory 1.0

**Opisujesz zadanie zwykłym językiem, a agenci kodujący sami je specyfikują, planują, budują, testują i recenzują.**
Ty zatwierdzasz tylko to, co ważne.

```
python factory.py "Napisz skrypt, który liczy słowa w pliku tekstowym"
```

Fabryka to jeden folder i Python (biblioteka standardowa, zero zależności). Nie jest przywiązana do jednego dostawcy:
każdy w zespole może podpiąć własne narzędzie CLI (Claude Code, Codex CLI, Gemini CLI, …), a później także lokalne
modele przez API.

## Spis treści

1. [Szybki start](#szybki-start)
2. [Jak to działa](#jak-to-działa)
3. [Tryby autonomii](#tryby-autonomii)
4. [Uruchamianie: terminal i GUI](#uruchamianie-terminal-i-gui)
5. [Weryfikacja: skąd wiadomo, że kod działa](#weryfikacja-skąd-wiadomo-że-kod-działa)
6. [Wznawianie przerwanych runów](#wznawianie-przerwanych-runów)
7. [Merge do własnego repozytorium](#merge-do-własnego-repozytorium)
8. [Konfiguracja](#konfiguracja)
9. [Silniki](#silniki)
10. [Zasady bezpieczeństwa](#zasady-bezpieczeństwa)
11. [Struktura projektu](#struktura-projektu)
12. [Jak pracować nad fabryką](#jak-pracować-nad-fabryką)
13. [Znane ograniczenia](#znane-ograniczenia)
14. [Pomysły na dalej](#pomysły-na-dalej)

---

## Szybki start

**Potrzebujesz:** Python 3.12+, Git i zalogowanego narzędzia CLI z agentem (domyślnie `claude`, musi być w `PATH`).
Node.js tylko wtedy, gdy budujesz projekty JavaScript z presetem `node`.

```bash
# 1. Sprawdź, czy fabryka działa na Twoim komputerze (bez modelu, około 30 s). Ma zwrócić kod 0.
python -m unittest test_factory test_gui test_engines

# 2. Pierwszy prawdziwy run (kilka minut, używa modelu)
python factory.py --mode auto "Napisz funkcję count_words(text) w words.py zliczającą słowa i testy do niej"

# 3. Zobacz wynik
#    runs/<data-i-godzina>/workspace/   gotowy projekt
#    runs/<data-i-godzina>/log.md       co się działo
```

> **Windows:** przed uruchomieniem ustaw `$env:PYTHONIOENCODING = "utf-8"` (PowerShell).
> **macOS/Linux:** konfiguracja używa polecenia `python`; jeśli masz tylko `python3`, zrób dowiązanie
> albo zamień `python` na `python3` w `config.json`.
> Testy wypisują własne „FAILED" z wewnętrznych przebiegów, więc wynik czytaj z kodu wyjścia, nie z tekstu.

## Jak to działa

```
 Twoje zadanie
      │
      ▼
 1. INTAKE       zadanie → spec.md (cel, zakres, założenia, kryteria akceptacji)
      │
      ▼
 2. SPEC_CHECK   kontroler szuka sprzeczności w specyfikacji (opcjonalny)
      │          ── bramka: zatwierdzasz spec ──
      ▼
 3. PLAN         spec → lista małych zadań z zależnościami
      │          ── bramka: zatwierdzasz plan (tylko tryb manual) ──
      ▼
 4. BUILD        każde zadanie w osobnym git worktree, niezależne równolegle
      ▼
 5. VERIFY       twarda komenda (testy), kod wyjścia decyduje
      ▼
 6. REVIEW       drugi agent, tylko do odczytu: VERDICT: PASS albo FAIL
      │          FAIL wraca do BUILD (maks. N razy)
      ▼
 7. MERGE        zadanie wchodzi do głównej gałęzi workspace
      ▼
 8. DELIVER      ── bramka: zatwierdzasz merge do Twojego repo (zawsze) ──
```

Najważniejsze decyzje projektowe:

- **Stan jest w plikach, nie w pamięci.** Każdy krok zostawia ślad w `runs/<id>/`, więc po awarii można wznowić.
- **O przejściu dalej decydują testy, nie opinia agenta.** Recenzent jest dodatkową siatką, nie sędzią.
- **Builder i recenzent to osobne wywołania** (najlepiej różne modele), recenzent nie ma prawa edytować.
- **Każde zadanie ma własny worktree.** Zadania bez zależności budują się równolegle (limit `max_parallel`),
  a konflikty przy scalaniu wracają do buildera do rozwiązania.

## Tryby autonomii

| Tryb     | Człowiek zatwierdza | Do czego                                         |
|----------|---------------------|--------------------------------------------------|
| `manual` | spec, plan i merge  | nowy, ważny projekt, chcesz kontrolować wszystko |
| `guided` | spec i merge        | domyślny, sensowny kompromis                     |
| `auto`   | tylko merge         | małe zadania, ufasz testom                       |

Dwie rzeczy obowiązują zawsze, w każdym trybie:

- **Merge do Twojego repo zatwierdza człowiek.** Trybu bez bramek („YOLO") świadomie nie ma.
- **Jeśli kontroler specyfikacji znajdzie sprzeczność,** run staje ze statusem `needs human: spec`, nawet w `auto`.
  Odrzucenie kończy run jako `rejected: spec` i nic się nie buduje.

Tryb wybierasz flagą `--mode manual|guided|auto` albo w `config.json`.

## Uruchamianie: terminal i GUI

### Terminal

```bash
python factory.py "opis zadania"                 # tryb z config.json
python factory.py --mode auto "opis zadania"
python factory.py --verify node "zrób stronę z licznikiem"
python factory.py --target ../moje-repo "opis"   # z merge do Twojego repo
python factory.py --resume 20261104-110250        # wznów przerwany run
```

Bramki pytają w terminalu (`t` = tak).

### GUI

```bash
python gui.py          # http://127.0.0.1:8765, tylko localhost
```

Podgląd na żywo: lista runów, etapy, spec, plan, log, status. Z GUI możesz też **startować runy**
(formularz u góry: zadanie, tryb, preset weryfikacji), **zatwierdzać bramki** (spec, plan, merge przyciskiem)
oraz **wznawiać** runy nieudane i przerwane. Runy odpalone z GUI mają bramki zawsze w GUI. Żeby ten sam efekt mieć
z terminala, ustaw `"approval": "gui"` w `config.json`.

GUI przyjmuje tylko nazwy presetów weryfikacji, nigdy surowych komend z przeglądarki.

## Weryfikacja: skąd wiadomo, że kod działa

Weryfikacja to **komenda uruchamiana w katalogu projektu**; jej kod wyjścia decyduje o przejściu dalej.
Agenci widzą tę komendę w każdym prompcie, więc piszą testy zgodne z nią.

| Preset   | Komenda                                         | Dla                      |
|----------|-------------------------------------------------|--------------------------|
| `python` | `python -m unittest discover`                   | skrypty i aplikacje Pythona |
| `node`   | `python tools/nonempty.py node --test`          | strony i skrypty JS      |

Wybierasz: `--verify <preset lub komenda>` albo lista w GUI. Własne presety dopisujesz w `config.json`.

**Zabezpieczenie przed pustą weryfikacją.** Przed startem fabryka sprawdza, czy komenda **nie przechodzi na pustym
projekcie** (np. `node --test` bez testów kończy się kodem 0 i niczego nie dowodzi). `tools/nonempty.py` opakowuje
komendę i wymusza, że jakiś test faktycznie się uruchomił. Wyłączysz kontrolę przez `"verify_canary": false`
(nie polecamy).

Uwaga dla Pythona starszego niż 3.12: `unittest discover` bez testów też kończy się kodem 0 i kontrola go odrzuci.
Użyj wtedy `python -m pytest`.

## Wznawianie przerwanych runów

Run można zatrzymać (awaria, zamknięty terminal, kill) i wznowić:

```bash
python factory.py --resume <id z runs/>
```

Wznowienie pomija zatwierdzone etapy i gotowe zadania. Zadania nieukończone robi od zera. Zadanie, które zdążyło
się scalić tuż przed śmiercią procesu, jest rozpoznawane po commicie i nie powstaje drugi raz. Specyfikację możesz
przed wznowieniem poprawić ręcznie w `runs/<id>/spec.md`. Komenda weryfikacji jest zapisana w runie i zostaje ta sama.

Ochrona: działający run trzyma PID w `runs/<id>/pid`, a `--resume` odmawia, gdy ten proces żyje. W GUI run zabity
w trakcie widać jako „PRZERWANY" z przyciskiem „Wznów run".

## Merge do własnego repozytorium

Domyślnie wynik zostaje w `runs/<id>/workspace`. Żeby trafił do Twojego projektu:

```bash
python factory.py --target C:\sciezka\do\mojego-repo "opis zadania"
```

(albo `"target"` w `config.json`; argument ma pierwszeństwo). Po zakończeniu fabryka pokazuje commity i diffstat
i pyta o zgodę, **zawsze**, także w `auto`. Po zatwierdzeniu:

1. sprawdza, czy target to repo git z **czystym** drzewem roboczym (inaczej odmawia i niczego nie zmienia),
2. pobiera wynik do gałęzi `factory/<id runu>`,
3. robi `git merge --no-ff` w aktualnej gałęzi targetu (z `--allow-unrelated-histories`, bo workspace ma własną historię).

Wyniki: sukces (`done`, `merged=true`), odrzucenie (`rejected: merge`, target nietknięty), konflikt
(`failed: merge`, `merge --abort`, gałąź `factory/<id>` zostaje do ręcznego rozwiązania).

## Konfiguracja

Wszystko w jednym pliku `config.json`:

```json
{
  "mode": "guided",
  "engines": {
    "claude":          { "type": "cli", "command": "claude -p --permission-mode acceptEdits" },
    "claude-readonly": { "type": "cli", "command": "claude -p --allowedTools Read Grep Glob" }
  },
  "roles": {
    "intake": "claude", "spec_check": "claude-readonly", "builder": "claude",
    "planner": "claude-readonly", "reviewer": "claude-readonly"
  },
  "verify": "python -m unittest discover",
  "limits": { "max_iterations": 3, "max_tasks": 8, "max_parallel": 3, "timeout_seconds": 900 }
}
```

| Klucz                  | Znaczenie                                                                    |
|------------------------|------------------------------------------------------------------------------|
| `mode`                 | `manual` / `guided` / `auto`                                                 |
| `engines`              | nazwane silniki (patrz niżej)                                                |
| `roles`                | która rola używa którego silnika; **brak roli = jej krok jest pomijany** (dotyczy `spec_check`, `planner`, `reviewer`) |
| `verify`, `verify_presets` | komenda weryfikacji domyślna i gotowe presety                           |
| `limits`               | maks. iteracji fix-loop, maks. zadań w planie, równoległość, timeout agenta (s) |
| `approval`             | `terminal` (domyślnie) lub `gui`                                             |
| `target`               | domyślne repo do merge                                                       |

Prompty ról leżą w `agents/*.md` i można je swobodnie poprawiać.

## Silniki

Rola wywołuje „silnik" przez wspólny interfejs `run_agent(engine, prompt, workdir, timeout)`.

| Typ   | Przykłady                                  | Pętlę „czytaj/edytuj/uruchom" robi |
|-------|--------------------------------------------|------------------------------------|
| `cli` | Claude Code, Codex CLI, Gemini CLI, inne   | samo narzędzie                     |
| `api` | lokalne LLM-y (Ollama, vLLM, LM Studio)    | fabryka (własna pętla narzędzi)    |

- **`cli`:** prompt trafia na stdin komendy z konfiguracji. Zespół podmienia komendę na swoje narzędzie.
- **`api`:** format OpenAI-compatible, więc jeden adapter obsługuje większość lokalnych serwerów. Klucze tylko
  ze zmiennych środowiskowych (`key_env`). **Obecnie wyłączony** flagą `API_ENABLED = False` w `engines.py`,
  bo nie był jeszcze sprawdzony na prawdziwym modelu.

## Zasady bezpieczeństwa

- Agenci pracują w workspace i worktree, a nie w Twoim repo. Do repo trafia tylko zatwierdzony merge.
- Recenzent i planner działają w trybie tylko do odczytu.
- Klucze API tylko ze zmiennych środowiskowych, nigdy z repo (`.env` jest w `.gitignore`).
- GUI słucha wyłącznie na `127.0.0.1`, sprawdza nagłówek `Origin` przy zapisach, waliduje id runów i nie przyjmuje
  surowych komend z przeglądarki.
- Silnik `api`: lista dozwolonych komend **nie jest piaskownicą**. `python` na liście pozwala uruchomić dowolny kod.
  Przed użyciem w pracy potrzebna jest prawdziwa izolacja (kontener).

## Struktura projektu

```
factory.py        pętla główna: intake → plan → zadania w worktree → verify → review → merge
engines.py        adaptery silników (cli, api)
gui.py, gui.html  podgląd na żywo i zatwierdzanie bramek
config.json       silniki, role, tryb, limity, presety weryfikacji
agents/           prompty ról: intake, spec_check, planner, builder, reviewer
tools/nonempty.py opakowanie komendy testów, które wymusza, że jakiś test się uruchomił
test_*.py         testy fabryki, GUI i silników (bez prawdziwego modelu)
runs/<id>/        stan jednego runu (poniżej)
```

Co znajdziesz w `runs/<id>/`:

| Plik / folder    | Zawartość                                                    |
|------------------|--------------------------------------------------------------|
| `spec.md`        | specyfikacja z intake                                        |
| `tasks.md`       | plan: zadania i ich zależności                               |
| `log.md`         | pełny dziennik runu, tu szukaj przyczyn błędów               |
| `status.json`    | stan: `running`, `needs human: …`, `done`, `failed: …`, `rejected: …` |
| `progress.json`  | co jest zatwierdzone i zrobione (podstawa wznowienia)        |
| `pid`            | PID działającego procesu (znika po zakończeniu)              |
| `pending.json`, `answer.json` | komunikacja bramek z GUI                        |
| `workspace/`     | projekt budowany przez agentów (repo git)                    |
| `wt/`            | tymczasowe worktree zadań                                    |

## Jak pracować nad fabryką

Zapraszamy do ulepszania. Kilka wskazówek:

- **Testy:** `python -m unittest test_factory test_gui test_engines`. Wszystkie używają atrap agentów i działają
  bez modelu. Wynik oceniaj po kodzie wyjścia.
- **Nowa funkcja = test.** Wzorzec jest w `test_factory.py`: funkcja `fake(...)` udaje agenta, a `go(...)` uruchamia
  cały run na tymczasowym katalogu. Dobrze jest sprawdzić, że nowy test **pada bez poprawki**.
- **Zmiana zachowania agentów** zwykle oznacza edycję pliku w `agents/`, a nie kodu.
- **Nowy silnik CLI:** wystarczy nowy wpis w `engines` i przypisanie roli w `roles`. Jeśli narzędzie nie czyta promptu
  ze stdin, trzeba rozszerzyć `_cli` w `engines.py`.
- **Styl:** komentarze i komunikaty po polsku, kod prosty, bez zależności spoza biblioteki standardowej.
- **Zasada na całe życie projektu:** nie dodawać trybu bez bramki merge.

## Znane ograniczenia

Uczciwa lista tego, czego fabryka jeszcze nie robi lub nie sprawdziła:

- Silnik `api` nie był uruchomiony na prawdziwym modelu (stąd wyłączony).
- Sprawdzone tylko na Windowsie. macOS i Linux oraz inne narzędzia CLI niż `claude` czekają na test w zespole.
- Zabicie fabryki nie zabija procesów agentów, które uruchomiła. Mogą chwilę trzymać katalogi worktree.
- Dwa zadania w jednym planie o identycznym tytule mogą zostać pomylone przy wznowieniu.
- Planner często scala małe zadania w jedno, więc równoległość bywa niewykorzystana.
- Kontroler specyfikacji poprawia ją tylko raz i nie sprawdza poprawionej wersji (decyduje człowiek na bramce).
- Testy węzłów DOM w stronach HTML nie są pokryte; `node --test` sprawdza logikę, nie podpięcie przycisków.
- Podgląd w bramce merge pokazuje cały wynik runu, a nie różnicę względem targetu.
- Ponowne użycie PID przez obcy proces może sprawić, że martwy run wygląda na żywy.

## Pomysły na dalej

1. Prawdziwy test silnika `api` na lokalnym modelu, potem odblokowanie flagi i izolacja w kontenerze.
2. Test na macOS/Linux i z innymi narzędziami CLI (Codex, Gemini).
3. Licznik tokenów i kosztów na run.
4. Drugi przebieg kontroli specyfikacji po poprawce.
5. Testy DOM dla stron (np. przeglądarka headless).
6. Zabijanie procesów agentów razem z fabryką (Job Object na Windows, grupa procesów gdzie indziej).
7. Dystrybucja dla zespołu: zdalne repozytorium i instrukcja instalacji.
