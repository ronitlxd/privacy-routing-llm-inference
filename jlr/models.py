"""Where each model runs and how to reach it: one file for the router, the local SLM and the larger model.

models.toml (repo root, or the file named by JLR_MODELS_FILE) has two parts:

  [endpoints.<name>]   a model server: provider (ollama | openai | anthropic), url, trust, api_key_env, ca_file
  [roles.router]       the model that reads the UNMASKED prompt and picks the route
  [roles.local]        the small model that answers simple or sensitive requests, UNMASKED
  [roles.larger]       the larger model; it only ever receives MASKED text

`openai` means any OpenAI-compatible server (vLLM, TGI, LM Studio, llama.cpp, Ollama's /v1, OpenAI itself),
so a model can be local or hosted: only the url changes. models.example.toml shows every key.

Privacy rules, enforced here at start-up (the server refuses to start) and again on every call:
  - the router and local roles see unmasked private data, so their endpoint must be trust = "on_vehicle";
  - an on_vehicle endpoint is either this machine (loopback) or a private HTTPS server - a known public
    model API is refused even if someone labels it on_vehicle;
  - every remote address is HTTPS with certificate checks (links.check_link);
  - API keys are never written in the file: `api_key_env` names the environment variable that holds one.

Without a models file, the same three roles are built from the older .env settings
(JLR_OLLAMA_URL, JLR_ROUTER_MODEL, JLR_V2_LOCAL_MODEL, JLR_V2_LARGER_*), so existing deployments
behave exactly as before.
"""
from __future__ import annotations

import json
import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .links import check_link, is_local

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FILE = REPO / 'models.toml'
PROVIDERS = ('ollama', 'openai', 'anthropic')
TRUST = ('on_vehicle', 'external')
ROLES = ('router', 'local', 'larger')
UNMASKED_ROLES = ('router', 'local')
LOCAL_MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
LARGER_MODEL = 'hermes3:8b'
DEFAULT_URLS = {'ollama': 'http://127.0.0.1:11434', 'openai': 'https://api.openai.com/v1', 'anthropic': 'https://api.anthropic.com/v1'}
DEFAULT_TIMEOUTS = {'router': 45.0, 'local': 90.0, 'larger': 150.0}
# Public hosted model APIs. An endpoint on one of these is never "on_vehicle": unmasked text would leave the vehicle.
PUBLIC_MODEL_APIS = ('api.openai.com', 'api.anthropic.com', 'api.groq.com', 'generativelanguage.googleapis.com',
                     'aiplatform.googleapis.com', 'api.together.xyz', 'api.together.ai', 'openrouter.ai', 'api.mistral.ai',
                     'api.cohere.com', 'api.cohere.ai', 'api.deepseek.com', 'api.fireworks.ai', 'api.perplexity.ai', 'api.x.ai',
                     'openai.azure.com', 'models.inference.ai.azure.com', 'api-inference.huggingface.co',
                     'router.huggingface.co', 'api.deepinfra.com', 'api.replicate.com', 'api.cerebras.ai')
ENDPOINT_KEYS = {'provider', 'url', 'trust', 'api_key_env', 'ca_file'}
ROLE_KEYS = {'endpoint', 'model', 'timeout', 'gpu_layers', 'threads', 'context', 'max_output_tokens',
             'temperature', 'seed', 'untuned_ok', 'warmup'}


def public_model_api(host):
    host = (host or '').lower().rstrip('.')
    return host.startswith('bedrock-runtime.') or any(host == d or host.endswith('.' + d) for d in PUBLIC_MODEL_APIS)


