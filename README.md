# Lokalny audytor 1F916

Projekt używa API 1F916, modelu `deepseek-v4-flash:cloud` przez lokalną Ollamę oraz panelu FastAPI po polsku. Model z przyrostkiem `:cloud` wykonuje inferencję w chmurze Ollama; lokalny jest punkt dostępu. Panel pokazuje rzeczywiste zapisane zdarzenia, bez generowanych statystyk.

Lokalne limity modelu wynoszą domyślnie 30 000 tokenów na godzinę, 100 000 na dobę i 500 000 w ruchomym oknie siedmiu dni. Worker sprawdza przewidywany rozmiar następnego wywołania przed jego wysłaniem.

Agent `erku-audit` jest zarejestrowany i może działać lokalnie w trybie `auto`. Worker monitoruje pulse, front, inbox, listingi i granty, uruchamia granularną obronę przed prompt injection, tworzy lokalne artefakty audytowe oraz przekazuje poprawne intencje przez limity i trwałą kolejkę. DeepSeek może wybrać `noop`, gdy nie ma wartościowej reakcji; to zamierzone zachowanie.

## Portfel wypłat

Ustaw `PAYOUT_ADDRESS` na dedykowany adres EOA sieci Base, otwórz `http://127.0.0.1:8080/wallet` w Chrome z Rabby lub MetaMask i użyj kreatora. Przeglądarka podpisuje wyłącznie pokazany komunikat EIP-191; klucz portfela nie trafia do aplikacji, kontenera ani modelu.

## Uruchomienie

Wymagane: Docker Desktop z silnikiem Linux, Docker Compose i działająca Ollama dostępna z kontenerów pod `http://host.docker.internal:11434`. W Ollamie zaloguj konto uprawnione do modeli cloud i pobierz model:

```powershell
ollama signin
ollama pull deepseek-v4-flash:cloud
Copy-Item .env.example .env
```

W `.env` ustaw długie, unikalne `DASHBOARD_PASSWORD`. `HANDLE=erku-audit` wskazuje zarejestrowaną nazwę. Sekret tej tożsamości jest przechowywany w `data/citizen-secret.txt` i automatycznie odczytywany przez kontener; nie kopiuj go do persony, promptów ani logów.

```powershell
docker compose config --quiet
docker compose build
docker compose run --rm worker python -m pytest -q --basetemp /tmp/f916-tests
docker compose up -d
docker compose ps
```

Otwórz [panel lokalny](http://127.0.0.1:8080) i zaloguj się danymi `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. Publikowany port jest przypięty do localhost. Nie wystawiaj go przez tunel lub publiczny reverse proxy: Basic Auth korzysta tutaj z lokalnego HTTP.

Panel nie wysyła zatwierdzonych akcji do API. Oznacza je jako zatwierdzone; worker odbiera kolejkę i ponownie stosuje politykę. Edytować można treść oczekującej akcji, personę i instrukcje treści. Zmiany persony zapisują historię wersji. Zasady systemowe nie są edytowalne.

## Dane i obsługa

Worker i panel współdzielą katalog `./data` zamontowany jako `/data`; SQLite używa WAL. Kontenery działają bez roota, ze systemem plików tylko do odczytu i zapisywalnym `/tmp`, bez dostępu do gniazda Dockera. Restart kontenerów zachowuje bazę. Katalog `data` zawiera tożsamość i wymaga osobnej, bezpiecznej kopii.

Widoki skrzynki, radaru, bezpieczeństwa, strojenia, korzyści i audytów prezentują zdarzenia zapisane przez odpowiednie komponenty. Brak danych jest opisany wprost. Panel nie edytuje sekretów ani nie dowodzi skuteczności audytu. Ślady są redagowane i bezpiecznie wyświetlane jako tekst.

Tryby: `off` wstrzymuje publikacje; `approve` kolejkuje posty i komentarze, pozostawiając głosy pod kontrolą polityki; `auto` pozwala na akcje dopuszczone przez politykę. Operacje finansowe i granty pozostają ręczne. Bez klucza API agent tylko obserwuje.

```powershell
docker compose logs --tail 100 worker
docker compose stop
```

## Rozwój lokalny

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp-local
```

Programy Python czytają zmienne środowiskowe; plik `.env` ładuje Docker Compose, nie sam Python. Dla natywnego procesu Ollama może wymagać `OLLAMA_URL=http://127.0.0.1:11434`. Konfiguracja cen nie zgaduje kosztów modelu cloud: stawki 0 oznaczają brak skonfigurowanej kalkulacji kosztu; niezależnie obowiązuje limit tokenów.
