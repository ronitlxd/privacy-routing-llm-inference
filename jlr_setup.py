"""JLR privacy-routing: setup tool for a new device.

    ./install.sh                  guided install on a server (runs: python jlr_setup.py --install)
    python jlr_setup.py           interactive menu
    python jlr_setup.py --check   print the status and exit

    Flags (also through ./install.sh): --install, --start, --stop, --check, --models, --set-key, --access,
    --restart-if-running, and --yes [--preset local|cloud] --small-model NAME [--large-model NAME] to answer every
    question with its default (there are no default models, so --yes names them).
    --uninstall (./uninstall.sh): stop the site, remove its service, release the models; deletes files only after a yes.

Checks the computer, installs the Python packages, connects to Ollama and pulls the
models, writes the settings file (.env) and, when the models run somewhere else
(an OpenAI-compatible server, a hosted API), the models file (models.toml), creates
a driver profile, runs the self-test and starts the web app.
Standard library only, so it runs before anything is installed.
"""
import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'
MODELS_FILE = ROOT / 'models.toml'
sys.path.insert(0, str(ROOT))
from jlr.settings import parse_env, write_env  # noqa: E402

# No default models: which model does each job, and where, is set in models.toml by the installer
# (./install.sh, or the menu item "Models: where they run"), from what the client's servers or services list.
DEFAULTS = {
    'HOST': '127.0.0.1', 'PORT': '8770', 'JLR_HOME_PAGE': 'driver', 'JLR_PROFILES_DIR': 'profiles', 'JLR_MIN_PASSWORD': '4',
    'JLR_INTERNET_TOOLS': '1',
    'JLR_RECEIPT_LOG': 'logs/privacy_receipts.jsonl',
    'JLR_V2_ROUTER_DEADLINE': '45', 'JLR_V2_LOCAL_DEADLINE': '90', 'JLR_V2_LARGER_DEADLINE': '150', 'JLR_V2_REQUEST_DEADLINE': '180',
    'JLR_ORIGIN_ADDRESS': 'Windsor City Hall, 350 City Hall Square West, Windsor, Ontario, Canada',
    'JLR_ORIGIN_LATITUDE': '42.317102946666665', 'JLR_ORIGIN_LONGITUDE': '-83.03487285333334',
}
OK, WARN, NO = '[ ok ]', '[ !! ]', '[ -- ]'
OLDER_MODEL_SETTINGS = ('JLR_OLLAMA_URL', 'JLR_ROUTER_MODEL', 'JLR_V2_LOCAL_MODEL', 'JLR_V2_LARGER_PROVIDER', 'JLR_V2_LARGER_MODEL')


# ------------------------------------------------------------------ checks (pure where possible, for tests)
def current_settings():
    return {**DEFAULTS, **(parse_env(ENV_FILE.read_text(encoding='utf-8')) if ENV_FILE.is_file() else {})}


def check_python():
    ok = sys.version_info >= (3, 11)
    return ok, f'Python {platform.python_version()}' + ('' if ok else ' (3.11 or newer is required)')


def missing_packages():
    missing = []
    try: import cryptography  # noqa: F401
    except ImportError: missing.append('cryptography')
    try: import argon2  # noqa: F401
    except ImportError: missing.append('argon2-cffi')
    return missing


def hardware():
    """RAM (GB), CPU count and NVIDIA GPU memory (GB, 0 if none) - best effort, no extra packages."""
    ram = 0.0
    try:
        if os.name == 'nt':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong),
                            ('page_total', ctypes.c_ulonglong), ('page_free', ctypes.c_ulonglong), ('virtual_total', ctypes.c_ulonglong),
                            ('virtual_free', ctypes.c_ulonglong), ('extended', ctypes.c_ulonglong)]
            status = MemoryStatus(); status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)); ram = status.total / 2**30
        elif sys.platform == 'darwin':
            ram = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5).stdout) / 2**30
        else:
            ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2**30
    except Exception: pass
    gpu = 0.0
    if shutil.which('nvidia-smi'):
        try:
            out = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=10).stdout
            gpu = max(float(x) for x in out.split()) / 1024
        except Exception: pass
    return {'ram_gb': round(ram, 1), 'cpus': os.cpu_count() or 1, 'gpu_gb': round(gpu, 1), 'apple_silicon': sys.platform == 'darwin' and platform.machine() == 'arm64'}