@dataclass(frozen=True)
class Endpoint:
    name: str
    provider: str
    url: str
    trust: str
    api_key_env: tuple = ()
    ca_file: str = ''
    setting: str = ''        # where the url came from, for error messages

    @property
    def host(self):
        return urlparse(self.url).hostname or ''

    def label(self):
        return self.setting or f'models file [endpoints.{self.name}] url'

    def api_key(self):
        """The key from the first named environment variable that is set (None if no key is configured)."""
        for name in self.api_key_env:
            if os.environ.get(name): return os.environ[name]
        if self.api_key_env:
            raise ValueError(f'Set {self.api_key_env[0]}' + (f' (or {", ".join(self.api_key_env[1:])})' if self.api_key_env[1:] else '')
                             + f' to use the {self.provider} provider at {self.host}.')
        return None

    def where(self):
        """(place, encrypted link?) as shown in privacy receipts."""
        if is_local(self.url): return 'this server', True
        return self.host, urlparse(self.url).scheme == 'https'

    def check_link(self):
        """Raise OSError unless the address is loopback or HTTPS (checked before every call: fail closed)."""
        try: check_link(self.url, self.label())
        except ValueError as exc: raise OSError(str(exc)) from None


@dataclass(frozen=True)
class Role:
    name: str
    endpoint: Endpoint
    model: str
    timeout: float
    gpu_layers: int | None = -1     # Ollama only: -1 = as many layers as fit on the GPU, 0 = CPU only
    threads: int = 4
    context: int = 4096
    max_output_tokens: int = 512
    temperature: float = 0.0
    seed: int = 42
    untuned_ok: bool = False         # router only: allow a model the routing profile was not tuned for
    warmup: bool = True

    def options(self, output_tokens=None):
        """Runner options in Ollama's shape; the other transports use temperature, seed and num_predict."""
        options = {'temperature': self.temperature, 'seed': self.seed, 'num_ctx': self.context, 'num_thread': self.threads,
                   'num_predict': self.max_output_tokens if output_tokens is None else output_tokens}
        if self.gpu_layers is not None: options['num_gpu'] = self.gpu_layers
        return options

    @property
    def transport(self):
        from .transports import for_endpoint
        return for_endpoint(self.endpoint)

    def describe(self):
        place, encrypted = self.endpoint.where()
        return {'endpoint': self.endpoint.name, 'provider': self.endpoint.provider, 'where': place,
                'encrypted_link': encrypted, 'trust': self.endpoint.trust, 'model': self.model}


@dataclass(frozen=True)
class Config:
    endpoints: dict
    roles: dict
    source: str
    warnings: tuple = field(default=())

    def describe(self):
        """Shown by /api/health, which anyone who can open the site may read: the file name only, never its path."""
        return {'source': Path(self.source).name, 'roles': {name: role.describe() for name, role in self.roles.items()},
                'warnings': list(self.warnings)}


# ------------------------------------------------------------------ loading
def _number(value, kind, where):
    try:
        if isinstance(value, bool): raise TypeError
        return kind(value)
    except (TypeError, ValueError):
        raise ValueError(f'{where} must be a number, not {value!r}.') from None


def _names(value, where):
    if isinstance(value, str): value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f'{where} must be the name of an environment variable (or a list of names).')
    return tuple(value)


