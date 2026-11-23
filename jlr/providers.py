"""Compatibility: the hosted larger-model providers from the .env settings.

  JLR_V2_LARGER_PROVIDER = ollama (default) | anthropic | openai
  JLR_V2_LARGER_MODEL    = the provider's model name
  JLR_LARGER_API_KEY     = API key (or ANTHROPIC_API_KEY / OPENAI_API_KEY)
  JLR_LARGER_BASE_URL    = optional; for openai, any OpenAI-compatible server (vLLM, Groq, ...)

The app now reads endpoints and roles from jlr.models (models.toml, or these settings when there is no
models file) and calls them through jlr.transports. These names stay for older callers.
"""
import os

from .models import DEFAULT_URLS as _URLS, PROVIDERS, Endpoint
from .transports import for_endpoint, json_text, schema_instruction  # noqa: F401  (re-exported)

DEFAULT_URLS = {name: url for name, url in _URLS.items() if name != 'ollama'}


def larger_provider():
    provider = os.environ.get('JLR_V2_LARGER_PROVIDER', 'ollama').strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f'JLR_V2_LARGER_PROVIDER must be one of {", ".join(PROVIDERS)}')
    return provider


def _endpoint(provider):
    return Endpoint('larger', provider, os.environ.get('JLR_LARGER_BASE_URL', DEFAULT_URLS[provider]).rstrip('/'), 'external',
                    ('JLR_LARGER_API_KEY', f'{provider.upper()}_API_KEY'), setting='JLR_LARGER_BASE_URL')


def api_key(provider):
    return _endpoint(provider).api_key()


def anthropic_transport(path, payload, timeout=240):
    return for_endpoint(_endpoint('anthropic'))(path, payload, timeout)


def openai_transport(path, payload, timeout=240):
    return for_endpoint(_endpoint('openai'))(path, payload, timeout)


TRANSPORTS = {'anthropic': anthropic_transport, 'openai': openai_transport}
