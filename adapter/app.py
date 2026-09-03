"""Application factory — wires config, the TMS client, a short-lived load cache,
and the routes together."""
from __future__ import annotations

from flask import Flask

from adapter.cache import TTLCache
from adapter.config import Config
from adapter.routes import bp
from adapter.tms_client import TmsClient


def create_app(client: TmsClient | None = None, config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    app = Flask(__name__)
    app.config["ADAPTER_CONFIG"] = config
    app.config["TMS_CLIENT"] = client or config.make_client()
    app.config["LOAD_CACHE"] = TTLCache(ttl_seconds=90)
    app.register_blueprint(bp)
    return app
