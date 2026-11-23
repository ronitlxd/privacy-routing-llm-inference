"""Guided installer: ./install.sh (or python jlr_setup.py --install) on the client's server.

    check the machine -> find model servers -> where do the models run -> which models -> API key (cloud only)
    -> write models.toml and .env -> first driver profile -> how is the site opened -> check -> start

Prompt rules: every question shows its default as "(default ...)" and Enter accepts it; "b" goes back one
question. The server type is detected, never asked. Technical rules (HTTPS, tunnels) are explained only when an
answer is refused.

Privacy and security rules kept here:
  - the router and the small model read unmasked requests: they run on this server or on a private server
    reached over HTTPS (or an SSH tunnel); never plain HTTP to another machine (jlr.links.check_link);
  - an API key is typed at a hidden prompt (or, if one is already set in the environment, the client is asked
    whether to use it), tested with one small request that carries no driver text, and saved only to .env
    (readable by this user only). It is never written to models.toml, printed or logged;
  - passwords are typed hidden and never stored; the profile key is derived from them (jlr.profiles);
  - the site starts in the background but is NOT enabled: it does not start by itself after a reboot
    (no `systemctl enable`, no `loginctl enable-linger`). ./install.sh --start starts it again;
  - the installer never runs sudo and never changes the firewall: it prints the command for the client.

Standard library only (cryptography is used lazily for a self-signed certificate, after install.sh installed it).
"""
import getpass
import ipaddress
import json
import os
import platform
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .links import check_link, is_local, tunnel_command

SITE_PORT, NETWORK_PORT = 8770, 8443
SERVICE = 'jlr-privacy-routing'
# The app ships no default models: the client picks from what their model server or cloud service lists.
CLOUD = {   # provider choice -> (provider, url, key variable, label)
    'anthropic': ('anthropic', 'https://api.anthropic.com/v1', 'ANTHROPIC_API_KEY', 'Anthropic Claude'),
    'openai': ('openai', 'https://api.openai.com/v1', 'OPENAI_API_KEY', 'OpenAI'),
    'other': ('openai', '', 'LARGER_API_KEY', 'OpenAI-compatible service'),
}


def tuned_router_model():
    """The model the routing rules (routing_profile.json) were written and tested with: information, not a default."""
    path = Path(os.environ.get('JLR_ROUTING_PROFILE') or Path(__file__).resolve().parent / 'routing_profile.json')
    try: return json.loads(path.read_text(encoding='utf-8')).get('model', '')
    except (OSError, ValueError): return ''


def size_label(size_gb):
    return f'{size_gb:.1f} GB' if size_gb else 'size not reported'


OK, WARN, NO, INFO = '[ ok ]', '[ !! ]', '[ -- ]', '[ info ]'


class GoBack(Exception):
    """The client typed b: return to the previous question."""


class InstallError(Exception):
    """The installer cannot continue (the message says what to do)."""


# ------------------------------------------------------------------ prompts
class Prompter:
    """Questions with a default (Enter accepts it) and b = Back. assume_defaults answers every question with its
    default (--yes); a question without a safe default (a password, an API key) then stops the install."""

    def __init__(self, read=input, secret=getpass.getpass, write=print, assume_defaults=False):
        self.read, self.secret_read, self.write, self.auto = read, secret, write, assume_defaults

    def _answer(self, text, default):
        if self.auto:
            self.write(f'{text}{default}'); return ''
        answer = self.read(text).strip()
        if answer.lower() == 'b': raise GoBack()
        return answer

    def ask(self, question, default=''):
        hint = f' (default {default})' if default not in ('', None) else ''
        return self._answer(f'  {question}{hint}, b = Back: ', default) or str(default)

    def yes(self, question, default=True):
        while True:
            answer = self._answer(f"  {question} (default {'y' if default else 'n'}), b = Back: ", 'y' if default else 'n').lower()
            if not answer: return default
            if answer[0] in 'yn': return answer[0] == 'y'
            self.write('    Type y or n (or b to go back).')

    def choose(self, question, options, default=1):
        """options: [(key, label)]; returns the chosen key."""
        self.write(f'\n {question}')
        for i, (_, label) in enumerate(options, 1): self.write(f'   {i}) {label}')
        self.write('   b) Back')
        while True:
            answer = self._answer(f'  Choose (default {default}): ', default)
            if not answer: return options[default - 1][0]
            if answer.isdigit() and 1 <= int(answer) <= len(options): return options[int(answer) - 1][0]
            self.write('    Type one of the numbers above (or b to go back).')

    def pick(self, question, items, preset=None, option_name='--small-model'):
        """Choose a model: items = [(name, note)] as the server or service lists them. A number picks from the list;
        any other text is taken as a model name. There is no default: Enter alone asks again. With --yes the name
        comes from the command line (option_name)."""
        if items:
            self.write(f'\n {question}')
            for i, (name, note) in enumerate(items, 1): self.write(f'   {i}) {name}' + (f'  ({note})' if note else ''))
            self.write('   b) Back')
        if self.auto:
            if not preset: raise InstallError(f'{question}: there is no default model; with --yes pass {option_name} NAME.')
            self.write(f'  {question}: {preset}'); return preset
        while True:
            answer = self._answer(f"  {'Number or model name' if items else question + ' (model name)'}, b = Back: ", '')
            if answer.isdigit() and items and 1 <= int(answer) <= len(items): return items[int(answer) - 1][0]
            if answer and not answer.isdigit(): return answer
            self.write('    Type one of the numbers above or a model name (or b to go back).' if items else '    Type the model name.')

    def secret(self, question):
        """Hidden input; b = Back. Never echoed, never defaulted."""
        if self.auto: raise InstallError(f'{question}: needs typing, so it cannot be answered by --yes.')
        while True:
            answer = self.secret_read(f'  {question} (hidden, b = Back): ').strip()
            if answer.lower() == 'b': raise GoBack()
            if answer: return answer
            self.write('    Nothing was typed.')


