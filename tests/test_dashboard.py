import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def panel(tmp_path):
    from dashboard.app import create_app
    from f916.db import Database
    db = Database(tmp_path / 'agent.sqlite3')
    db.initialize()
    settings = SimpleNamespace(data_dir=tmp_path, dashboard_user='admin', dashboard_password='test-password', api_key='top-secret-key', mode='approve', handle='tester', ollama_model='deepseek-v4-flash:cloud', payout_address='0x'+'12'*20)
    with TestClient(create_app(settings, db), base_url='http://localhost') as client:
        yield client, db


def token(client):
    page = client.get('/', auth=('admin', 'test-password'))
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_authentication_required_and_secrets_escaped(panel):
    client, db = panel
    db.log('action', {'text': '<script>alert(1)</script>', 'api_key': 'top-secret-key'})
    assert client.get('/').status_code == 401
    response = client.get('/actions', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert '<script>' not in response.text
    assert 'top-secret-key' not in response.text
    assert '&lt;script&gt;' in response.text


def test_csrf_and_origin_prevent_mode_changes(panel):
    client, db = panel
    auth = ('admin', 'test-password')
    csrf = token(client)
    assert client.post('/settings', auth=auth, data={'mode': 'auto'}).status_code == 403
    assert client.post('/settings', auth=auth, headers={'Origin': 'https://evil.example'}, data={'mode': 'auto', 'csrf': csrf}).status_code == 403
    assert db.get_setting('mode') is None
    assert client.post('/settings', auth=auth, data={'mode': 'auto', 'csrf': csrf}).status_code == 200
    assert db.get_setting('mode') == 'auto'


def test_loopback_origin_alias_can_change_mode(panel):
    client, db = panel
    auth = ('admin', 'test-password')
    csrf = token(client)
    response = client.post('/settings', auth=auth,
                           headers={'Origin':'http://127.0.0.1'},
                           data={'mode':'off','csrf':csrf})
    assert response.status_code == 200
    assert db.get_setting('mode') == 'off'


def test_sandboxed_browser_null_origin_requires_valid_csrf(panel):
    client, db = panel
    auth = ('admin', 'test-password')
    csrf = token(client)
    response = client.post('/settings', auth=auth, headers={'Origin':'null'},
                           data={'mode':'auto','csrf':csrf})
    assert response.status_code == 200
    assert db.get_setting('mode') == 'auto'
    assert client.post('/settings', auth=auth, headers={'Origin':'null'},
                       data={'mode':'off','csrf':'wrong'}).status_code == 403


def test_approve_only_marks_queue_and_persona_is_versioned(panel):
    client, db = panel
    auth = ('admin', 'test-password')
    csrf = token(client)
    item = db.queue({'action': 'comment', 'post_id': 1, 'body': 'test'}, 'review')
    assert client.post(f'/queue/{item}/approve', auth=auth, data={'csrf': csrf}).status_code == 200
    assert db.get_queue(item)['status'] == 'approved'
    assert client.post('/persona', auth=auth, data={'csrf': csrf, 'persona': 'Pomocny audytor', 'content_prompt': 'Pisz jasno.'}).status_code == 200
    assert db.get_setting('persona') == 'Pomocny audytor'
    assert len(db.events(kind='persona_version')) == 1


def test_edit_queue_changes_only_body_and_rejects_invalid_body(panel):
    client, db = panel
    auth = ('admin', 'test-password')
    csrf = token(client)
    item = db.queue({'action': 'comment', 'post_id': 1, 'body': 'old'}, 'review')
    assert client.post(f'/queue/{item}/edit', auth=auth, data={'csrf': csrf, 'content': 'new'}).status_code == 200
    assert db.get_queue(item)['intent']['body'] == 'new'
    assert db.get_queue(item)['intent']['post_id'] == 1
    assert db.get_queue(item)['status'] == 'pending'
    assert client.post(f'/queue/{item}/edit', auth=auth, data={'csrf': csrf, 'content': ' '}).status_code == 422


def test_unconfigured_password_fails_closed(tmp_path):
    from dashboard.app import create_app
    from f916.db import Database
    settings = SimpleNamespace(data_dir=tmp_path, dashboard_user='admin', dashboard_password='', api_key='', mode='off')
    with TestClient(create_app(settings, Database(tmp_path / 'db'))) as client:
        assert client.get('/', auth=('admin', '')).status_code == 503


def test_health_and_budget_status_are_visible(panel):
    client, db = panel
    assert client.get('/healthz').json() == {'status':'ok'}
    page=client.get('/settings',auth=('admin','test-password'))
    assert page.status_code == 200
    assert 'Stan workera i budżet Ollama' in page.text


def test_results_page_renders_on_empty_db(panel):
    client, db = panel
    response = client.get('/results', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert '0.00 USD' in response.text
    assert '0%' in response.text


def test_results_page_shows_earnings_and_win_rate(panel):
    client, db = panel
    db.log('outcome', {'listing_id': 1, 'submission_id': 1, 'state': 'paid', 'amount_atomic': '1000000'})
    metrics = db.get_setting('reward_metrics', {})
    metrics['outside_funded_earnings_usd'] = 12.5
    db.set_setting('reward_metrics', metrics)
    response = client.get('/results', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert '12.50 USD' in response.text
    assert '100.0%' in response.text


def test_results_page_counts_template_hits(panel):
    client, db = panel
    db.log('opportunity', {'listing_id': 2, 'classification': 'supported', 'template_id': 'rail-report-v1'})
    response = client.get('/results', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert 'rail-report-v1' in response.text
    assert 'Trafienia szablonów: <strong>1</strong>' in response.text


def test_results_page_shows_market_section_on_empty_db(panel):
    client, db = panel
    response = client.get('/results', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert 'Rynek' in response.text
    assert 'Brak zaobserwowanych zarobków.' in response.text
    assert 'Brak wykrytych luk.' in response.text


def test_results_page_reflects_market_intel_and_capability_gap(panel):
    client, db = panel
    db.log('market_intel', {'earners': {'rival': {'paid_count': 3, 'total_atomic': 4200000}}, 'gaps': []})
    db.log('capability_gap', {
        'class_key': 'acme|cadence-report-weekly',
        'funder': 'Acme',
        'sample_listing_id': 1,
        'sample_title': 'Weekly Cadence <script>alert(1)</script>',
        'paid_handles': ['rival'],
        'paid_total_atomic': 4200000,
        'our_classification': 'unsupported',
        'suggestion': 'template',
    })
    response = client.get('/results', auth=('admin', 'test-password'))
    assert response.status_code == 200
    assert 'rival' in response.text
    assert '4200000' in response.text
    assert 'acme|cadence-report-weekly' in response.text
    assert 'template' in response.text
    # Listing prose is DATA, never markup or a command: it must render escaped.
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text


def test_token_limit_toggle_is_persisted(panel):
    client,db=panel; auth=('admin','test-password'); csrf=token(client)
    response=client.post('/settings/token-limits',auth=auth,data={'token_limits_form':'1','enabled':'true','csrf':csrf})
    assert response.status_code==200 and db.get_setting('llm_token_limits_enabled') is True
    csrf=token(client)
    response=client.post('/settings',auth=auth,data={'mode':'auto','csrf':csrf})
    assert response.status_code==200 and db.get_setting('llm_token_limits_enabled') is True
    csrf=token(client)
    response=client.post('/settings/token-limits',auth=auth,data={'token_limits_form':'1','csrf':csrf})
    assert response.status_code==200 and db.get_setting('llm_token_limits_enabled') is False
