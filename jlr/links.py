"""Model link security: unmasked text never crosses a network unencrypted.

The router and the local model receive the driver's unmasked prompt, so Ollama
must be on this machine (plain HTTP to a loopback address) or reached over HTTPS
with a verified certificate. A hosted larger model only receives masked text, but
its address follows the same rule. A remote plain-HTTP address is refused: the
server does not start, and every call checks again (fail closed).

Remote Ollama, two supported ways:
  SSH tunnel:  ssh -N -L 11434:127.0.0.1:11434 <user>@<host>   then JLR_OLLAMA_URL=http://127.0.0.1:11434
  HTTPS:       a TLS reverse proxy in front of Ollama            then JLR_OLLAMA_URL=https://<host>
               (JLR_OLLAMA_CA_FILE for a private or self-signed certificate authority)
"""
import ipaddress
import os
import ssl
from urllib.parse import urlparse


def is_local(url):
    host = (urlparse(url).hostname or '').strip('[]')
    if host == 'localhost': return True
    try: return ipaddress.ip_address(host).is_loopback
    except ValueError: return False


def tunnel_command(url):
    parsed = urlparse(url); port = parsed.port or 11434
    return f'ssh -N -L {port}:127.0.0.1:{port} <user>@{parsed.hostname}'


def check_link(url, setting):
    """Raise ValueError unless the address is loopback HTTP(S) or remote HTTPS."""
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError(f'{setting} must be an http:// or https:// address, not {url!r}.')
    if parsed.scheme == 'http' and not is_local(url):
        raise ValueError(f'{setting}={url} is another machine over plain HTTP; prompts would cross the network unencrypted. '
                         f'Use https://{parsed.hostname} behind a TLS proxy, or an SSH tunnel: {tunnel_command(url)} '
                         f'and set {setting}=http://127.0.0.1:{parsed.port or 11434}{parsed.path}.')
    return url


def check_model_links(environ=None):
    """Check every model address (models.toml, or the .env settings): loopback or HTTPS, and the trust rules."""
    from .models import check, load
    check(load(environ), require_keys=False)


def ollama_ssl_context():
    """Certificate verification is always on; a private CA can be added with JLR_OLLAMA_CA_FILE."""
    cafile = os.environ.get('JLR_OLLAMA_CA_FILE')
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