# ------------------------------------------------------------------ talking to model servers (no driver text is ever sent)
class ProbeError(Exception):
    def __init__(self, kind, detail=''):
        super().__init__(detail or kind); self.kind = kind   # unreachable | tls | auth | http


def http_get(url, headers=None, ca_file='', timeout=3):
    """GET a JSON document. Proxies are ignored (as for model calls); certificates are always verified."""
    context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ProbeError('auth' if exc.code in (401, 403) else 'http', f'HTTP {exc.code}') from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        raise ProbeError('tls' if isinstance(reason, ssl.SSLCertVerificationError) else 'unreachable', str(reason)) from None
    except (OSError, ValueError) as exc:
        raise ProbeError('unreachable', str(exc)) from None
    try: return json.loads(body or b'{}')
    except ValueError: return {}


# What runs where: (name shown to the client, port, path, provider, check of the reply)
PROBES = [
    ('Ollama', 11434, '/api/version', 'ollama', lambda d: 'version' in d),
    ('vLLM', 8000, '/v1/models', 'openai', lambda d: isinstance(d.get('data'), list)),
    ('LM Studio', 1234, '/v1/models', 'openai', lambda d: isinstance(d.get('data'), list)),
    ('llama.cpp', 8080, '/health', 'openai', lambda d: d.get('status') == 'ok'),
    ('TGI', 8080, '/info', 'openai', lambda d: 'model_id' in d),
    ('TGI', 3000, '/info', 'openai', lambda d: 'model_id' in d),
]
HINT_PROGRAMS = {'ollama': 'Ollama', 'vllm': 'vLLM', 'llama-server': 'llama.cpp', 'text-generation-launcher': 'TGI', 'lms': 'LM Studio'}


def served_models(provider, url, headers=None, ca_file='', get=None):
    get = get or http_get
    try:
        if provider == 'ollama': return [m['name'] for m in get(url + '/api/tags', headers, ca_file).get('models', [])]
        return [m.get('id') for m in get(url + '/models', headers, ca_file).get('data', []) if m.get('id')]
    except ProbeError:
        return []


def ollama_sizes(url, ca_file='', get=None):
    """{model name: size in GB} as an Ollama server reports its installed models (empty if unknown)."""
    get = get or http_get
    try: return {m['name']: m.get('size', 0) / 1e9 for m in get(url + '/api/tags', None, ca_file).get('models', [])}
    except (ProbeError, KeyError, TypeError): return {}


def is_installed(name, installed):
    return any(n == name or n.split(':latest')[0] == name or n == name + ':latest' for n in installed)


def detect_local(get=None):
    """Model servers answering on this machine's usual ports: [{'kind', 'provider', 'url', 'models'}]."""
    get = get or http_get
    found, ports = [], set()
    for kind, port, path, provider, looks_right in PROBES:
        if port in ports: continue
        base = f'http://127.0.0.1:{port}'
        try: reply = get(base + path, None, '', 2)
        except ProbeError: continue
        if not looks_right(reply): continue
        url = base if provider == 'ollama' else base + '/v1'
        found.append({'kind': kind, 'provider': provider, 'url': url, 'models': served_models(provider, url, get=get)})
        ports.add(port)
    return found


def detection_hints(run=subprocess.run, which=shutil.which):
    """Model programs installed but not answering (shown only when nothing was found)."""
    hints = [f'{label} is installed ({which(program)}) but is not answering' for program, label in HINT_PROGRAMS.items() if which(program)]
    if which('systemctl'):
        for unit in ('ollama', 'snap.ollama.listener', 'snap.ollama.ollama'):
            try: state = run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError): continue
            if state and state not in ('active', 'unknown') and 'could not be found' not in state:
                hints.append(f'service {unit} is {state}')
    return hints


def identify(address, get=None, ca_file=''):
    """Find out what kind of model server answers at an address the client typed.
    Returns {'kind', 'provider', 'url', 'models'}; raises ProbeError (unreachable | tls | auth)."""
    get = get or http_get
    url = address.strip().rstrip('/')
    if '://' not in url: url = 'https://' + url
    base = url[:-3].rstrip('/') if url.endswith('/v1') else url
    failures = []
    try:
        if 'version' in get(base + '/api/version', None, ca_file, 4):
            return {'kind': 'Ollama', 'provider': 'ollama', 'url': base, 'models': served_models('ollama', base, ca_file=ca_file, get=get)}
    except ProbeError as exc:
        failures.append(exc)
    v1 = base + '/v1'
    try:
        reply = get(v1 + '/models', None, ca_file, 4)
        if isinstance(reply.get('data'), list):
            return {'kind': 'OpenAI-compatible server', 'provider': 'openai', 'url': v1, 'models': [m.get('id') for m in reply['data'] if m.get('id')]}
    except ProbeError as exc:
        failures.append(exc)
    for kind in ('tls', 'auth'):
        if any(f.kind == kind for f in failures): raise ProbeError(kind)
    raise ProbeError('unreachable')


def test_key(provider, url, key, get=None, ca_file=''):
    """One small request with the key: the service's list of models (no driver text is sent).
    Returns (result, [model names]); result is 'ok' | 'refused' | 'unreachable'."""
    get = get or http_get
    headers = ({'x-api-key': key, 'anthropic-version': '2023-06-01'} if provider == 'anthropic' else {'Authorization': 'Bearer ' + key})
    try:
        reply = get(url + '/models', headers, ca_file, 10)
    except ProbeError as exc:
        return ('refused' if exc.kind == 'auth' else 'unreachable'), []
    return 'ok', [m.get('id') for m in reply.get('data', []) if isinstance(m, dict) and m.get('id')]