def ollama_status(url, opener=urllib.request.urlopen):
    """(running, [installed model names])."""
    try:
        with opener(url.rstrip('/') + '/api/tags', timeout=5) as response:
            return True, [m['name'] for m in json.load(response).get('models', [])]
    except Exception:
        return False, []


def models_config():
    """The models file as the app will read it (None without models.toml, or the error text)."""
    from jlr import models
    if not MODELS_FILE.is_file(): return None
    try: return models.check(models.from_file(MODELS_FILE), require_keys=False)
    except ValueError as exc: return str(exc)


def ollama_needs(config):
    """{ollama url: [models]} for the Ollama endpoints of a models file."""
    needs = {}
    for role in config.roles.values():
        if role.endpoint.provider == 'ollama': needs.setdefault(role.endpoint.url, []).append(role.model)
    return {url: list(dict.fromkeys(names)) for url, names in needs.items()}


def endpoint_status(endpoint, opener=urllib.request.urlopen):
    """(reachable, detail) for one endpoint of the models file. Never sends a prompt."""
    if endpoint.provider == 'anthropic':
        has_key = any(os.environ.get(name) for name in endpoint.api_key_env)
        return has_key, 'API key set' if has_key else f'API key missing ({endpoint.api_key_env[0]})'
    if endpoint.provider == 'ollama':
        running, installed = ollama_status(endpoint.url, opener)
        return running, f'{len(installed)} models installed' if running else 'not reachable'
    headers = {}
    key = next((os.environ[name] for name in endpoint.api_key_env if os.environ.get(name)), None)
    if endpoint.api_key_env and not key: return False, f'API key missing ({endpoint.api_key_env[0]})'
    if key: headers['Authorization'] = 'Bearer ' + key
    try:
        with opener(urllib.request.Request(endpoint.url + '/models', headers=headers), timeout=5) as response:
            names = [m.get('id') for m in json.load(response).get('data', [])]
            return True, f'{len(names)} models served'
    except Exception:
        return False, 'not reachable'


def older_models_config(settings):
    """Model settings written in .env before models.toml existed (None if there are none), checked like a models file."""
    from jlr import models
    values = {**settings, **{k: v for k, v in os.environ.items() if k.startswith('JLR_')}}
    if not any(values.get(k) for k in OLDER_MODEL_SETTINGS): return None
    try: return models.check(models.from_env(values), require_keys=False)
    except ValueError as exc: return str(exc)


def models_file_lines(config, opener=urllib.request.urlopen, label='models file models.toml'):
    if isinstance(config, str): return [f'{NO} {label}: {config}']
    lines = [f'{OK} {label} (privacy rules pass)']
    seen = {}
    for name, role in config.roles.items():
        if role.endpoint not in seen: seen[role.endpoint] = endpoint_status(role.endpoint, opener)
        reachable, detail = seen[role.endpoint]
        where, _ = role.endpoint.where()
        lines.append(f"{OK if reachable else NO} {name}: {role.model} via {role.endpoint.provider} at {where} "
                     f"[{role.endpoint.trust}] - {detail}")
    for url, names in ollama_needs(config).items():
        running, installed = ollama_status(url, opener)
        for model in names:
            have = any(n == model or n.split(':latest')[0] == model for n in installed)
            if running and not have: lines.append(f'{NO} model {model} not installed in Ollama at {url}')
    lines += [f'{WARN} {w}' for w in config.warnings]
    return lines


