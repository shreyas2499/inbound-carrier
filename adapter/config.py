"""Runtime configuration, loaded from environment variables (see .env.example).

The repo-root `.env` is auto-loaded if present, so `flask run` and the smoke
script both pick up local creds without any manual exporting, regardless of the
working directory they're launched from. In Docker the vars come from
docker-compose's env_file, so a missing python-dotenv is harmless.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # python-dotenv absent (e.g. minimal runtime) — real env still works
    pass

from adapter.tms_client import TmsClient


@dataclass
class Config:
    tms_host: str
    tms_port: int
    tms_token: str
    fmcsa_api_key: str
    adapter_api_key: str
    tms_timeout: float
    tms_max_retries: int
    # --- HappyRobot Twin (managed Postgres via REST) ------------------------
    # Optional: when twin_api_key is blank, every twin_helper call is a no-op,
    # so the adapter runs perfectly fine before these creds are ever fetched.
    twin_api_key: str
    twin_api_base: str
    twin_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            tms_host=os.environ.get("TMS_HOST", ""),
            tms_port=int(os.environ.get("TMS_PORT", "0") or 0),
            tms_token=os.environ.get("TMS_AUTH_TOKEN", ""),
            fmcsa_api_key=os.environ.get("FMCSA_API_KEY", ""),
            adapter_api_key=os.environ.get("ADAPTER_API_KEY", ""),
            tms_timeout=float(os.environ.get("TMS_CLIENT_TIMEOUT_SECONDS", "8")),
            tms_max_retries=int(os.environ.get("TMS_MAX_RETRIES", "2")),
            twin_api_key=os.environ.get("HAPPYROBOT_API_KEY", ""),
            twin_api_base=os.environ.get(
                "HAPPYROBOT_API_BASE", "https://platform.happyrobot.ai/api/v2"
            ).rstrip("/"),
            twin_timeout=float(os.environ.get("TWIN_TIMEOUT_SECONDS", "6")),
        )

    def make_client(self) -> TmsClient:
        return TmsClient(
            self.tms_host, self.tms_port, self.tms_token,
            timeout=self.tms_timeout, max_retries=self.tms_max_retries,
        )