# ------------------------------------------------------------------ HTTPS certificate for network access
def reachable_ip(text):
    """An address another computer can use: not loopback (127.x, which includes Debian's 127.0.1.1), not link-local."""
    try: ip = ipaddress.ip_address(text)
    except ValueError: return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def network_addresses(run=subprocess.run):
    """(names, ips) other computers can use to reach this server: every interface (Ethernet, Wi-Fi, Tailscale ...),
    never a loopback address. Names: the host name and, with Tailscale, its MagicDNS name. Nothing is changed."""
    ips = set()
    try:   # Linux: every interface's IPv4 address
        out = run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True, text=True, timeout=5).stdout
        ips.update(f.split('/')[0] for f in (line.split()[3] for line in out.splitlines() if len(line.split()) > 3))
    except (OSError, subprocess.SubprocessError, IndexError): pass
    try: ips.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError: pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('10.255.255.255', 1)); ips.add(s.getsockname()[0])   # picks the outgoing interface; sends nothing
    except OSError: pass
    names = {socket.gethostname()}
    try:   # Tailscale's name for this machine (read only)
        out = run(['tailscale', 'status', '--self', '--json'], capture_output=True, text=True, timeout=5).stdout
        dns = (json.loads(out or '{}').get('Self') or {}).get('DNSName', '').rstrip('.')
        if dns: names.add(dns)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError): pass
    return sorted(n for n in names if n and n != 'localhost'), sorted(i for i in ips if reachable_ip(i))


def cert_covers(cert_path, names, ips, days_left=30):
    """True when an existing certificate names every address and has more than `days_left` days to run, so it can
    be kept (re-making it would bring the browser warning back and undo a 'trust this certificate')."""
    import datetime
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        alt = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except (OSError, ValueError, x509.ExtensionNotFound):
        return False
    have_names, have_ips = set(alt.get_values_for_type(x509.DNSName)), {str(i) for i in alt.get_values_for_type(x509.IPAddress)}
    fresh = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc) > datetime.timedelta(days=days_left)
    return fresh and set(names) | {'localhost'} <= have_names and set(ips) | {'127.0.0.1'} <= have_ips


def make_self_signed(cert_path, key_path, names, ips, days=825):
    """A self-signed certificate for this server's names and addresses. The key file is readable by this user only."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else 'jlr-privacy-routing')])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days)).add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).sign(key, hashes.SHA256()))
    cert_path, key_path = Path(cert_path), Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    write_private(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def write_private(path, data):
    """Create or replace a file readable by this user only, without a moment where others could read it."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'wb') as f: f.write(data)
    try: os.chmod(path, 0o600)
    except OSError: pass
    return path


# ------------------------------------------------------------------ the site as a background service (started, never enabled)
class Site:
    """Start/stop the site. With a systemd user session: a user unit that is started but not enabled.
    Without systemd (or on Windows): a background process with a pid file. Neither starts after a reboot."""

    def __init__(self, root, python=sys.executable, run=subprocess.run, popen=subprocess.Popen, environ=os.environ, which=shutil.which,
                 os_name=os.name):
        self.os_name = os_name
        self.root, self.python, self.run, self.popen, self.environ, self.which = Path(root), python, run, popen, environ, which
        config = Path(environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        self.unit_path = config / 'systemd' / 'user' / f'{SERVICE}.service'
        self.pid_file, self.log_file = self.root / 'run' / 'site.pid', self.root / 'logs' / 'site.log'

    def _systemctl(self, *args):
        return self.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=30)

    def uses_systemd(self):
        if self.os_name == 'nt' or not self.which('systemctl'): return False
        try: return self._systemctl('show-environment').returncode == 0
        except (OSError, subprocess.SubprocessError): return False

    def unit_text(self):
        template = (self.root / 'deploy' / f'{SERVICE}.service.example').read_text(encoding='utf-8')
        quote = lambda p: f'"{p}"' if ' ' in str(p) else str(p)
        text = template.replace('/path/to/jlr-privacy-routing/.venv/bin/python', quote(self.python))
        text = text.replace('/path/to/jlr-privacy-routing/server/app.py', quote(self.root / 'server' / 'app.py'))
        return text.replace('/path/to/jlr-privacy-routing', str(self.root))

    def running(self):
        if self.uses_systemd(): return self._systemctl('is-active', SERVICE).stdout.strip() == 'active'
        pid = self._pid()
        return bool(pid) and _alive(pid)

    def start(self):
        if self.uses_systemd():
            self.unit_path.parent.mkdir(parents=True, exist_ok=True)
            self.unit_path.write_text(self.unit_text(), encoding='utf-8', newline='\n')
            self._systemctl('daemon-reload')
            result = self._systemctl('start', SERVICE)      # started now; deliberately NOT enabled
            if result.returncode != 0: raise InstallError(f'systemctl --user start {SERVICE} failed: {result.stderr.strip()}')
            return
        if self.running(): return
        self.pid_file.parent.mkdir(exist_ok=True); self.log_file.parent.mkdir(exist_ok=True)
        flags = {'creationflags': 0x00000008 | 0x00000200} if os.name == 'nt' else {'start_new_session': True}
        with open(self.log_file, 'ab') as log:
            process = self.popen([self.python, str(self.root / 'server' / 'app.py')], cwd=self.root, stdout=log, stderr=log,
                                 stdin=subprocess.DEVNULL, **flags)
        self.pid_file.write_text(str(process.pid), encoding='utf-8')

    def stop(self):
        if self.uses_systemd():
            self._systemctl('stop', SERVICE); return
        pid = self._pid()
        if pid and _alive(pid): _terminate(pid)
        self.pid_file.unlink(missing_ok=True)

    def restart(self):
        self.stop(); self.start()

    def remove(self):
        """Stop the site and delete its user unit (uninstall). Returns what was removed."""
        removed = []
        if self.uses_systemd():
            self._systemctl('stop', SERVICE)
            if self.unit_path.is_file():
                self.unit_path.unlink(); removed.append(str(self.unit_path))
            self._systemctl('daemon-reload'); self._systemctl('reset-failed', SERVICE)
        if self._pid() and _alive(self._pid()): _terminate(self._pid())
        self.pid_file.unlink(missing_ok=True)
        return removed

    def lingers(self):
        """True when user services keep running after this user logs out (set by the admin; the installer never sets it)."""
        if not self.which('loginctl'): return True
        try:
            out = self.run(['loginctl', 'show-user', getpass.getuser(), '-p', 'Linger'], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError): return True
        return 'Linger=yes' in out

    def logs_hint(self):
        return f'journalctl --user -u {SERVICE} -n 30' if self.uses_systemd() else str(self.log_file)

    def _pid(self):
        try: return int(self.pid_file.read_text(encoding='utf-8').strip())
        except (OSError, ValueError): return 0


