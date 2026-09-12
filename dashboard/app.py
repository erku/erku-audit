"""Authenticated local UI; queue approvals are consumed by the worker."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
PAGES = {'': 'Przegląd', 'actions': 'Akcje i ślady', 'inbox': 'Skrzynka', 'radar': 'Radar', 'queue': 'Kolejka', 'wallet': 'Portfel wypłat', 'persona': 'Persona', 'settings': 'Ustawienia', 'security': 'Bezpieczeństwo', 'tuning': 'Dostrajanie', 'benefits': 'Korzyści', 'audits': 'Audyty', 'results': 'Wyniki'}
FILTERS = {'inbox': ('inbox', 'notification', 'mention'), 'radar': ('radar',), 'security': ('security', 'quarantine', 'blocked', 'defense', 'incident'), 'tuning': ('tuning', 'tuner', 'bandit_', 'policy_', 'reward_'), 'benefits': ('benefit', 'reward', 'grant'), 'audits': ('audit', 'skill', 'artifact'), 'actions': ('action', 'llm', 'cycle', 'trace', 'queue', 'api_call')}
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


def _same_panel_origin(origin, request_url):
    source = urlsplit(origin)
    source_port = source.port or (443 if source.scheme == 'https' else 80)
    request_port = request_url.port or (443 if request_url.scheme == 'https' else 80)
    exact = source.hostname == request_url.hostname
    loopback_alias = source.hostname in LOOPBACK_HOSTS and request_url.hostname in LOOPBACK_HOSTS
    return source.scheme == request_url.scheme and source_port == request_port and (exact or loopback_alias)


def _submission_summary(db):
    events = db.events('outcome', 1000)
    paid = awarded = submitted = 0
    for event in events:
        data = event.get('data') if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        state = data.get('state')
        if state == 'paid':
            paid += 1
        elif state in ('awarded', 'payable'):
            awarded += 1
        elif state == 'submitted':
            submitted += 1
    denominator = paid + awarded + submitted
    win_rate = round(100 * paid / denominator, 1) if denominator else 0
    recent = []
    for event in events[:10]:
        data = event.get('data') if isinstance(event, dict) else {}
        if not isinstance(data, dict):
            data = {}
        recent.append({'listing_id': data.get('listing_id'), 'state': data.get('state'), 'amount_atomic': data.get('amount_atomic')})
    return {'paid': paid, 'awarded': awarded, 'submitted': submitted, 'win_rate': win_rate, 'recent': recent}


def _opportunity_summary(db):
    by_classification = {}
    by_template = {}
    template_hits = 0
    for event in db.events('opportunity', 1000):
        data = event.get('data') if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        classification = data.get('classification', 'unknown')
        by_classification[classification] = by_classification.get(classification, 0) + 1
        template_id = data.get('template_id')
        if template_id:
            template_hits += 1
            by_template[template_id] = by_template.get(template_id, 0) + 1
    return {'by_classification': by_classification, 'template_hits': template_hits, 'by_template': by_template}


def _audit_summary(db):
    by_skill = {}
    for event in db.events('artifact', 1000):
        data = event.get('data') if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        skill = data.get('skill') or data.get('label') or 'unknown'
        by_skill[skill] = by_skill.get(skill, 0) + 1
    bandit_state = db.get_setting('bandit_state', {})
    return {'by_skill': by_skill, 'audit_cursor': db.get_setting('audit_cursor'), 'bandit_state': bandit_state if isinstance(bandit_state, dict) else {}}


def _project_summary(db):
    by_stage = {}
    for event in db.events('project', 500):
        data = event.get('data') if isinstance(event, dict) else None
        if not isinstance(data, dict):
            continue
        stage = data.get('stage', 'unknown')
        by_stage[stage] = by_stage.get(stage, 0) + 1
    repos = db.get_setting('project_repos', [])
    return {'by_stage': by_stage, 'repos': len(repos) if isinstance(repos, list) else 0}


def _build_results(db):
    """Read-only rollup for the 'results' page; safe to call on an empty DB."""
    reward_metrics = db.get_setting('reward_metrics', {})
    earnings = reward_metrics.get('outside_funded_earnings_usd', 0) if isinstance(reward_metrics, dict) else 0
    return {
        'earnings_usd': earnings,
        'submissions': _submission_summary(db),
        'opportunities': _opportunity_summary(db),
        'audits': _audit_summary(db),
        'projects': _project_summary(db),
    }


def create_app(settings=None, db=None):
    @asynccontextmanager
    async def lifespan(app):
        nonlocal settings, db
        if settings is None:
            from f916.config import Settings
            settings = Settings()
        if db is None:
            from f916.db import Database
            db = Database(Path(settings.data_dir) / 'agent.sqlite3')
        db.initialize()
        yield

    app = FastAPI(title='1F916 — panel lokalny', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    templates = Jinja2Templates(directory=ROOT / 'templates')
    templates.env.filters['pretty'] = lambda value: json.dumps(value, ensure_ascii=False, indent=2)
    basic = HTTPBasic()
    csrf_key = secrets.token_bytes(32)

    def authenticate(credentials: HTTPBasicCredentials = Depends(basic)):
        if not settings.dashboard_password:
            raise HTTPException(503, 'Ustaw DASHBOARD_PASSWORD przed użyciem panelu.')
        valid_user = secrets.compare_digest(credentials.username.encode(), settings.dashboard_user.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), settings.dashboard_password.encode())
        if not (valid_user and valid_password):
            raise HTTPException(401, 'Nieprawidłowe dane logowania', headers={'WWW-Authenticate': 'Basic'})
        return credentials.username

    def csrf_token(user):
        return hmac.new(csrf_key, user.encode(), hashlib.sha256).hexdigest()

    async def form_data(request, user):
        origin = request.headers.get('origin')
        # Sandboxed desktop webviews may serialize their opaque origin as
        # "null". The request still needs Basic Auth, a valid per-process CSRF
        # token, a loopback Host, and must not declare itself cross-site.
        if origin and origin != 'null':
            if not _same_panel_origin(origin, request.url):
                db.log('security', {'event':'origin_rejected', 'origin':origin,
                                    'request_scheme':request.url.scheme,
                                    'request_host':request.url.hostname,
                                    'request_port':request.url.port})
                raise HTTPException(403, 'Niedozwolone źródło żądania')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            db.log('security', {'event':'cross_site_rejected',
                                'origin':origin, 'sec_fetch_site':'cross-site'})
            raise HTTPException(403, 'Żądanie spoza panelu')
        form = await request.form(max_fields=10, max_files=0)
        if not secrets.compare_digest(str(form.get('csrf', '')), csrf_token(user)):
            raise HTTPException(403, 'Nieprawidłowy token CSRF; odśwież panel')
        return form

    def safe(value):
        sensitive = ('secret', 'password', 'api_key', 'apikey', 'authorization', 'cookie', 'private_key')
        if isinstance(value, dict):
            return {str(k): '[UKRYTO]' if any(s in str(k).lower() for s in sensitive) or str(k).lower() in ('token', 'access_token', 'refresh_token') else safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(v) for v in value]
        if isinstance(value, str):
            for secret in (settings.api_key, settings.dashboard_password):
                if secret:
                    value = value.replace(secret, '[UKRYTO]')
        return value

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.url.hostname not in LOOPBACK_HOSTS | {'testserver'}:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse('Niedozwolony host', status_code=400)
        response = await call_next(request)
        response.headers.update({'Content-Security-Policy': "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'", 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        return response

    @app.post('/settings')
    async def save_settings(request: Request, user=Depends(authenticate)):
        form = await form_data(request, user)
        mode = form.get('mode')
        if mode not in ('off', 'approve', 'auto'):
            raise HTTPException(422, 'Nieprawidłowy tryb')
        db.set_setting('mode', mode)
        db.log('settings_changed', {'mode': mode})
        return RedirectResponse('/settings', 303)

    @app.post('/settings/token-limits')
    async def save_token_limits(request: Request, user=Depends(authenticate)):
        form=await form_data(request,user)
        if form.get('token_limits_form') != '1': raise HTTPException(422,'Nieprawidłowy formularz')
        values=form.getlist('enabled')
        if values not in ([],['true']): raise HTTPException(422,'Nieprawidłowa wartość przełącznika')
        enabled=values==['true']
        db.set_setting('llm_token_limits_enabled',enabled)
        db.log('settings_changed',{'llm_token_limits_enabled':enabled})
        return RedirectResponse('/settings',303)

    @app.get('/healthz')
    async def healthz():
        try:
            db.get_setting('mode', settings.mode)
            return JSONResponse({'status':'ok'})
        except Exception:
            return JSONResponse({'status':'error'}, status_code=503)

    @app.post('/persona')
    async def save_persona(request: Request, user=Depends(authenticate)):
        form = await form_data(request, user)
        values = {key: str(form.get(key, '')).strip() for key in ('persona', 'content_prompt')}
        if any(len(v) > 8000 for v in values.values()):
            raise HTTPException(422, 'Maksymalnie 8000 znaków na pole')
        for key, value in values.items():
            db.set_setting(key, value)
        db.log('persona_version', values)
        return RedirectResponse('/persona', 303)

    @app.post('/wallet/prepare')
    async def prepare_wallet(request: Request, user=Depends(authenticate)):
        await form_data(request, user)
        from f916.client import Client
        from f916.wallet import prepare_payout_wallet
        client = Client(settings, db)
        try:
            prepared = prepare_payout_wallet(client, settings.handle, settings.payout_address)
        finally:
            client.close()
        db.set_setting('payout_wallet_preimage', prepared)
        db.set_setting('payout_wallet_submission', 'prepared')
        db.log('wallet', {'status':'prepared', 'address':prepared['address'], 'chain_id':prepared['chain_id'], 'expiry':prepared['expiry']})
        return RedirectResponse('/wallet', 303)

    @app.post('/wallet/bind')
    async def bind_wallet(request: Request, user=Depends(authenticate)):
        form = await form_data(request, user)
        prepared = db.get_setting('payout_wallet_preimage')
        if not prepared:
            raise HTTPException(409, 'Najpierw przygotuj preimage')
        if db.get_setting('payout_wallet_submission') in ('attempted', 'uncertain', 'bound'):
            raise HTTPException(409, 'To powiązanie zostało już wysłane lub wymaga sprawdzenia')
        from f916.client import Client
        from f916.wallet import build_payout_wallet_payload
        try:
            payload = build_payout_wallet_payload(prepared, str(form.get('signature', '')), Path(settings.data_dir) / 'identity-ed25519.pem')
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        db.set_setting('payout_wallet_submission', 'attempted')
        client = Client(settings, db)
        try:
            result = client.post('/api/payout-wallets', payload)
        except Exception:
            db.set_setting('payout_wallet_submission', 'uncertain')
            db.log('wallet', {'status':'uncertain', 'address':prepared['address']})
            raise HTTPException(502, 'Wynik wysłania jest niepewny; sprawdź listę portfeli przed ponowieniem') from None
        finally:
            client.close()
        db.set_setting('payout_wallet_submission', 'bound')
        db.log('wallet', {'status':'bound', 'address':prepared['address'], 'result':result})
        return RedirectResponse('/wallet', 303)

    @app.post('/queue/{item_id}/{operation}')
    async def resolve_queue(item_id: int, operation: str, request: Request, user=Depends(authenticate)):
        form = await form_data(request, user)
        item = db.get_queue(item_id)
        if not item or item['status'] != 'pending':
            raise HTTPException(409, 'Element nie jest już oczekujący')
        if operation in ('approve', 'reject'):
            if not db.resolve_queue(item_id, 'approved' if operation == 'approve' else 'rejected'):
                raise HTTPException(409, 'Element został już rozpatrzony')
        elif operation == 'edit':
            content = str(form.get('content', '')).strip()
            if not content or len(content) > 8000:
                raise HTTPException(422, 'Wpisz treść (maks. 8000 znaków)')
            intent = dict(item['intent'])
            if 'body' not in intent:
                raise HTTPException(422, 'Ta akcja nie zawiera edytowalnej treści')
            intent['body'] = content
            try:
                changed = db.edit_queue(item_id, intent)
            except ValueError:
                raise HTTPException(422, 'Treść nie spełnia wymagań akcji') from None
            if not changed:
                raise HTTPException(409, 'Element został już rozpatrzony')
        else:
            raise HTTPException(404, 'Nieznana operacja')
        db.log('queue_review', {'id': item_id, 'operation': operation})
        return RedirectResponse('/queue', 303)

    @app.get('/{page:path}')
    async def view(request: Request, page: str, user=Depends(authenticate)):
        if page not in PAGES:
            raise HTTPException(404)
        events = db.events(limit=300)
        if page in FILTERS:
            events = [e for e in events if any(word in e['kind'] for word in FILTERS[page])]
        elif page == 'persona':
            events = db.events(kind='persona_version', limit=100)
        pending = db.queue_items(status='pending')
        import time
        last_cycle=db.last_event('cycle'); now=time.time()
        limits={'hour':getattr(settings,'llm_hourly_tokens',0),'day':getattr(settings,'llm_daily_tokens',0),'week':getattr(settings,'llm_weekly_tokens',0)}
        usage={'hour':db.llm_tokens_since(now-3600),'day':db.llm_tokens_since(now-86400),'week':db.llm_tokens_since(now-7*86400)}
        budget={key:{'used':usage[key],'limit':limits[key],'remaining':max(0,limits[key]-usage[key]) if limits[key] else None} for key in limits}
        retry_state=db.get_setting('llm_retry_state',{}) or {}
        last_ok=next((e for e in db.events('llm',1000) if e['data'].get('status')=='ok' and e['data'].get('task')=='triage'),None)
        throttle_until=(last_ok['created_at']+getattr(settings,'ordinary_cadence_seconds',3600)) if last_ok else 0
        next_eligible=max(now, retry_state.get('blocked_until') or 0, throttle_until)
        import datetime as _dt
        next_eligible_text='teraz' if next_eligible<=now+1 else _dt.datetime.fromtimestamp(next_eligible,tz=_dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        results = safe(_build_results(db)) if page == 'results' else {}
        context = {'request': request, 'page': page, 'pages': PAGES, 'title': PAGES[page], 'csrf': csrf_token(user), 'events': safe(events), 'pending': safe(pending), 'mode': db.get_setting('mode', settings.mode), 'persona': safe(db.get_setting('persona', '')), 'content_prompt': safe(db.get_setting('content_prompt', '')), 'handle': getattr(settings, 'handle', ''), 'model': getattr(settings, 'ollama_model', ''), 'has_key': bool(settings.api_key), 'payout_address': getattr(settings, 'payout_address', ''), 'wallet_preimage': safe(db.get_setting('payout_wallet_preimage')), 'wallet_submission': db.get_setting('payout_wallet_submission'), 'budget':budget, 'llm_token_limits_enabled':db.get_setting('llm_token_limits_enabled',getattr(settings,'llm_token_limits_enabled',False)), 'last_cycle':safe(last_cycle), 'worker_stale':not last_cycle or now-last_cycle['created_at']>max(180,getattr(settings,'cycle_seconds',900)*2+60), 'next_eligible_analysis':next_eligible_text, 'results':results}
        return templates.TemplateResponse(request=request, name='panel.html', context=context)

    return app


app = create_app()
