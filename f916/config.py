import os
from pathlib import Path
from dataclasses import dataclass, field

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
    cycle_seconds: int = field(default_factory=lambda: int(os.getenv('CYCLE_SECONDS','900')))
    llm_daily_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_DAILY_TOKENS','100000')))
    llm_hourly_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_HOURLY_TOKENS','30000')))
    llm_weekly_tokens: int = field(default_factory=lambda: int(os.getenv('LLM_WEEKLY_TOKENS','500000')))
    llm_daily_budget_usd: float = field(default_factory=lambda: float(os.getenv('LLM_DAILY_BUDGET_USD','3')))
    llm_input_usd_per_million: float = field(default_factory=lambda: float(os.getenv('LLM_INPUT_USD_PER_MILLION','0')))
    llm_output_usd_per_million: float = field(default_factory=lambda: float(os.getenv('LLM_OUTPUT_USD_PER_MILLION','0')))
    def __post_init__(self):
        self.data_dir=Path(self.data_dir)
        if self.mode not in {'off','approve','auto'}: raise ValueError('Invalid mode')
        secret=self.data_dir/'citizen-secret.txt'
        if not self.api_key and secret.exists(): self.api_key=secret.read_text().strip()