def _terminate(pid):
    """Stop the background site. On Windows a venv's python.exe is a launcher that runs the real Python as its
    child, so the whole process tree is ended; elsewhere SIGTERM lets the server stop normally."""
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
        return
    try: os.kill(pid, signal.SIGTERM)
    except OSError: pass


def _alive(pid):
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    try: os.kill(pid, 0); return True
    except OSError: return False


def wait_for_port(port, seconds=20, host='127.0.0.1'):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1): return True
        except OSError: time.sleep(0.5)
    return False


# ------------------------------------------------------------------ the guided install
class Installer:
    """The questions, in order. `setup` is the jlr_setup module (paths, hardware probe, models-file writer, status)."""

    def __init__(self, setup, prompter=None, get=None, site=None, environ=os.environ, write=print, preset='local', clock=time.monotonic,
                 choices=None):
        self.setup, self.p, self.get = setup, prompter or Prompter(), get or http_get
        self.choices = choices or {}   # --small-model / --large-model for --yes (there are no default models)
        self.site = site or Site(setup.ROOT)
        self.environ, self.write, self.preset, self.clock = environ, write, preset, clock
        self.started = clock()
        self.env = dict(setup.current_settings())   # .env values (defaults filled in)
        self.secrets = {}                           # API keys to save in .env: never in models.toml, never printed
        self.found, self.small, self.larger, self.drivers = [], {}, {}, []

    # -------------------------------------------------- flows
    def run_steps(self, steps):
        """Run (function, asks_questions) steps; b goes back to the previous step that asks something."""
        i = 0
        while i < len(steps):
            try:
                steps[i][0](); i += 1
            except GoBack:
                earlier = [j for j in range(i) if steps[j][1]]
                if earlier: i = earlier[-1]   # with nothing earlier to go back to, the same question is asked again

    def model_steps(self):
        return [(self.step_detect, False), (self.step_where, True), (self.step_small, True), (self.step_larger, True),
                (self.step_pull, False), (self.step_write_models, False)]

    def install(self):
        self.run_steps([(self.step_machine, False), *self.model_steps(), (self.step_profile, True), (self.step_access, True),
                        (self.step_save_env, False), (self.step_check, False), (self.step_start, True)])
        self.final_screen()

    def models_only(self):
        self.run_steps([*self.model_steps(), (self.step_save_env, False), (self.step_check, False)])
        self.restart_if_running()

    def access_only(self):
        self.run_steps([(self.step_access, True), (self.step_save_env, False)])
        self.restart_if_running()
        self.write(''); self.write_open_site()

    def set_key(self):
        config = self.setup.models_config()
        if config is None or isinstance(config, str):
            raise InstallError('No valid models.toml yet: run ./install.sh first.')
        keyed = {e.name: e for e in config.endpoints.values() if e.api_key_env}
        if not keyed: self.write(f'  {OK} No model server in models.toml uses an API key.'); return
        for endpoint in keyed.values():
            self.write(f'\n {endpoint.label()}')
            self.run_steps([(lambda e=endpoint: self.ask_key(e.provider, e.url, e.api_key_env[0], e.label(), e.ca_file, reuse_env=False), True),   # --set-key: a new key
                            (self.step_save_env, False)])
        self.restart_if_running()

    def restart_if_running(self):
        if self.site.running():
            self.site.restart(); self.write(f'  {OK} Site restarted with the new settings.')

    # -------------------------------------------------- 1. machine
    def step_machine(self):
        self.write('\n== Checking this machine')
        hw = self.hw = self.setup.hardware()
        os_name = platform.platform()
        try:
            release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
            os_name = release.get('PRETTY_NAME', os_name).strip('"')
        except OSError: pass
        self.write(f'  {OK} {os_name}')
        ok_py, py = self.setup.check_python()
        self.write(f'  {OK if ok_py else NO} {py}')
        if not ok_py: raise InstallError('Python 3.11 or newer is needed: sudo apt install python3.12 python3.12-venv')
        missing = self.setup.missing_packages()
        if missing: raise InstallError(f"Missing Python packages ({', '.join(missing)}): run ./install.sh, which installs them.")
        self.write(f"  {OK} {hw['cpus']} CPUs, {hw['ram_gb'] or '?'} GB RAM")
        self.write(f"  {OK} NVIDIA GPU, {hw['gpu_gb']} GB" if hw['gpu_gb'] else f'  {INFO} No NVIDIA GPU found: models on this server run on the CPU (slower).')
        free = shutil.disk_usage(self.setup.ROOT).free / 2**30
        self.write(f"  {OK if free > 10 else WARN} {free:.0f} GB free disk" + ('' if free > 10 else ' (models need about 3-8 GB)'))

    # -------------------------------------------------- 2. model servers
    def step_detect(self):
        self.write('\n== Looking for model servers on this machine')
        self.found = detect_local(self.get)
        for server in self.found:
            self.write(f"  {OK} {server['kind']} at {server['url']} ({len(server['models'])} models)")
        if not self.found:
            self.write(f'  {INFO} No model server is answering on this machine.')
            for hint in detection_hints(): self.write(f'  {INFO} {hint}')

    def step_where(self):
        default = 3 if self.preset == 'cloud' else 1
        self.where = self.p.choose('Where do the models run?', [
            ('here', 'Everything on this server'),
            ('remote', 'My small model already runs on another server'),
            ('cloud', 'This server, plus a cloud model for complex requests')], default)

    def server_models(self, exclude=()):
        """[(name, note)] for the small model's server: what it actually serves, with sizes where reported."""
        sizes = self.small.get('sizes', {})
        tuned = tuned_router_model()
        items = []
        for name in self.small['models']:
            if name in exclude: continue
            notes = [size_label(sizes[name])] if name in sizes else []
            if name == tuned: notes.append('the routing rules were tested with this model')
            items.append((name, ', '.join(notes)))
        return items

    def step_small(self):
        self.small = self.remote_server() if self.where == 'remote' else self.local_server()
        if self.small['provider'] == 'ollama':
            self.small['sizes'] = ollama_sizes(self.small['url'], self.small.get('ca_file', ''), self.get)
        if not self.small['models']: self.write(f"  {INFO} {self.small['kind']} lists no models yet: type the name of the one to use.")
        model = self.p.pick('Small model (it routes every request and answers the simple and private ones)',
                            self.server_models(), self.choices.get('small'), '--small-model')
        tuned = tuned_router_model()
        self.small['model'], self.small['untuned_ok'] = model, model != tuned
        if model != tuned:   # warn only (client's decision, 21 Nov): any model may be used
            self.write(f'  {WARN} The routing rules were written and tested with {tuned}. With {model}, how reliably')
            self.write('       private details are found (and masked before the large model) has not been measured.')
            self.write('       Test a few requests with private details before relying on it.')

    def local_server(self):
        while True:
            ollama = [s for s in self.found if s['provider'] == 'ollama']
            if ollama or self.found: return dict((ollama or self.found)[0])
            if self.p.auto: raise InstallError('No model server answers on this machine. Start Ollama (or another model server), then run again.')
            self.write('\n  No model server runs on this server yet. The simplest is Ollama:')
            self.write('    curl -fsSL https://ollama.com/install.sh | sh')
            self.write('  (vLLM, llama.cpp, LM Studio or TGI also work: start one on its usual port.)')
            self.p.ask('Press Enter to look again')
            self.found = detect_local(self.get)

    def remote_server(self):
        ca_file = ''
        while True:
            address = self.p.ask('Address of that server (e.g. https://models.example.internal)')
            if not address: self.write('    Type the address.'); continue
            url = address if '://' in address else 'https://' + address
            try: check_link(url.rstrip('/'), 'That address')
            except ValueError:
                host = urlparse(url).hostname
                self.write(f'  {NO} {url} is another machine over plain HTTP: the driver\'s private requests would cross the')
                self.write('       network unencrypted. Two safe ways:')
                self.write(f'       1) HTTPS in front of the model server, then type https://{host}')
                self.write(f'       2) An SSH tunnel kept running on this server:  {tunnel_command(url)}')
                self.write(f'          then type http://127.0.0.1:{urlparse(url).port or 11434}')
                continue
            try:
                server = identify(url, self.get, ca_file)
            except ProbeError as exc:
                if exc.kind == 'tls':
                    self.write(f'  {NO} Its HTTPS certificate is not trusted by this server (self-signed or a private authority).')
                    path = self.p.ask('Path to the certificate authority file (.pem) that signed it')
                    if path and Path(path).is_file(): ca_file = str(Path(path).resolve()); continue
                    self.write(f'  {NO} File not found: {path}'); continue
                if exc.kind == 'auth':
                    self.write(f'  {NO} That server wants an API key. Add it after the install with ./install.sh --set-key,')
                    self.write('       or check the address.'); continue
                self.write(f'  {NO} Nothing answered at {url}. Check the address and that the model server is running.'); continue
            server['ca_file'] = ca_file
            self.write(f"  {OK} {server['kind']} at {server['url']} ({len(server['models'])} models)")
            return server

    def step_larger(self):
        if self.where == 'cloud':
            self.larger = self.cloud_model(); return
        others = self.server_models(exclude=(self.small['model'],))
        options = [('same', 'Another model on the same server as the small model'), ('cloud', 'A cloud model (gets masked text only)'),
                   ('none', 'None: the small model answers everything')]
        choice = self.p.choose('Which model handles complex requests?', options, 1 if others or not self.small['models'] else 3)
        if choice == 'cloud': self.larger = self.cloud_model(); return
        if choice == 'none': self.larger = {'mode': 'none'}; return
        model = self.p.pick('Model for complex requests (it gets masked text only)', others, self.choices.get('large'), '--large-model')
        self.larger = {'mode': 'same', 'model': model, 'gpu_layers': self.gpu_placement(model)}

    def gpu_placement(self, larger):
        """gpu_layers for the large model on the small model's Ollama, worked out from the two models' sizes and this
        server's GPU memory. The small model keeps the GPU first: it is on every request's path. None = not decided
        here (another kind of server, another machine, or sizes unknown): the model server decides."""
        if self.small['provider'] != 'ollama' or self.where != 'here': return None
        sizes, hw = self.small.get('sizes', {}), getattr(self, 'hw', None) or self.setup.hardware()
        small, big = sizes.get(self.small['model']), sizes.get(larger)
        if not hw['gpu_gb'] or not small or not big: return None
        need = (small + big) * 1.2   # weights plus working memory
        if need <= hw['gpu_gb']:
            self.write(f"  {OK} Both models fit in the GPU's {hw['gpu_gb']:.0f} GB (about {need:.1f} GB): {larger} runs on the GPU.")
            return -1
        self.write(f"  {INFO} The GPU's {hw['gpu_gb']:.0f} GB cannot hold both models (about {need:.1f} GB): {larger} runs on the CPU")
        self.write('         (slower); the small model keeps the GPU. Change gpu_layers in models.toml to try otherwise.')
        return 0

    def cloud_model(self):
        choice = self.p.choose('Which cloud service?', [('anthropic', 'Anthropic Claude'), ('openai', 'OpenAI'),
                                                         ('other', 'Another OpenAI-compatible service')], 1)
        provider, url, key_env, label = CLOUD[choice]
        if not url:
            while True:
                url = self.p.ask('Its address (https://.../v1)').rstrip('/')
                try: check_link(url, 'That address')
                except ValueError: self.write(f'  {NO} Cloud services are reached over HTTPS: type an https:// address.'); continue
                if not is_local(url) and urlparse(url).scheme == 'https': break
                self.write(f'  {NO} Type the https:// address of the service.')
        listed = self.ask_key(provider, url, key_env, label)
        model = self.p.pick(f'{label} model for complex requests (it gets masked text only)', [(m, '') for m in listed],
                            self.choices.get('large'), '--large-model')
        return {'mode': 'cloud', 'provider': provider, 'url': url, 'model': model, 'key_env': key_env, 'label': label}

    def ask_key(self, provider, url, key_env, label, ca_file='', reuse_env=True):
        """Hidden prompt (or a key already set in the environment, if the client says so), tested, kept for .env.
        Returns the models the service lists for that key (empty if it could not be reached)."""
        existing = (self.environ.get(key_env) or self.setup.current_settings().get(key_env)) if reuse_env else None
        use_existing = bool(existing) and self.p.yes(f'A key is already set in {key_env} (not shown). Use it?', default=True)
        while True:
            key = existing if use_existing else self.p.secret(f'{label} API key')
            result, listed = test_key(provider, url, key, self.get, ca_file)
            if result == 'ok':
                self.write(f'  {OK} Key works ({label}, {len(listed)} models available).'); break
            if result == 'refused':
                self.write(f'  {NO} {label} refused this key. Type it again.'); use_existing = False; continue
            self.write(f'  {WARN} Could not reach {urlparse(url).hostname} to test the key.')
            if self.p.yes('Save it anyway?', default=False): break
            use_existing = False
        self.secrets[key_env] = key
        self.write(f'  {OK} It will be saved in .env (readable by you only), never in models.toml.')
        return listed

    def step_pull(self):
        """Ollama models the client named but the server does not have yet (downloaded only after a yes)."""
        if self.small['provider'] != 'ollama': return
        wanted = [self.small['model']] + ([self.larger['model']] if self.larger.get('mode') == 'same' else [])
        installed = served_models('ollama', self.small['url'], ca_file=self.small.get('ca_file', ''), get=self.get)
        todo = [m for m in dict.fromkeys(wanted) if not is_installed(m, installed)]
        if not todo: self.write(f'  {OK} Models are installed: ' + ', '.join(dict.fromkeys(wanted))); return
        if not self.p.yes(f"{', '.join(todo)} not installed on {self.small['kind']}. Download now (the size shows as it downloads)?", default=True):
            self.write(f'  {WARN} Not downloaded: the site needs them before it can answer.'); return
        for model in todo:
            try: self.setup.pull_model(self.small['url'], model)
            except Exception as exc: raise InstallError(f'Download of {model} failed: {exc}') from None

    def step_write_models(self):
        endpoints = {'vehicle': {'provider': self.small['provider'], 'url': self.small['url'], 'trust': 'on_vehicle'}}
        if self.small.get('ca_file'): endpoints['vehicle']['ca_file'] = self.small['ca_file']
        router = {'endpoint': 'vehicle', 'model': self.small['model'], 'timeout': 45}
        if self.small.get('untuned_ok'): router['untuned_ok'] = True
        roles = {'router': router, 'local': {'endpoint': 'vehicle', 'model': self.small['model'], 'timeout': 90}}
        mode = self.larger.get('mode')
        if mode == 'cloud':
            endpoints['cloud'] = {'provider': self.larger['provider'], 'url': self.larger['url'], 'trust': 'external',
                                  'api_key_env': self.larger['key_env']}
            roles['larger'] = {'endpoint': 'cloud', 'model': self.larger['model'], 'timeout': 150}
        elif mode == 'same':
            roles['larger'] = {'endpoint': 'vehicle', 'model': self.larger['model'], 'timeout': 150}
            if self.larger.get('gpu_layers') is not None: roles['larger']['gpu_layers'] = self.larger['gpu_layers']
        else:   # none: a role is still required; JLR_LOCAL_ONLY keeps every request on the small model
            roles['larger'] = {'endpoint': 'vehicle', 'model': self.small['model'], 'timeout': 150}
        if mode == 'none': self.env['JLR_LOCAL_ONLY'] = '1'
        else: self.env.pop('JLR_LOCAL_ONLY', None)
        from . import models
        candidate = Path(str(self.setup.MODELS_FILE) + '.new')
        self.setup.write_models_file(candidate, endpoints, roles)
        try: models.check(models.from_file(candidate), require_keys=False, routing_profile=None)
        except ValueError as exc:
            candidate.unlink(missing_ok=True); raise InstallError(f'models.toml not saved: {exc}') from None
        candidate.replace(self.setup.MODELS_FILE)
        self.write(f'\n  {OK} Saved models.toml')

    # -------------------------------------------------- 3. people and access
    def step_profile(self):
        os.environ['JLR_MIN_PASSWORD'] = str(self.env.get('JLR_MIN_PASSWORD', '4'))
        from .profiles import ProfileError, ProfileRegistry, min_password
        self.drivers = []
        name = self.p.ask("First driver's name (Enter to skip)", '')
        if not name: return
        while True:
            password = self.p.secret(f'Password for {name} ({min_password()}+ characters)')
            if password != self.p.secret('Type it again'): self.write(f'  {NO} The two passwords differ.'); continue
            try:
                store = ProfileRegistry(self.setup.ROOT / self.env.get('JLR_PROFILES_DIR', 'profiles')).create(name, password)
            except ProfileError as exc:
                self.write(f'  {NO} {exc}'); continue
            self.drivers = [store.profile['name']]
            self.write(f"  {OK} Driver {store.profile['name']} created. Contacts and places are added on the site (profile -> Edit my private data).")
            return

    def step_access(self):
        self.access = self.p.choose('How will people open the site?', [
            ('tunnel', 'Only through an SSH tunnel (safest)'), ('network', 'From other computers on this network')], 1)
        if self.access == 'tunnel':
            self.env.update(HOST='127.0.0.1', PORT=str(SITE_PORT))
            for key in ('JLR_TLS_CERT', 'JLR_TLS_KEY'): self.env.pop(key, None)
            return
        self.write('  Other computers reach the site over HTTPS only (never plain HTTP). Use it on a trusted internal network,')
        self.write('  never on the internet.')
        if self.p.yes('Use your own certificate (otherwise one is made for this server)?', default=False):
            while True:
                cert, key = self.p.ask('Certificate file (.pem)'), self.p.ask('Its private key file (.pem)')
                if Path(cert).is_file() and Path(key).is_file(): cert, key = str(Path(cert).resolve()), str(Path(key).resolve()); break
                self.write(f'  {NO} One of the files was not found.')
        else:
            names, ips = network_addresses()
            cert_path, key_path = self.setup.ROOT / 'certs' / 'site-cert.pem', self.setup.ROOT / 'certs' / 'site-key.pem'
            if key_path.is_file() and cert_covers(cert_path, names, ips):
                self.write(f"  {OK} Keeping the existing certificate: it still covers {', '.join(names + ips)}.")
            else:   # the server's own names and addresses, plus localhost for use on the server itself
                make_self_signed(cert_path, key_path, names + ['localhost'], ips + ['127.0.0.1'])
                self.write(f"  {OK} Made a certificate for {', '.join(names + ips)} (browsers warn once: it is self-signed).")
            cert, key = 'certs/site-cert.pem', 'certs/site-key.pem'
        self.env.update(HOST='0.0.0.0', PORT=str(NETWORK_PORT), JLR_TLS_CERT=cert, JLR_TLS_KEY=key)
        self.write(f'  {INFO} If a firewall is on, allow the port yourself:  sudo ufw allow {NETWORK_PORT}/tcp')

    def step_save_env(self):
        values = {**self.env, **self.secrets}
        self.setup.write_env(self.setup.ENV_FILE, values,
                             'JLR privacy-routing settings, written by the installer. May hold an API key: keep private, never commit.')
        self.environ.update({k: str(v) for k, v in self.secrets.items()})   # so the check below sees the key
        self.write(f'  {OK} Saved .env' + (' (readable by you only)' if self.secrets and os.name != 'nt' else ''))

    def step_check(self):
        self.write('\n== Checking everything')
        lines = self.setup.status_lines(self.env)
        for line in lines: self.write('  ' + line)
        self.check_failed = any(line.startswith(NO) for line in lines)

    def step_start(self):
        self.write('')
        self.running = False
        if not self.p.yes('Start the app now?', default=True): return
        if self.site.running(): self.site.restart()
        else: self.site.start()
        port = int(self.env['PORT'])
        self.running = wait_for_port(port)
        if self.running:
            self.write(f'  {OK} Site is running in the background (port {port})')
        else:
            self.write(f'  {NO} The site did not answer on port {port}. Its log: {self.site.logs_hint()}')
        self.write(f'  {INFO} It will not start by itself after a reboot.')
        self.write('           To start it again: ./install.sh --start')
        if not self.site.lingers():
            self.write('           It also stops when you log out of this server (ask the admin about "loginctl enable-linger").')

    # -------------------------------------------------- final screen
    def place(self, provider, url, kind):
        where = 'on this server' if is_local(url) else f'at {urlparse(url).hostname}'
        return f'{kind} {where}'

    def final_screen(self):
        took = int(self.clock() - self.started)
        self.write('\n ' + '-' * 45)
        self.write(f' Done in {took // 60} min {took % 60} s.' if took >= 60 else f' Done in {took} s.')
        self.write('\n Setup summary')
        rows = [('Router + small model', self.place(self.small['provider'], self.small['url'], self.small['kind']), self.small['model'])]
        mode = self.larger.get('mode')
        if mode == 'cloud': rows.append(('Large model', f"{self.larger['label']} (cloud, masked text)", self.larger['model']))
        elif mode == 'same': rows.append(('Large model', self.place(self.small['provider'], self.small['url'], self.small['kind']), self.larger['model']))
        else: rows.append(('Large model', 'none (small model answers everything)', ''))
        rows += [('Drivers', ', '.join(self.drivers) or 'none yet (Guest only)', ''),
                 ('Site access', 'SSH tunnel only' if self.env['HOST'] == '127.0.0.1' else f"HTTPS on this network (port {self.env['PORT']})", ''),
                 ('Config', 'models.toml, .env', '')]
        for label, where, model in rows: self.write(f'   {label:<22} {where:<26} {model}'.rstrip())
        self.write(''); self.write_open_site()
        self.write('\n Useful commands')
        for flag, does in [('--start', 'start the site'), ('--stop', 'stop the site'), ('--check', 'check everything'),
                           ('--models', 'change models'), ('--access', 'change how the site is opened'), ('--update', 'update and restart')]:
            self.write(f'   ./install.sh {flag:<10} {does}')

    def write_open_site(self):
        port = self.env['PORT']
        self.write(' Open the site')
        if self.env['HOST'] == '127.0.0.1':
            self.write('   On this server:'); self.write(f'     http://localhost:{port}/')
            self.write('\n   From another computer (SSH tunnel):')
            self.write(f'     ssh -N -L {port}:127.0.0.1:{port} {getpass.getuser()}@{socket.gethostname()}')
            self.write(f'     then open  http://localhost:{port}/')
            self.write('\n   To let other computers on your network open it directly:'); self.write('     ./install.sh --access')
        else:
            names, ips = network_addresses()
            self.write('   From other computers (any address below that their network can reach):')
            for address in ips + [n for n in names if '.' in n] or [socket.gethostname()]: self.write(f'     https://{address}:{port}/')
            self.write(f'   On this server:  https://localhost:{port}/')
            self.write('   (A self-signed certificate shows a browser warning the first time.)')