def pull_model(url, name, progress=print, opener=urllib.request.urlopen):
    """Pull a model through the Ollama API, reporting progress; True when Ollama reports success."""
    request = urllib.request.Request(url.rstrip('/') + '/api/pull', data=json.dumps({'model': name, 'stream': True}).encode(),
                                     headers={'Content-Type': 'application/json'})
    last = None
    with opener(request, timeout=3600) as response:
        for line in response:
            if not line.strip(): continue
            event = json.loads(line)
            if event.get('error'): raise RuntimeError(event['error'])
            status = event.get('status', '')
            if event.get('total'):
                status = f"{status} {100 * event.get('completed', 0) // event['total']}%"
            if status != last: progress(f'    {name}: {status}'); last = status
            if event.get('status') == 'success': return True
    return False


def status_lines(settings=None, opener=urllib.request.urlopen):
    settings = settings or current_settings()
    ok_py, py = check_python(); missing = missing_packages()
    lines = [f"{OK if ok_py else NO} {py}",
             f"{OK if not missing else NO} Python packages" + (f" (missing: {', '.join(missing)})" if missing else '')]
    config = models_config()
    if config is not None:   # models.toml decides where every model runs
        lines += models_file_lines(config, opener)
    elif (older := older_models_config(settings)) is not None:
        lines += models_file_lines(older, opener, '.env model settings (older setup; models.toml replaces them)')
    else:
        lines.append(f'{NO} models: not set up yet (run ./install.sh, or the menu item "Models: where they run")')
    return lines + settings_lines(settings)


def settings_lines(settings):
    lines = []
    lines.append(f"{OK if ENV_FILE.is_file() else WARN} settings file .env" + ('' if ENV_FILE.is_file() else ' (not written yet; defaults apply)'))
    profiles = ROOT / settings['JLR_PROFILES_DIR'] / 'index.json'
    count = len(json.loads(profiles.read_text(encoding='utf-8'))) if profiles.is_file() else 0
    from jlr.demo_profile import NAME, enabled
    demo = enabled(settings) and enabled()
    note = f'demo driver {NAME} is created when the site starts' if demo and not count else 'Guest is always available'
    lines.append(f"{OK if count or demo else WARN} driver profiles: {count} ({note})")
    return lines


# ------------------------------------------------------------------ interactive steps
def ask(question, default=''):
    answer = input(f'  {question}' + (f' [{default}]' if default else '') + ': ').strip()
    return answer or default


