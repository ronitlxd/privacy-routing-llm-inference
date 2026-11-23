"""Compatibility: the Ollama client from the .env settings (JLR_OLLAMA_URL).

The app now reaches every model through jlr.models (models.toml) and jlr.transports. This module keeps
the old names for callers and scripts that still import them (e.g. the benchmark scripts).
"""
import os

from .models import LOCAL_MODEL, Endpoint
from .transports import for_endpoint

MODEL = os.environ.get("JLR_ROUTER_MODEL", LOCAL_MODEL)
OLLAMA = os.environ.get("JLR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def request_json(path, payload=None, timeout=240):
    # Ollama receives unmasked prompts: loopback HTTP or verified HTTPS only (checked on every call).
    endpoint = Endpoint("ollama", "ollama", OLLAMA, "on_vehicle", ca_file=os.environ.get("JLR_OLLAMA_CA_FILE", ""), setting="JLR_OLLAMA_URL")
    return for_endpoint(endpoint)(path, payload, timeout)
