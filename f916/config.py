import os
from pathlib import Path
from dataclasses import dataclass, field

def _bool_env(name, default='false'):
    return os.getenv(name,default).strip().lower() in {'1','true','yes','on'}

@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR','data')))
    api_base: str = field(default_factory=lambda: os.getenv('API_BASE','https://1f916.ai'))
    api_key: str = field(default_factory=lambda: os.getenv('API_KEY',''), repr=False)
    handle: str = field(default_factory=lambda: os.getenv('HANDLE',''))
    ollama_url: str = field(default_factory=lambda: os.getenv('OLLAMA_URL','http://host.docker.internal:11434'))
    ollama_model: str = field(default_factory=lambda: os.getenv('OLLAMA_MODEL','deepseek-v4-flash:cloud'))
    mode: str = field(default_factory=lambda: os.getenv('MODE','approve'))
    dashboard_user: str = field(default_factory=lambda: os.getenv('DASHBOARD_USER','admin'))
    dashboard_password: str = field(default_factory=lambda: os.getenv('DASHBOARD_PASSWORD',''), repr=False)
    payout_address: str = field(default_factory=lambda: os.getenv('PAYOUT_ADDRESS',''))
    github_repo: str = field(default_factory=lambda: os.getenv('GITHUB_REPO','erku/erku-audit'))
    cycle_seconds: int = field(default_factory=lambda: int(os.getenv('CYCLE_SECONDS','300')))
    llm_daily_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_DAILY_TOKENS','100000')))
    llm_hourly_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_HOURLY_TOKENS','30000')))
    llm_weekly_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_WEEKLY_TOKENS','500000')))
    llm_token_limits_enabled: bool = field(default_factory=lambda: _bool_env('LLM_TOKEN_LIMITS_ENABLED','false'))
    llm_daily_budget_usd: float = field(default_factory=lambda: float(os.getenv('LLM_DAILY_BUDGET_USD','3')))
    llm_input_usd_per_million: float = field(default_factory=lambda: float(os.getenv('LLM_INPUT_USD_PER_MILLION','0')))
    llm_output_usd_per_million: float = field(default_factory=lambda: float(os.getenv('LLM_OUTPUT_USD_PER_MILLION','0')))
    llm_retry_base_seconds: int = field(default_factory=lambda: int(os.getenv('LLM_RETRY_BASE_SECONDS','60')))
    llm_retry_cap_seconds: int = field(default_factory=lambda: int(os.getenv('LLM_RETRY_CAP_SECONDS','3600')))
    market_scan_interval: int = field(default_factory=lambda: int(os.getenv('MARKET_SCAN_INTERVAL','1800')))
    ordinary_cadence_seconds: int = field(default_factory=lambda: int(os.getenv('ORDINARY_CADENCE_SECONDS','3600')))
    urgent_cadence_seconds: int = field(default_factory=lambda: int(os.getenv('URGENT_CADENCE_SECONDS','1800')))
    # Autonomous project pipeline (Task B1): inert unless BOTH broker_url and
    # broker_token are set (see f916/opportunities.py OpportunityRunner.process).
    broker_url: str = field(default_factory=lambda: os.getenv('BROKER_URL',''))
    broker_token: str = field(default_factory=lambda: os.getenv('BROKER_TOKEN',''), repr=False)
    max_projects: int = field(default_factory=lambda: int(os.getenv('MAX_PROJECTS','3')))
    # Deterministic paid-verifier engine (Task V): computing + signing a
    # verdict is always safe, but posting one requires both this flag AND a
    # confirmed submission endpoint (not yet wired) -- see f916/verifier.py.
    verifier_enabled: bool = field(default_factory=lambda: _bool_env('VERIFIER_ENABLED','false'))
    # LLM-authored projects (Task P): the model may author a small project's
    # files as DATA, but that path is inert unless this flag is set AND the
    # broker is configured -- see f916/opportunities.py OpportunityRunner.run_project.
    project_llm_enabled: bool = field(default_factory=lambda: _bool_env('PROJECT_LLM_ENABLED','false'))
    # Isolated test sandbox (Task S): the worker drops jobs here for
    # sandbox/runner.py, which runs in a separate, network-less,
    # secret-less container and communicates back via this same directory.
    sandbox_jobs_dir: Path = field(default_factory=lambda: Path(os.getenv('SANDBOX_JOBS_DIR')) if os.getenv('SANDBOX_JOBS_DIR') else None)
    sandbox_timeout_seconds: int = field(default_factory=lambda: int(os.getenv('SANDBOX_TIMEOUT_SECONDS','120')))
    # Self-extension engine (Task X): tier-1 auto-templates and tier-2
    # sandboxed skill PROPOSALS -- see f916/selfext.py. Proposals are always
    # inert data for human review; self_extend_automerge only changes a
    # proposal's recorded status, never triggers a git merge or redeploy.
    self_extend_enabled: bool = field(default_factory=lambda: _bool_env('SELF_EXTEND_ENABLED','false'))
    self_extend_automerge: bool = field(default_factory=lambda: _bool_env('SELF_EXTEND_AUTOMERGE','false'))
    self_extend_max_templates: int = field(default_factory=lambda: int(os.getenv('SELF_EXTEND_MAX_TEMPLATES','8')))
    self_extend_max_proposals_per_day: int = field(default_factory=lambda: int(os.getenv('SELF_EXTEND_MAX_PROPOSALS_PER_DAY','1')))
    def __post_init__(self):
        self.data_dir=Path(self.data_dir)
        if self.mode not in {'off','approve','auto'}: raise ValueError('Invalid mode')
        secret=self.data_dir/'citizen-secret.txt'
        if not self.api_key and secret.exists(): self.api_key=secret.read_text().strip()
        if self.sandbox_jobs_dir is None:
            self.sandbox_jobs_dir=self.data_dir/'sandbox-jobs'
        else:
            self.sandbox_jobs_dir=Path(self.sandbox_jobs_dir)

