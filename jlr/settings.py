"""Settings file (.env) support: one file holds every setting, for the server and the setup tool.

Format: KEY=VALUE per line; blank lines and lines starting with # are ignored; an
unquoted value ends at " #" (inline comment); quotes around a value are removed.
Real environment variables always win, so systemd or shell settings override the file.
"""
import os
import re
from pathlib import Path

LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')


def parse_env(text):
    values = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'): continue
        match = LINE.match(raw)
        if not match: continue
        key, value = match.groups()
        if value[:1] in ('"', "'") and value.endswith(value[:1]) and len(value) >= 2: value = value[1:-1]
        else: value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        values[key] = value
    return values


def load_env(path):
    """Apply a .env file to os.environ without overriding variables that are already set."""
    path = Path(path)
    if not path.is_file(): return {}
    values = parse_env(path.read_text(encoding='utf-8'))
    for key, value in values.items(): os.environ.setdefault(key, value)
    return values


def write_env(path, values, header=''):
    """Write settings (private to the user where the OS supports it): the file may hold an API key."""
    path = Path(path)
    lines = [f'# {line}' for line in header.splitlines()] + [''] if header else []
    for key, value in values.items():
        value = str(value)
        lines.append(f'{key}="{value}"' if re.search(r'[\s#"\']', value) else f'{key}={value}')
    # Created readable by this user only (never a moment where others could read an API key); tightened if it existed.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'wb') as f: f.write(('\n'.join(lines) + '\n').encode('utf-8'))
    try: os.chmod(path, 0o600)
    except OSError: pass
    return path