# ------------------------------------------------------------------ uninstall
def unload_model(endpoint, model, timeout=30):
    """Ask an Ollama server to drop a model from memory now (keep_alive 0). The model file stays on disk."""
    from . import transports
    key = endpoint.api_key() if endpoint.api_key_env else None
    transports.post_json(endpoint.url + '/api/generate', {'model': model, 'keep_alive': 0},
                         {'Authorization': 'Bearer ' + key} if key else {}, timeout, transports.ssl_context(endpoint.ca_file),
                         f'Model server "{endpoint.name}"')


class Uninstaller:
    """./uninstall.sh: stop the site, remove its service, release the models it was using, and (only after a yes)
    delete local files. It never deletes model files, never stops a model server or system service, never runs sudo."""

    CODE_FILES = ('.venv', 'logs', 'run', 'certs')               # rebuilt by ./install.sh
    PRIVATE_FILES = ('profiles', '.env', 'models.toml')          # driver data, API keys, model settings

    def __init__(self, setup, prompter=None, site=None, unload=unload_model, write=print):
        self.setup, self.p = setup, prompter or Prompter()
        self.site, self.unload, self.write = site or Site(setup.ROOT), unload, write

    def run(self):
        self.write('\n== Uninstall: stop the site, remove its service, release the models')
        was_running = self.site.running()
        removed = self.site.remove()
        self.write(f"  {OK} Site stopped" + ('' if was_running else ' (it was not running)'))
        for path in removed: self.write(f'  {OK} Removed the service {path}')
        self.release_models()
        self.delete_files()
        self.write('\n Left in place')
        self.write('   - model files and the model servers themselves (Ollama, vLLM ...); stop one yourself if no one else uses it,')
        self.write('     e.g.  sudo systemctl stop ollama   (models stay on disk; "ollama rm <model>" deletes one)')
        self.write(f'   - this folder ({self.setup.ROOT}); delete it to remove the code')

    def release_models(self):
        config = self.setup.models_config()
        if config is None:
            from . import models
            try: config = models.from_env(self.setup.current_settings())
            except ValueError: config = None
        if config is None or isinstance(config, str):
            self.write(f'  {INFO} No readable models file: no models to release.'); return
        seen = set()
        for role in config.roles.values():
            target = (role.endpoint.url, role.model)
            if target in seen: continue
            seen.add(target)
            where = role.endpoint.where()[0]
            if role.endpoint.provider != 'ollama':
                self.write(f'  {INFO} {role.model} at {where}: memory is managed by that server (nothing to release from here)'); continue
            try:
                self.unload(role.endpoint, role.model)
                self.write(f'  {OK} Released {role.model} from memory at {where} (the model file stays)')
            except (OSError, ValueError) as exc:
                self.write(f'  {INFO} {role.model} at {where}: not released ({type(exc).__name__}: server not reachable or already stopped)')

    def delete_files(self):
        root = Path(self.setup.ROOT).resolve()
        groups = [(self.CODE_FILES, 'Delete the Python environment, logs and certificates (.venv, logs, run, certs)?'),
                  (self.PRIVATE_FILES, 'Also delete driver profiles, .env (API keys) and models.toml? This cannot be undone.')]
        for names, question in groups:
            present = [root / n for n in names if (root / n).exists()]
            if not present: continue
            if not self.p.yes(question, default=False):
                self.write(f"  {INFO} Kept: {', '.join(p.name for p in present)}")
                if names is self.CODE_FILES: return   # private data is only offered after the code files
                continue
            for path in present:
                if path.resolve().parent != root: continue   # only the named entries in this folder, never anything else
                try:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                    self.write(f'  {OK} Deleted {path.name}')
                except OSError as exc:   # e.g. Windows: the running Python inside .venv cannot delete itself
                    self.write(f'  {WARN} Could not delete {path.name} ({exc.strerror or exc}); delete it by hand.')
