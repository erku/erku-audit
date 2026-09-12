# 1F916 agent — nowe możliwości wykonawcze (v3)

Data: 2026-09-12. Gałąź: `feat/autonomous-executor`.

Rozszerzenie z monitora obserwującego okazje w uczestnika, który potrafi zbudować dowód przypisany do konkretnego listingu, publikować i pieczętować tylko weryfikowalną pracę, oraz — pod kontrolą — budować większe projekty i publikować je przez broker o minimalnych uprawnieniach.

## Pełna ścieżka bounty (Task 1)
`f916/opportunities.py` — `OpportunityRunner` klasyfikuje listing wyłącznie z danych strukturalnych (nigdy z prozy społeczności):

- `supported` — bounded audyt (`gate-probe`, `leak-probe`, `rail-audit`) z kompletnym wejściem w polu `audit`. Buduje dowód związany z `listing_id` + `source_hash` + `payload_hash`, **publikuje → pieczętuje → zgłasza** z pełną referencją `URL sha256:<h> commit:<c> seal:<n>`.
- `project_required` — praca wymagająca kodu/PR (rozpoznana po słowach kluczowych). Nie jest udawana jako wykonana.
- `unsupported` — brak dozwolonego, strukturalnego audytu.
- `already_attempted` — stan terminalny per `(listing_id, source_hash)`; niepewne zapisy (seal/submit) nie są nigdy ponawiane.

Walidator w `Executor._submission_valid` wymaga zapieczętowanego, publicznego artefaktu związanego z danym listingiem — zgłoszenia bez dowodu są blokowane.

## Rotacja audytów (Task 2 + integracja)
`f916/audit_program.py` — deterministyczna rotacja `self-redteam → rail-audit → leak-probe → gate-probe`. `daily_audit` buduje wejścia z listingów ostatniego cyklu, wybiera pierwszy wykonalny audyt (pomija te bez danych), z gwarantowanym fallbackiem `self-redteam`. Dedup per typ na haszu treści (`last_published_artifact_hash:<label>`); audyt związany z listingiem publikowany jest zawsze. Kursor (`audit_cursor`) przesuwa się tylko po ukończonym przebiegu — błąd publikacji ponawia ten sam audyt.

## Kadencja i odzysk po limicie (Task 3)
- Zwykła analiza: **1 h** (`ordinary_cadence_seconds`, domyślnie 3600), pilna: 1800 s (`urgent_cadence_seconds`).
- `f916/recovery.py` — trwały `llm_retry_state`: przy 429/503 z Ollamy parsuje `Retry-After` (sekundy lub data HTTP), a bez niej stosuje ograniczony backoff wykładniczy (≤ `llm_retry_cap_seconds`). Model nie jest odpytywany, dopóki blokada trwa; stan czyszczony po pierwszym sukcesie.
- Panel pokazuje „Następna dopuszczalna analiza".

## Builder projektów (Task 4)
`f916/builder.py` — deterministyczny, ograniczony. Szablony `python-tool` i `static-report`. Wymusza prefiks `erku-1f916-`, limity plików/rozmiaru, odrzuca traversal/symlink/ucieczkę z workspace, skanuje sekrety, dodaje README/LICENSE/testy i manifest `1f916.project.v1` z reprodukowalnym `tree_hash` (niezależnym od czasu). **Nigdy nie wykonuje treści** — czysty templating z escapingiem.

## Broker GitHub o minimalnych uprawnieniach (Task 5)
`broker/` — osobny serwis FastAPI, jedyny posiadacz tokena GitHub (montowany sekret `GITHUB_TOKEN_FILE`). Worker uwierzytelnia się osobnym `BROKER_TOKEN` (porównanie w stałym czasie); LLM nie widzi żadnego z nich.

- `POST /repos` — publiczne repo z wymuszonym prefiksem, limitem (`BROKER_MAX_REPOS`, domyślnie 5), idempotentne.
- `POST /repos/{name}/publish` — waliduje manifest (`sha256` każdego pliku + `tree_hash` przeliczane i porównywane), ścieżki bezpieczne, limit payloadu; treść w polu `file_contents`.
- **Brak** tras kasowania, zmiany widoczności i dowolnego gita. Token nigdy nie trafia do odpowiedzi/logów/wyjątków.

### Uruchomienie brokera (operator, ręcznie)
Broker jest domyślnie uśpiony (profil `broker`). Włączenie:

```bash
python scripts/broker_bootstrap.py   # kopiuje token z `gh` do ./secrets/github_token (0600, ignorowany)
docker compose --profile broker up -d broker
```

**Ograniczenie: broker musi działać jako jeden proces** (bez `--workers>1`) — serializacja stanu jest wewnątrzprocesowa.

### Granica bezpieczeństwa (świadomie niewpięte)
Zgodnie z planem worker **nie** tworzy repozytoriów automatycznie. Builder i broker to dostępne możliwości; tworzenie repo następuje dopiero, gdy realna okazja przejdzie politykę kwalifikacji projektu — decyzja pozostaje bramkowana. Klucz deploy `erku/erku-audit` pozostaje ograniczony do repozytorium audytowego. Ścieżka drugiej publikacji (PR) w brokerze wymaga prymitywu `create_branch` zanim zostanie użyta na żywym GitHubie.
