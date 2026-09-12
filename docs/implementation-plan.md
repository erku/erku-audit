# Plan wdrożenia 1F916

Cel: działający lokalnie audytor społeczności 1F916, zgodny z trzema dokumentami docs i lokalną Ollamą.
Stos: Python 3.12, httpx, pydantic 2, FastAPI/Jinja2, SQLite WAL, Docker Compose.
Pierwszeństwo: rozszerzenie techniczne > plan v2 > analiza; lokalna Ollama zastępuje chmurę.

## Decyzje
- Wymagany model deepseek-v4-flash:cloud, zgodnie z planem budowy i potwierdzeniem użytkownika; http://host.docker.internal:11434 z kontenera. Lokalna Ollama jest bramą do tego modelu chmurowego, nie zamiennikiem na model lokalny. Brak automatycznego fallbacku na qwen.
- Publiczne API https://1f916.ai; kontrakt zapisany w contracts/openapi.json.
- Approve: głosy automatyczne, komentarze/posty w kolejce. Pieniądze i granty zawsze ręcznie.
- Bez klucza obserwacja. Panel tylko localhost, uwierzytelnienie, CSRF, trwałe dane.
- Sekrety poza modelem, bez wykonywania kodu generowanego przez LLM i bez docker.sock w workerze.
- RTK.md wskazany w instrukcji użytkownika nie istnieje w projekcie ani przodkach.
- Brak repo git; pracujemy bezpośrednio w pustym projekcie, zachowując docs.
- Użytkownik wybrał handle erku-audit i zatwierdził utworzenie tej tożsamości.
- Użytkownik zmienił model prowadzący zadanie Codex na Sol; nie zmienia to DeepSeek w agencie.

## Dziennik weryfikacji środowiska
- Docker Engine 29.7.2 i Compose v5.5.1 odpowiadają.
- Ollama /api/pull dla deepseek-v4-flash:cloud zakończone sukcesem.
- Rzeczywiste /api/chat przez localhost:11434 zwróciło poprawny JSON (12 tokenów wejścia, 53 wyjścia).
- /api/changes bez since/kursorów odpowiada 400; pętla musi przekazywać wymagane parametry.
- Zależności testowe zainstalowane w lokalnym .venv, bez zmian globalnego Pythona.

## Zadania i własność
- [x] 1. core: f916/config.py, db.py, client.py, actions.py, models.py, scripts/register.py; testy core.
- [x] 2. defense: defense/, tuner/, f916/skills/, f916/tools/, invariants.py, config/policy.json; testy obrony i audytów.
- [x] 3. dashboard: dashboard/, Dockerfile, compose.yaml, pyproject.toml, .env.example, README.md; testy panelu.
- [x] 4. worker: f916/brain.py, loop.py, radar.py, prompts/; testy cyklu i LLM.
- [x] 5. integracja: testy całości, test kontenerów i Ollamy, przegląd bezpieczeństwa, lokalne uruchomienie.

Każde zadanie implementuje oddzielny subagent z ograniczonym kontekstem; najpierw testy kluczowego zachowania, potem implementacja i weryfikacja. Kontroler integruje i sprawdza wyniki. Bez duplikowania pełnej analizy.

## Wspólne interfejsy
Settings() z env: data_dir: Path, api_base: str, api_key: str, handle: str, ollama_url: str, ollama_model: str, mode: str, dashboard_user: str, dashboard_password: str, cycle_seconds: int, llm_daily_tokens: int. Domyślny data_dir=data, mode=approve, cycle_seconds=900.
Database(path): SQLite. initialize(); get_setting(key, default=None); set_setting(key,value); log(kind, data)->int; events(kind=None,limit=100)->list[dict] z id,kind,data,created_at. Dane JSON, redakcja sekretów. queue(intent:dict,reason:str)->int; queue_items(status='pending')->list[dict]; resolve_queue(id,status); get_queue(id)->dict|None. Record struktura queue: id,intent,reason,status,created_at. Trwała atomowa kontrola akcji w core.
Client(settings,db): synchroniczny httpx. get(path,params=None)->dict|None (None dla 304); post(path,payload)->dict; check_contract()->bool. Sesja do mockowania przez opcjonalny transport.
Intent: pydantic, action oraz pola akcji; model_validate(dict), model_dump(exclude_none=True). Executor(settings,db,client).dispatch(intent, approved=False)->dict; queued/sent/blocked/error status. Kolejka nie wykonuje akcji w procesie dashboardu; panel oznacza approved, worker je odbiera.
defense.ingest.inspect_content(value)->dict z safe:bool, reasons:list[str], content:any. wrap_untrusted(value)->str. Treści escape, brak sekretów. Nie jest to gwarancja wykrycia wszystkich injection.
f916.skills.run(skill:str,target:dict,params:dict,output_dir:Path)->dict artefaktu (summary,evidence_files,repro_cmd,hash). Brak dowolnego kodu. Audyty deterministyczne na wejściowych danych.
Brain(settings,db).decide(snapshot,task='triage')->list[Intent]. Ollama /api/chat format JSON, timeout, twardy budżet tokenów, ślad zredagowany.
Worker(settings,db,client,brain).cycle(); main uruchamiany python -m f916.loop. Odczyt zatwierdzonej kolejki, scheduler, radar/daily/nightly/weekly przez trwałe znaczniki w DB.

## Kryteria weryfikacji
Testy: limity UTC i rolling, brak ponowień niepewnych zapisów, kolejka i off, self-vote, brak sekretów, injection/quarantine, walidacja JSON, brak zmian bez LLM, budżet, hash artefaktu, auth/CSRF, restart.
Docker: compose config, build, pytest w obrazie, zdrowy panel, udany odczyt Ollamy z kontenera, obserwacyjny cykl bez klucza. Publikacja dopiero z tożsamością i zachowaniem kolejki.
