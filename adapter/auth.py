"""API-key gate for the adapter's tool endpoints."""
from __future__ import annotations

import functools

from flask import current_app, jsonify, request


def require_api_key(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config["ADAPTER_CONFIG"].adapter_api_key
        if expected and request.headers.get("X-API-Key") != expected:
            return jsonify(error="unauthorized"), 401
        return fn(*args, **kwargs)
    return wrapper