def from_file(path):
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError(f'Models file {path} does not exist.') from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f'Models file {path} is not valid TOML: {exc}') from None
    unknown = set(data) - {'endpoints', 'roles'}
    if unknown: raise ValueError(f'Models file: unknown section(s) {", ".join(sorted(unknown))}; use [endpoints.<name>] and [roles.<role>].')
    endpoints = {}
    for name, raw in (data.get('endpoints') or {}).items():
        where = f'[endpoints.{name}]'
        if not isinstance(raw, dict): raise ValueError(f'{where} must be a table.')
        if 'api_key' in raw:
            raise ValueError(f'{where}: never write an API key in the models file. Put it in an environment variable '
                             f'(or .env) and give its name with api_key_env = "MY_KEY_VARIABLE".')
        bad = set(raw) - ENDPOINT_KEYS
        if bad: raise ValueError(f'{where}: unknown key(s) {", ".join(sorted(bad))}; allowed: {", ".join(sorted(ENDPOINT_KEYS))}.')
        provider = str(raw.get('provider', '')).strip().lower()
        if provider not in PROVIDERS: raise ValueError(f'{where} provider must be one of {", ".join(PROVIDERS)}.')
        if 'trust' not in raw:
            raise ValueError(f'{where} needs trust = "on_vehicle" (private: may see unmasked data) or "external" (masked text only).')
        ca = str(raw.get('ca_file', ''))
        if ca and not Path(ca).is_absolute(): ca = str((path.parent / ca).resolve())
        endpoints[name] = Endpoint(name, provider, str(raw.get('url', DEFAULT_URLS[provider])).rstrip('/'), str(raw['trust']),
                                   _names(raw['api_key_env'], f'{where} api_key_env') if 'api_key_env' in raw else (), ca)
    roles = {}
    for name, raw in (data.get('roles') or {}).items():
        where = f'[roles.{name}]'
        if name not in ROLES: raise ValueError(f'{where}: unknown role; the roles are {", ".join(ROLES)}.')
        bad = set(raw) - ROLE_KEYS
        if bad: raise ValueError(f'{where}: unknown key(s) {", ".join(sorted(bad))}; allowed: {", ".join(sorted(ROLE_KEYS))}.')
        if raw.get('endpoint') not in endpoints:
            raise ValueError(f'{where} endpoint must name one of the [endpoints.*] sections ({", ".join(endpoints) or "none defined"}).')
        if not raw.get('model'): raise ValueError(f'{where} needs a model name.')
        endpoint = endpoints[raw['endpoint']]
        gpu = raw.get('gpu_layers', -1 if endpoint.provider == 'ollama' else None)
        roles[name] = Role(name, endpoint, str(raw['model']), _number(raw.get('timeout', DEFAULT_TIMEOUTS[name]), float, f'{where} timeout'),
                           None if gpu is None else _number(gpu, int, f'{where} gpu_layers'),
                           _number(raw.get('threads', 4), int, f'{where} threads'), _number(raw.get('context', 4096), int, f'{where} context'),
                           _number(raw.get('max_output_tokens', 512), int, f'{where} max_output_tokens'),
                           _number(raw.get('temperature', 0), float, f'{where} temperature'), _number(raw.get('seed', 42), int, f'{where} seed'),
                           bool(raw.get('untuned_ok', False)), bool(raw.get('warmup', True)))
    missing = [r for r in ROLES if r not in roles]
    if missing: raise ValueError(f'Models file: missing role(s) {", ".join(missing)} ([roles.router], [roles.local], [roles.larger]).')
    return Config(endpoints, roles, str(path))


def from_env(env):
    """The three roles from the older .env settings (no models file): identical to the behaviour before models.toml."""
    ollama = Endpoint('ollama', 'ollama', env.get('JLR_OLLAMA_URL', DEFAULT_URLS['ollama']).rstrip('/'), 'on_vehicle',
                      ca_file=env.get('JLR_OLLAMA_CA_FILE', ''), setting='JLR_OLLAMA_URL')
    provider = env.get('JLR_V2_LARGER_PROVIDER', 'ollama').strip().lower()
    if provider not in PROVIDERS: raise ValueError(f'JLR_V2_LARGER_PROVIDER must be one of {", ".join(PROVIDERS)}')
    larger_endpoint = ollama if provider == 'ollama' else Endpoint(
        'larger', provider, env.get('JLR_LARGER_BASE_URL', DEFAULT_URLS[provider]).rstrip('/'), 'external',
        ('JLR_LARGER_API_KEY', f'{provider.upper()}_API_KEY'), setting='JLR_LARGER_BASE_URL')
    gpu = int(env.get('JLR_NUM_GPU', '-1')); threads = int(env.get('JLR_THREADS', '4')); context = int(env.get('JLR_V2_CONTEXT', '4096'))
    larger_model = env.get('JLR_V2_LARGER_MODEL', LARGER_MODEL)
    # Before the models file, a local Hermes always ran on the CPU; JLR_LARGER_NUM_GPU now makes that a setting.
    larger_gpu = int(env.get('JLR_LARGER_NUM_GPU', '0' if larger_model.startswith('hermes') else str(gpu)))
    common = {'threads': threads, 'context': context}
    roles = {
        'router': Role('router', ollama, env.get('JLR_ROUTER_MODEL', LOCAL_MODEL), float(env.get('JLR_V2_ROUTER_DEADLINE', '300')),
                       gpu, untuned_ok=env.get('JLR_ROUTER_UNTUNED_OK') == '1', **common),
        'local': Role('local', ollama, env.get('JLR_V2_LOCAL_MODEL', LOCAL_MODEL), float(env.get('JLR_V2_LOCAL_DEADLINE', '90')), gpu, **common),
        'larger': Role('larger', larger_endpoint, larger_model, float(env.get('JLR_V2_LARGER_DEADLINE', '300')),
                       larger_gpu if provider == 'ollama' else None, **common),
    }
    endpoints = {ollama.name: ollama, larger_endpoint.name: larger_endpoint}
    return Config(endpoints, roles, '.env settings (no models file)')


