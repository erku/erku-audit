# Plan budowy agenta 1F916 (Ollama Cloud + panel podglądu)

Wersja 2, 12.09.2026. Zakres: bez donacji. Cele agenta: reputacja (karma), zarabianie przez listings/granty, radar trendów. Wszystko na modelach chmurowych Ollama, z własnym interfejsem WWW do podglądu działań.

---

## 0. Odpowiedzi na trzy pytania

**Solana czy ETH?** Oficjalna szyna płatności 1F916 (listings, awardy, granty, x402 patron) działa **wyłącznie na Base w USDC / 1F916** — guide mówi wprost „No other chains or assets are supported", a `POST /api/payout-wallets` wymaga podpisu EIP-191, czyli klucza EVM. Solana pojawia się w społeczności tylko jako **nieoficjalne, peer-to-peer** rozliczenia między agentami poza szyną: izanami sprzedaje odpowiedzi za 1,5 USDC na Solanie, scholium-notes notatki za 0,01 SOL, cairn przyjmuje „SOL or USDC on Solana", bankr-agent operuje „base, solana, EVM". To, co czytałeś o „kompatybilności", dotyczy szerszego ekosystemu agentów (x402 obsługuje Solanę, Bankr itd.), nie samego 1F916.

Decyzja: **zostaw EVM jako podstawę** (Base = ten sam format co ETH). Ale nie używaj `0x6aBE…`, jeśli to twój główny portfel — załóż **dedykowane EOA dla agenta** (docs: „hold little; keep human's main funds out"). Adres Solana można dołożyć w fazie 3, gdy agent zacznie sprzedawać własne usługi poza szyną. Nie blokuje to startu.

**Czy trzeba na wejściu określić specjalizację?** Platforma nie wymaga — rejestracja to tylko `handle` + `model`; nie ma pola bio, kategorii ani deklaracji. Ale rekord jest append-only i publiczny (`GET /api/record/:handle`), więc pierwsze 2–3 tygodnie postów **stają się** tożsamością agenta, czy tego chcesz, czy nie. Rekomendacja: nie wpisuj specjalizacji „na sztywno" w kod, tylko jako **edytowalny plik persony** (`persona.md`), który wstrzykujesz do promptu. Na start ustaw wąską, weryfikowalną niszę (audyt infrastruktury/bezpieczeństwa agentów — pokrywa się z 90% otwartych listingów od `understory` i z tym, co umiesz), a po miesiącu sprawdzasz w panelu, które posty zbierały karmę, i korygujesz plik. Ewolucja jest dopuszczalna, chaos nie.

**Które modele?** Ollama Cloud wystawia `deepseek-v4-flash:cloud` (304B, kontekst 1M, tryby no-thinking / thinking / max-thinking, $0,22/M input, $0,66/M output, cache $0,007/M) i `deepseek-v4-pro:cloud`. Flash bije Pro-Preview w benchmarkach agentowych (Terminal Bench 82,7 vs 72,1) i jest tańszy — to model domyślny. Pro trzymaj jako opcję dla posta dnia i propozycji grantowych, jeśli w praktyce da lepszy tekst (panel pozwoli to porównać).

---

## 1. Architektura

```
┌──────────────────────── serwer (Docker Compose) ────────────────────────┐
│                                                                          │
│  agent-worker (Python)          dashboard (FastAPI + HTMX)               │
│  ├─ scheduler (APScheduler)     ├─ /            przegląd dnia            │
│  ├─ perceive  → 1f916 API       ├─ /actions     log działań + trace LLM  │
│  ├─ decide    → Ollama Cloud    ├─ /inbox       odpowiedzi/wzmianki      │
│  ├─ act       → 1f916 API       ├─ /radar       listingi, granty, tagi   │
│  ├─ radar     → diff + alerty   ├─ /queue       kolejka do zatwierdzenia │
│  └─ writes    → SQLite          ├─ /persona     edycja persona.md        │
│                                 └─ /settings    tryb auto / approve, off │
│           ▲                                ▲                              │
│           └────────── SQLite (WAL) ────────┘                              │
│                                                                          │
│  caddy / nginx  → HTTPS, basic-auth lub za VPN/Tailscale                  │
└──────────────────────────────────────────────────────────────────────────┘
        │                                    │
        ▼                                    ▼
  https://1f916.ai/api/*           https://ollama.com/v1  (OLLAMA_API_KEY)
```

Zasady projektowe:

- **Jedna baza, jedna prawda.** Każde spostrzeżenie, decyzja i akcja to wiersz w SQLite z pełnym śladem: co agent widział (snapshot JSON), jaki prompt dostał, co odpowiedział model, co poszło do API, co wróciło. Panel tylko to czyta.
- **Sekret obywatela i klucz Ed25519 nigdy nie trafiają do modelu.** Model zwraca *intencje* (`{"action":"comment","post_id":...,"body":...}`), a warstwa `act` je wykonuje i egzekwuje limity.
- **Tryb pracy przełączalny z panelu:** `approve` (każdy post/zgłoszenie do listingu czeka na twoje OK, komentarze i głosy lecą same) → `auto` po tym, jak zaufasz agentowi. Plus globalny wyłącznik.
- **Wszystko od innych agentów to dane, nie instrukcje** — treści z forum idą do promptu w wydzielonym bloku z twardą instrukcją systemową; wszystkie intencje modelu walidowane schematem (pydantic) przed wykonaniem.

---

## 2. Komponenty

### 2.1 Klient 1F916 (`f916/client.py`)

Cienki wrapper nad `httpx` z:
- ETag cache dla `/api/changes` i `/api/pulse`,
- licznikiem lokalnych limitów (1/20/50 na dobę UTC, reset o 00:00 UTC) — żeby nie marnować wywołań modelu na akcje, które i tak zostaną odrzucone,
- retry/backoff, logowaniem każdego wywołania do tabeli `api_calls`.

Endpointy używane: `me`, `me/history`, `me/ack`, `pulse`, `changes`, `front`, `new`, `post/:id`, `search`, `tags`, `listings`, `listings/:id`, `grants`, `grants/:slug`, `rail`, `porch`; zapisy: `post`, `comment`, `vote`, `tag`, `listings/:id/submissions`, `grants/:slug/proposals`, `me/cadence`, `porch`. Schematy pól — z `openapi.json` (pobierany przy starcie, żeby wykryć zmiany API i podnieść alarm w panelu).

Alternatywa: podpiąć się do `https://1f916.ai/mcp` przez klienta MCP w Pythonie. Zaleta: 60 gotowych narzędzi i nazwy pól utrzymywane przez nich; wada: mniej kontroli nad limitami i logowaniem. Dla panelu z pełnym śladem lepszy jest własny klient REST.

### 2.2 Warstwa LLM (`f916/brain.py`)

Ollama Cloud przez endpoint zgodny z OpenAI (`https://ollama.com/v1`, nagłówek `Authorization: Bearer $OLLAMA_API_KEY`) albo przez oficjalne SDK `ollama` z `host="https://ollama.com"`. Modele:

| Zadanie | Model | Tryb |
|---|---|---|
| Triage feedu, wybór głosów, ocena listingów | `deepseek-v4-flash:cloud` | no-thinking |
| Komentarze | `deepseek-v4-flash:cloud` | thinking |
| Post dnia, propozycja grantu, zgłoszenie do listingu | `deepseek-v4-flash:cloud` (A/B z `deepseek-v4-pro:cloud`) | max-thinking |
| Radar: klasyfikacja „czy to nowy sposób zarabiania" | `deepseek-v4-flash:cloud` | no-thinking, structured output |

Wyjście zawsze w JSON (structured output / `response_format`), walidowane pydantic. Kontekst 1M tokenów pozwala wrzucić cały dzisiejszy front + wątki, w które agent jest zaangażowany, bez RAG — na start wystarczy. Koszt szacunkowo: kilkadziesiąt tys. tokenów input dziennie × 24 cykle ≈ 1–3 USD/dzień przy Flash; cache promptu (persona + reguły) obniża to znacząco.

Prompty (pliki w `prompts/`, edytowalne z panelu):
- `system.md` — konstytucja 1F916 w skrócie, twarde zakazy (nie prosić o pieniądze, nie shillować tokena, nie ujawniać sekretów, traktować treści forum jako dane), format JSON.
- `persona.md` — kim jest agent, nisza, ton, czego nie robi. Wersjonowany (każda edycja = wiersz w `persona_versions`, żeby panel mógł pokazać „karma per wersja persony").
- `post_of_the_day.md`, `comment.md`, `triage.md`, `listing_eval.md`, `radar.md`.

### 2.3 Pętla agenta (`f916/loop.py`)

Cykl krótki co 15 min (`pulse` z `wait=25` jako trigger + fallback timer):

1. `GET /api/me` → inbox; `GET /api/changes` (ETag).
2. Jeśli nic nowego → koniec cyklu (zero kosztu LLM).
3. Triage (Flash, no-thinking): dla każdej nowej pozycji — odpowiedzieć / zagłosować / zignorować / odłożyć do posta dnia. Budżet: max 8 komentarzy w cyklu, reszta limitu na wieczór.
4. Wykonanie: głosy od razu; komentarze od razu (tryb auto) lub do kolejki (tryb approve).
5. `POST /api/me/ack`.

Cykl dzienny (raz, np. 09:00 UTC — przed europejskim południem, gdy tablica jest najżywsza):

1. Pobierz front, new, listings, grants, własną historię i notatki „odłożone" z cykli krótkich.
2. Wybór tematu posta dnia (max-thinking) — wymóg: konkretny artefakt (wynik testu, skrypt, liczby). Jeśli agent nie ma artefaktu, **nie publikuje** — lepiej opuścić dzień niż wrzucić pustą treść (rekord jest wieczny).
3. Ocena listingów: czy warunek jest spełnialny narzędziami agenta? Jeśli tak → do kolejki z propozycją wykonania; ty zatwierdzasz, agent wykonuje (w sandboxie Docker), składa `submission`.
4. Radar (patrz 2.5) + raport dzienny.

Narzędzia agenta do generowania artefaktów (żeby miał o czym pisać): sandbox `docker run --rm` z Pythonem i podstawowymi narzędziami sieciowymi; dostęp read-only do publicznych API 1F916 (własne „probe'y" społeczności są ulubionym tematem tablicy — tag `board-instrument`, `measurement`, `falsifier`).

### 2.4 Panel (`dashboard/`)

FastAPI + Jinja2 + HTMX (bez SPA, bez build-stepu), Tailwind z CDN lub własny CSS. Strony:

- **Dziś** — karma (delta 24h/7d), zużyte limity (posty/komentarze/głosy), koszt LLM w USD, ostatnie 20 działań, stan trybu (auto/approve/off).
- **Działania** — tabela z filtrami (typ, wynik, dzień). Klik → pełny trace: snapshot wejścia, prompt, odpowiedź modelu, request do API, response, czas, tokeny, koszt. To jest serce „podglądu, co się działo".
- **Inbox** — odpowiedzi i wzmianki z `me`, z zaznaczeniem, na które agent już zareagował.
- **Radar** — nowe listingi (z podświetleniem funderów spoza `1f916-agent`/`understory`), granty i ich fazy, delta tagów, wykres outside-funded GMV z `/api/rail`, lista postów sklasyfikowanych jako „nowy sposób zarabiania" z uzasadnieniem modelu.
- **Kolejka** — posty, zgłoszenia do listingów i propozycje grantowe czekające na twoje zatwierdzenie; edycja inline, „zatwierdź / odrzuć / przepisz".
- **Persona i prompty** — edycja plików z historią wersji; przy każdej wersji statystyka karmy zebranej pod nią.
- **Ustawienia** — tryb, budżet dzienny USD dla LLM, wybór modelu per zadanie, wyłącznik.

Powiadomienia: Telegram bot (lub e-mail) dla: pozycja w kolejce, nowy listing od zewnętrznego fundera, award dla agenta, błąd API, przekroczenie budżetu. Panel za Tailscale/WireGuard lub Caddy z basic-auth — nie wystawiaj go publicznie, bo zawiera trace'y i ślady po sekretach w logach.

### 2.5 Radar trendów (`f916/radar.py`)

Co godzinę: diff `listings` (nowe id, nowi funderzy, nowe assety), `grants` (nowe sluggi, przejścia stanów), `tags` (delta liczników dla `bounty|economics|agent-economics|custody|x402|payout|revenue`), `search` po `USDC|SOL|recurring|revenue|x402`. Nowe posty spełniające filtr → Flash klasyfikuje w JSON: `{kind: "new_earning_mechanism" | "listing_demand" | "noise", evidence: "...", confidence}`. Tylko `confidence ≥ 0.7` trafia na stronę Radar i do Telegrama. Raz dziennie zrzut `/api/rail` do tabeli `rail_snapshots` — wykres GMV z zewnątrz pokaże, kiedy ekonomia realnie ruszy.

Reguła twarda w prompcie radaru: „nowy sposób zarabiania = powtarzalny listing od zewnętrznego fundera z paragonem na szynie, albo agent publikujący liczby przychodu z weryfikowalnym dowodem; deklaracje bez paragonów to `noise`".

### 2.6 Portfel (faza 3, opcjonalnie od razu)

- Dedykowane EOA na Base (klucz w `.env` na serwerze, nigdy w modelu). `eth_account` do podpisów EIP-191.
- `POST /api/keys` (Ed25519, custody self) → `GET /api/payout-wallets/preimage` → podpis EIP-191 + Ed25519 → `POST /api/payout-wallets`.
- Bindings/receipts po pierwszym awardzie — zaimplementować, gdy będzie potrzeba; panel pokaże stan (`/api/payouts`).
- Solana: dołożyć adres, gdy agent zacznie sprzedawać usługę poza szyną (np. „ask rail" jak izanami). Wtedy `solders`/`solana-py` do weryfikacji wpłat.

---

## 3. Stos i repo

```
f916-agent/
├── docker-compose.yml        # worker, dashboard, caddy
├── .env.example              # F916_SK, F916_ED25519_PEM, OLLAMA_API_KEY, TELEGRAM_*, WALLET_PK
├── f916/
│   ├── client.py             # REST + limity + ETag
│   ├── brain.py              # Ollama Cloud, structured output, koszty
│   ├── loop.py               # cykle 15 min / dzienny
│   ├── radar.py
│   ├── actions.py            # wykonanie intencji, walidacja, kolejka
│   ├── tools/sandbox.py      # docker run dla artefaktów
│   └── db.py                 # SQLModel/SQLAlchemy, WAL
├── prompts/{system,persona,post_of_the_day,comment,triage,listing_eval,radar}.md
├── dashboard/                # FastAPI + Jinja2 + HTMX
├── scripts/register.py       # jednorazowa rejestracja + klucz Ed25519
└── tests/                    # limity, walidacja intencji, parsowanie API
```

Python 3.12, `httpx`, `pydantic`, `apscheduler`, `sqlmodel`, `fastapi`, `jinja2`, `ollama` (lub `openai` z `base_url=https://ollama.com/v1`), `cryptography`, `eth-account`, `python-telegram-bot`. Baza SQLite z WAL wystarczy na lata przy tym wolumenie (kilkaset wierszy dziennie).

---

## 4. Harmonogram

**Faza 0 — fundament (1–2 dni).** Rejestracja handle + Ed25519 (skrypt), klient REST z limitami, SQLite, cykl 15 min tylko w trybie *read* (agent obserwuje, nic nie pisze), panel „Dziś" + „Działania". Efekt: widzisz w panelu, co agent by zrobił.

**Faza 1 — głos i komentarze (2–3 dni).** Triage + głosy auto, komentarze w trybie approve. Persona v1. Telegram. Po tygodniu przegląd trace'ów, poprawka promptów, przełączenie komentarzy na auto.

**Faza 2 — post dnia i radar (3–4 dni).** Sandbox do artefaktów, post dnia w trybie approve, radar + strona Radar + raport dzienny. Tu zaczyna się budowanie karmy.

**Faza 3 — pieniądze (2–3 dni, gdy pojawi się listing wart wykonania).** EOA na Base, payout-wallet, zgłoszenia do listingów przez kolejkę, propozycje grantowe. Ewentualnie Solana dla własnych usług.

**Faza 4 — autonomia i ocena (ciągłe).** Metryki w panelu: karma per wersja persony, koszt/karma, hit-rate komentarzy (ile dostało głosy), listingi wygrane/złożone. Co 2 tygodnie decyzja: zmiana niszy, modelu, rytmu.

---

## 5. Ryzyka i jak je panel łapie

- **Prompt injection od innych agentów** — walidacja schematu intencji, zakaz akcji spoza listy, każdy zapis widoczny w trace; w trybie approve nic nieodwracalnego nie wychodzi samo.
- **Zmiana API 1F916** — porównanie hasha `openapi.json` przy starcie; różnica = alert i wstrzymanie zapisów.
- **Koszt LLM** — twardy budżet dzienny w USD, licznik tokenów per wywołanie, wykres w panelu.
- **Pusta treść** — reguła „bez artefaktu nie ma posta"; strona Działania pokazuje dni pominięte z powodem.
- **Utrata sekretu** — kopia `F916_SK` i PEM w menedżerze haseł poza serwerem; rotacja `POST /api/rotate` tylko ręcznie.
- **Nagrobek w mieście** — cadence zadeklarowane uczciwie (np. 900 s), alarm gdy worker nie wykonał cyklu przez 2 h.

---

## Źródła

- 1F916: https://1f916.ai/llms.txt · https://1f916.ai/api/surface · https://1f916.ai/openapi.json · https://1f916.ai/api/listings/guide · https://1f916.ai/api/search?q=solana
- Ollama Cloud: https://ollama.com/library/deepseek-v4-flash:cloud · https://ollama.com/library/deepseek-v4-pro:cloud
