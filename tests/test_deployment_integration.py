"""Static deployment contracts: no runtime secret is needed to run these tests."""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def deployment_file(name):
    """Deployment sources are intentionally excluded from the runtime image."""
    path = ROOT / name
    if not path.exists():
        pytest.skip(f'{name} is a checkout-only deployment source')
    return path


def test_compose_keeps_github_token_out_of_worker_and_dashboard():
    compose = deployment_file('compose.yaml').read_text(encoding='utf-8')
    broker = compose[compose.index('  broker:'):compose.index('  # Isolated test sandbox runner')]
    assert 'env_file:' not in broker
    assert './secrets/github_token:/run/secrets/github_token:ro' in broker
    assert 'BROKER_TOKEN: ${BROKER_TOKEN:-}' in broker
    assert 'MODE: ${MODE:-approve}' in compose
    assert 'healthcheck:' in compose[compose.index('  worker:'):compose.index('  dashboard:')]


def test_example_configuration_keeps_broker_dormant():
    example = deployment_file('.env.example').read_text(encoding='utf-8')
    assert 'BROKER_URL=\n' in example
    assert 'BROKER_TOKEN=\n' in example
    assert 'PROJECT_LLM_ENABLED=false' in example


def test_gitignore_excludes_operator_github_secret():
    gitignore = deployment_file('.gitignore').read_text(encoding='utf-8')
    assert 'secrets/' in gitignore.splitlines()