def models_file(environ):
    """The models file to use, or None for the .env settings."""
    named = environ.get('JLR_MODELS_FILE', '').strip()
    if named:
        path = Path(named)
        return path if path.is_absolute() else REPO / path
    return DEFAULT_FILE if environ is os.environ and DEFAULT_FILE.is_file() else None


def load(environ=None):
    environ = os.environ if environ is None else environ
    path = models_file(environ)
    return from_file(path) if path else from_env(environ)


# ------------------------------------------------------------------ checks
def check(config, *, require_keys=True, routing_profile=False):
    """Enforce the privacy rules; raise ValueError on the first problem. Returns the config with any warnings.

    routing_profile: False = skip; None = the default profile (or JLR_ROUTING_PROFILE); or a path.
    """
    for endpoint in config.endpoints.values():
        check_link(endpoint.url, endpoint.label())
        if endpoint.trust not in TRUST:
            raise ValueError(f'{endpoint.label()}: trust must be "on_vehicle" or "external", not {endpoint.trust!r}.')
        if endpoint.trust == 'on_vehicle' and not is_local(endpoint.url) and public_model_api(endpoint.host):
            raise ValueError(f'{endpoint.label()}: {endpoint.host} is a public hosted model API, so it cannot be "on_vehicle". '
                             'Unmasked private data would leave the vehicle. Use it only as trust = "external" for the larger model.')
        if endpoint.ca_file and not Path(endpoint.ca_file).is_file():
            raise ValueError(f'{endpoint.label()}: ca_file {endpoint.ca_file} does not exist.')
        if endpoint.provider == 'anthropic' and not endpoint.api_key_env:
            raise ValueError(f'{endpoint.label()}: the anthropic provider needs api_key_env (the name of the variable holding the key).')
    for name in UNMASKED_ROLES:
        role = config.roles[name]
        if role.endpoint.trust != 'on_vehicle':
            raise ValueError(f'The {name} model reads the driver\'s unmasked request, so its endpoint must be trust = "on_vehicle" '
                             f'(this machine or a private server). Endpoint "{role.endpoint.name}" is "{role.endpoint.trust}".')
    if require_keys:
        for role in config.roles.values(): role.endpoint.api_key()
    warnings = []
    if routing_profile is not False:
        path = Path(routing_profile or os.environ.get('JLR_ROUTING_PROFILE') or REPO / 'jlr' / 'routing_profile.json')
        tuned = json.loads(path.read_text(encoding='utf-8')).get('model')
        router = config.roles['router']
        if tuned != router.model:
            if not router.untuned_ok:
                raise ValueError(f'The routing profile is tuned for {tuned}, but the router model is {router.model}. '
                                 'Routing accuracy is unknown for another model: re-test it, then set untuned_ok = true in '
                                 '[roles.router] (JLR_ROUTER_UNTUNED_OK=1 without a models file) to accept it.')
            warnings.append(f'Router model {router.model} is not the model the routing profile was tuned for ({tuned}); re-test routing.')
    return Config(config.endpoints, config.roles, config.source, tuple(warnings))


# ------------------------------------------------------------------ current settings (re-read when they change)
_cache = {}
_lock = threading.Lock()


def config():
    """The current configuration. Re-read when the models file or a JLR_* setting changes (tests patch the environment)."""
    path = models_file(os.environ)
    stamp = path.stat().st_mtime_ns if path and path.is_file() else None
    key = (str(path), stamp, tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith('JLR_'))))
    with _lock:
        if key not in _cache:
            _cache.clear(); _cache[key] = load()
        return _cache[key]


def role(name):
    return config().roles[name]
