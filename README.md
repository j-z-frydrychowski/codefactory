# CodeFactory 1.0

Fabryka kodu: człowiek podaje zadanie w języku naturalnym, agenci kodujący sami
je specyfikują, planują, budują, weryfikują i recenzują. Człowiek zatwierdza
tylko wybrane punkty (zależnie od trybu).

Status: **zaimplementowane: kroki 1-3 oraz planner i reviewer z kroku 4** (adapter CLI, adapter `api` (**wyłączony** flagą `engines.API_ENABLED`, do czasu testu na prawdziwym modelu), intake, planner, zadania w osobnych git worktree (równolegle wg `depends_on` z planu, limit `max_parallel`),
verify, reviewer, GUI; tryby `manual`/`guided`/`auto`). Brak: merge do repo docelowego.

Użycie: `python factory.py "zrób skrypt, który liczy słowa w pliku"`; testy: `python -m unittest`.
Konfiguracja w `config.json` (silniki, role, komenda weryfikacji, limity).

Weryfikacja (`verify`) to komenda uruchamiana w katalogu projektu; jej kod wyjścia decyduje o przejściu dalej, a agenci
widzą ją w każdym prompcie. Dla nowego runu wybierasz ją flagą `--verify <preset|komenda>` albo listą w GUI. Presety są w
`config.json` (`verify_presets`: `python`, `node`). Przed startem fabryka sprawdza, czy komenda **nie przechodzi na pustym
projekcie** (`node --test` bez testów kończy się kodem 0, więc preset `node` używa opakowania `tools/nonempty.py`);
wyłączysz to przez `"verify_canary": false`. Uwaga: na Pythonie <3.12 `unittest discover` bez testów też kończy się
kodem 0 i canary go odrzuci. Użyj wtedy `python -m pytest`. Wybrana komenda zapisuje się w `progress.json` i wznowienie
używa tej samej.

GUI: `python gui.py` (http://127.0.0.1:8765, tylko localhost) pokazuje runy na żywo. Żeby zatwierdzać
bramki (spec, plan) przyciskiem zamiast w terminalu, ustaw w `config.json` `"approval": "gui"`.
Przerwany lub nieudany run wznowisz: `python factory.py --resume <id z runs/>` (albo przyciskiem „Wznów run" w GUI dla runów
zakończonych błędem). Wznowienie pomija zatwierdzone etapy i gotowe zadania, a nieukończone robi od zera; spec można
przed wznowieniem poprawić ręcznie w `runs/<id>/spec.md`. Nie wznawiaj runu, który jeszcze działa.
Runy można też startować z GUI (formularz u góry; wtedy bramki są zawsze w GUI).

## Cele

- Zadania: strony, skrypty, aplikacje.
- Uniwersalność: każdy w zespole używa własnego narzędzia (CLI) lub dostarczonych
  kluczy API, w tym lokalnych LLM-ów na zespołowych maszynach.
- Przenośność: sam folder + Python, bez zależności od jednego dostawcy.

## Przepływ

```
prompt człowieka
   -> 1. INTAKE   prompt -> spec.md (cel, kryteria akceptacji, założenia)
   -> 2. PLAN     spec -> tasks.md (małe zadania z testem "done")
   -> 3. BUILD    agent na zadanie, w osobnym git worktree
   -> 4. VERIFY   testy + lint + build (twarde, bez LLM)
   -> 5. REVIEW   inny agent czyta diff i spec
   -> 6. FIX LOOP wraca do 3, maks. N razy
   -> 7. DELIVER  gałąź / PR + raport po ludzku
```

## Zasady

1. Stan w plikach, nie w pamięci rozmowy: każdy krok można wznowić po awarii.
2. Weryfikacja twarda: o przejściu dalej decydują testy i build, nie opinia agenta.
3. Builder i reviewer to różne agenty (najlepiej różne modele).
4. Limity na każdy run: maks. iteracji, budżet, dozwolone komendy.
5. Klucze tylko ze zmiennych środowiskowych, nigdy z repo.

## Tryby autonomii

| Tryb     | Człowiek zatwierdza |
|----------|---------------------|
| `manual` | spec, plan i merge  |
| `guided` | spec i merge        |
| `auto`   | tylko merge         |

Trybu bez żadnych bramek (YOLO) świadomie nie ma. Merge zawsze zatwierdza człowiek.

## Silniki

Każda rola (intake, planner, builder, reviewer) ma w konfiguracji przypisany silnik.

| Typ   | Przykłady                                      | Pętlę "czytaj/edytuj/uruchom" robi |
|-------|------------------------------------------------|------------------------------------|
| `cli` | Claude Code, Codex CLI, Gemini CLI, inne       | samo narzędzie                     |
| `api` | lokalne LLM-y (Ollama, vLLM, LM Studio), klucze | fabryka (własna pętla narzędzi)    |

Silnik `api` mówi formatem OpenAI-compatible, więc jeden adapter obsługuje
większość lokalnych serwerów (zmienia się tylko adres i klucz).

```yaml
engines:
  claude: { type: cli, command: "claude -p" }
  local:  { type: api, base_url: "http://gpu-box:8000/v1", key_env: LOCAL_KEY, model: "qwen-coder" }

roles:
  planner:  claude
  builder:  local
  reviewer: claude
```

(Przykład ilustracyjny, składnia komend CLI do weryfikacji.)

## Struktura (planowana)

```
agents/        intake.md, planner.md, builder.md, reviewer.md  (prompty ról)
runs/<id>/     spec.md, tasks.md, log.md, status.json          (stan runa)
factory.py     pętla: wybierz zadanie -> odpal agenta -> sprawdź -> dalej
config.yaml    silniki, role, tryb, limity
```

## Plan budowy

1. Interfejs `run_agent(prompt, workdir, tools) -> wynik` i adapter CLI.
2. Pętla intake -> build -> verify z trybami `manual`/`guided`/`auto`.
3. Adapter API z własną pętlą narzędzi (praca tylko w worktree, lista dozwolonych komend).
4. Planner, reviewer, równoległość.
5. Interfejs graficzny wyświetlany podczas pracy fabryki: czytelny podgląd etapów,
   zadań, logu i statusu runa oraz miejsce na zatwierdzanie bramek (spec, plan, merge).
   Czyta stan z `runs/<id>/`, więc nie zmienia rdzenia.

## Otwarte kwestie

- Izolacja dla silnika `api` (kontener vs worktree + allowlista komend).
- Format konfiguracji (YAML wymaga zależności; alternatywa: JSON/TOML ze standardowej biblioteki).
- Zdalne repozytorium i sposób dystrybucji dla zespołu.