def confirm(question, default=True):
    answer = input(f"  {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith('y')


def step_check():
    print('\n== This computer')
    hw = hardware()
    print(f"  {platform.system()} {platform.release()} | {hw['cpus']} CPUs | {hw['ram_gb'] or '?'} GB RAM | "
          + (f"NVIDIA GPU {hw['gpu_gb']} GB" if hw['gpu_gb'] else 'Apple silicon' if hw['apple_silicon'] else 'no NVIDIA GPU found'))
    for line in status_lines(): print('  ' + line)
    free = shutil.disk_usage(ROOT).free / 2**30
    print(f"  {OK if free > 10 else WARN} {free:.0f} GB free disk (models need about 3-8 GB)")


def step_packages():
    print('\n== Python packages')
    missing = missing_packages()
    if not missing: print(f'  {OK} everything installed'); return True
    print(f"  Missing: {', '.join(missing)} (from requirements.txt)")
    if os.environ.get('VIRTUAL_ENV') is None: print(f'  {WARN} Not in a virtual environment; packages go to this Python ({sys.executable}).')
    if not confirm('Install them now with pip?'): return False
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')])
    return result.returncode == 0 and not missing_packages()


def step_settings():
    print('\n== Settings (.env)')
    values = current_settings(); hw = hardware()
    print('  (Models are set in models.toml: menu item "Models: where they run".)')
    values['PORT'] = ask('Web app port', values['PORT'])
    host = ask('Listen address (127.0.0.1 = this device only)', values['HOST'])
    if host not in ('127.0.0.1', 'localhost', '::1') and not values.get('JLR_TLS_CERT'):
        print(f'  {WARN} Other computers reach the site over HTTPS only. Use "Site access" (./install.sh --access) to set it up;')
        print('       keeping 127.0.0.1 (this device only, or an SSH tunnel).')
        host = '127.0.0.1'
    values['HOST'] = host
    values['JLR_MIN_PASSWORD'] = ask('Minimum profile password length', values['JLR_MIN_PASSWORD'])
    values['JLR_INTERNET_TOOLS'] = '1' if confirm('Use OpenStreetMap for places and routes (needs internet)?', values['JLR_INTERNET_TOOLS'] == '1') else '0'
    if confirm('Change the starting location (default: Windsor City Hall)?', default=False):
        values['JLR_ORIGIN_ADDRESS'] = ask('Address', values['JLR_ORIGIN_ADDRESS'])
        values['JLR_ORIGIN_LATITUDE'] = ask('Latitude', values['JLR_ORIGIN_LATITUDE'])
        values['JLR_ORIGIN_LONGITUDE'] = ask('Longitude', values['JLR_ORIGIN_LONGITUDE'])
    if not hw['gpu_gb'] and not hw['apple_silicon']:
        values.update(JLR_V2_LARGER_DEADLINE='240', JLR_V2_REQUEST_DEADLINE='270')  # CPU-only: slower larger model
    write_env(ENV_FILE, values, 'JLR privacy-routing settings, written by jlr_setup.py. May hold an API key: keep private, never commit.')
    print(f'  {OK} saved {ENV_FILE}')
    return values


def toml_value(value):
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, (int, float)): return str(value)
    return json.dumps(str(value))


def write_models_file(path, endpoints, roles):
    lines = ['# Where each model runs (written by jlr_setup.py). See models.example.toml for every option.',
             '# Router and local see UNMASKED requests: their endpoint must be trust = "on_vehicle".', '']
    for name, values in endpoints.items():
        lines.append(f'[endpoints.{name}]'); lines += [f'{k} = {toml_value(v)}' for k, v in values.items()]; lines.append('')
    for name, values in roles.items():
        lines.append(f'[roles.{name}]'); lines += [f'{k} = {toml_value(v)}' for k, v in values.items()]; lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    return path


def step_profile():
    print('\n== Driver profile')
    os.environ.update({k: v for k, v in current_settings().items() if k == 'JLR_MIN_PASSWORD'})
    try:
        from jlr.profiles import ProfileError, ProfileRegistry
    except ImportError:
        print(f'  {NO} Install the Python packages first.'); return False
    name = ask('Profile name (blank to skip)')
    if not name: return False
    password = getpass.getpass('  Password: ')
    if password != getpass.getpass('  Repeat password: '): print(f'  {NO} Passwords do not match.'); return False
    try:
        store = ProfileRegistry(ROOT / current_settings()['JLR_PROFILES_DIR']).create(name, password)
    except ProfileError as exc: print(f'  {NO} {exc}'); return False
    print(f"  {OK} created {store.profile['name']}. Add contacts and places in the web app: profile button -> Edit my private data.")
    return True


def step_selftest():
    print('\n== Self-test (no models needed)')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT, env=env, capture_output=True, text=True)
    summary = [line for line in (result.stdout + result.stderr).splitlines() if line.startswith(('Ran ', 'OK', 'FAILED'))]
    print('  ' + ' - '.join(summary))
    print(f"  {OK if result.returncode == 0 else NO} self-test {'passed' if result.returncode == 0 else 'failed'}")
    return result.returncode == 0


def step_start():
    settings = current_settings()
    print(f"\n== Starting the web app on http://{settings['HOST']}:{settings['PORT']}/  (Ctrl+C to stop)")
    try: subprocess.run([sys.executable, str(ROOT / 'server' / 'app.py')], cwd=ROOT)
    except KeyboardInterrupt: print('\n  stopped')


def installer(argv=(), **kw):
    from jlr.installer import Installer, Prompter
    auto = '--yes' in argv
    preset = argv[argv.index('--preset') + 1] if '--preset' in argv and argv.index('--preset') + 1 < len(argv) else 'local'
    if preset not in ('local', 'cloud'): raise SystemExit(f'Unknown preset {preset!r}: use local or cloud.')
    choices = {key: argv[argv.index(flag) + 1] for key, flag in (('small', '--small-model'), ('large', '--large-model'))
               if flag in argv and argv.index(flag) + 1 < len(argv)}
    return Installer(sys.modules[__name__], Prompter(assume_defaults=auto), preset=preset, choices=choices, **kw)


def guided_install(): installer().install()
def guided_models(): installer().models_only()
def guided_access(): installer().access_only()


MENU = [('Guided install (recommended; the same as ./install.sh)', guided_install), ('Check this computer', step_check),
        ('Install Python packages', step_packages), ('Models: where they run (models.toml)', guided_models),
        ('Settings (.env)', step_settings), ('Site access (SSH tunnel or HTTPS on the network)', guided_access),
        ('Create a driver profile', step_profile), ('Run self-test', step_selftest), ('Start the web app here (Ctrl+C stops it)', step_start)]
FLAGS = ('--install', '--start', '--stop', '--models', '--set-key', '--access', '--restart-if-running', '--uninstall')


def run_flag(argv):
    """The ./install.sh flags. Returns the exit code."""
    from jlr.installer import InstallError, Site, wait_for_port
    site = Site(ROOT)
    try:
        if '--start' in argv:
            port = int(current_settings()['PORT'])
            if site.running(): print(f'  {OK} The site is already running (port {port}).'); return 0
            site.start()
            if not wait_for_port(port): print(f'  {NO} The site did not answer on port {port}. Its log: {site.logs_hint()}'); return 1
            print(f'  {OK} Site is running in the background (port {port}). It will not start by itself after a reboot.'); return 0
        if '--stop' in argv:
            site.stop(); print(f'  {OK} Site stopped.'); return 0
        if '--uninstall' in argv:
            from jlr.installer import Prompter, Uninstaller
            Uninstaller(sys.modules[__name__], Prompter(assume_defaults='--yes' in argv), site).run(); return 0
        if '--restart-if-running' in argv:
            if site.running(): site.restart(); print(f'  {OK} Site restarted.')
            return 0
        tool = installer(argv)
        if '--install' in argv: tool.install()
        elif '--models' in argv: tool.models_only()
        elif '--set-key' in argv: tool.set_key()
        elif '--access' in argv: tool.access_only()
        return 0
    except InstallError as exc:
        print(f'\n  {NO} {exc}'); return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):   # a console that cannot show a character prints '?' instead of stopping
        try: stream.reconfigure(errors='replace')
        except (AttributeError, ValueError): pass
    if '--check' in argv:
        lines = status_lines()
        print('\n'.join(lines)); return 0 if not any(line.startswith(NO) for line in lines) else 1
    if any(flag in argv for flag in FLAGS): return run_flag(argv)
    while True:
        print('\n==========================================')
        print(' JLR privacy-routing - setup')
        print('==========================================')
        for line in status_lines(): print(' ' + line)
        print()
        for i, (label, _) in enumerate(MENU, 1): print(f'  {i}  {label}')
        print('  0  Exit')
        choice = input('\nChoose: ').strip()
        if choice == '0': return 0
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            from jlr.installer import GoBack, InstallError
            try: MENU[int(choice) - 1][1]()
            except (KeyboardInterrupt, EOFError, GoBack): print('\n  cancelled')
            except InstallError as exc: print(f'\n  {NO} {exc}')


if __name__ == '__main__':
    try: sys.exit(main())
    except (KeyboardInterrupt, EOFError): print(); sys.exit(130)
