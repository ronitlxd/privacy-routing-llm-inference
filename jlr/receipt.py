"""Privacy receipt: what happened to the driver's data in one request.

Built from the finished request itself (route, tier, masking counts, trace events),
so it reports what actually happened, not what was intended: where each model ran
and whether that link is encrypted, what reached the larger model, which outside
services were contacted and what kind of data each received. It never contains the
prompt, the answer or any private value, so it is shown in the Block details panel
and written to the local receipt log (JLR_RECEIPT_LOG) as is.
"""
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .links import is_local

# trace event -> outside services it contacts and what they receive
SERVICES = {
    'map_find_places': [('OpenStreetMap search (Nominatim, Photon fallback)', 'public search text'), ('OSRM routing', 'coordinates only')],
    'map_resolve_address': [('OpenStreetMap search (Nominatim)', 'the typed public address')],
    'map_route': [('OSRM routing', 'coordinates only')],
    'task_facts': [('OSRM routing', 'coordinates only')],
}
# driver-screen action -> what showing it contacts (the browser is the driver's own screen)
SCREEN_SERVICES = {
    'show_route': ('OpenStreetMap map tiles (through this server)', 'the map area on screen; no route, names or addresses'),
    'media': ('YouTube (only if you tap Play on YouTube)', 'the music search text, shown to you first'),
}
TOOL_EVENTS = {'map.find_places': 'map_find_places', 'map.resolve_address': 'map_resolve_address', 'map.route': 'map_route'}
_log_lock = threading.Lock()


def where(url):
    """(place, encrypted link?) for a model address."""
    if is_local(url): return 'this server', True
    parsed = urlparse(url)
    return parsed.hostname, parsed.scheme == 'https'


def model_place(role):
    """Where the model of a role (router | local | larger) runs, from jlr.models (models.toml or .env)."""
    from .models import config
    return config().roles[role].endpoint.where()


def build(result):
    events = {e['id'] for e in result.get('trace', [])}
    for e in result.get('trace', []):
        if e['id'] == 'tool_gateway' and isinstance(e.get('input'), dict) and e['input'].get('tool') in TOOL_EVENTS:
            events.add(TOOL_EVENTS[e['input']['tool']])
    worker = result.get('worker') or {}
    routing = result.get('privacy_routing') or {}
    larger = result.get('model_invoked') and result.get('next_route') == 'privacy_layer'
    if result.get('model_invoked'):
        place, encrypted = model_place('larger' if larger else 'local')
        answered = {'by': 'larger model' if larger else 'local model', 'model': worker.get('model'), 'where': place, 'encrypted_link': encrypted}
    else:
        answered = {'by': 'system rules (no model)' if result.get('status') == 'unsupported' else 'no model (stopped before one ran)',
                    'model': None, 'where': 'this server', 'encrypted_link': True}
    router_place = model_place('router') if result.get('routing_invoked') else None

    sent = None
    privacy = result.get('privacy')
    if larger and privacy:
        facts = result.get('task_facts') or {}
        sent = {'what': 'masked request', 'placeholders': privacy.get('token_count', 0),
                'typed_words_masked': len(privacy.get('masked_words', [])), 'saved_values_hidden': privacy.get('hidden_saved_values', 0),
                'roles_described': len(privacy.get('placeholders') or {}),
                'driving_times_shared': len(facts.get('driving_times', [])),
                'trip_options_shared': len((facts.get('trip_options') or {}).get('options', [])),
                'private_values_sent': 0}  # guaranteed: the leak check fails the request if any private value remains
    services = {}
    for event, entries in SERVICES.items():
        if event in events:
            for service, data in entries: services.setdefault(service, data)
    for action in result.get('actions') or []:
        if action.get('type') in SCREEN_SERVICES and (action['type'] != 'media' or action.get('command') == 'play'):
            service, data = SCREEN_SERVICES[action['type']]
            services.setdefault(service, data)
    warnings = [f"{name} at {place} is reached over an unencrypted link."
                for name, (place, encrypted) in [('The model', (answered['where'], answered['encrypted_link'])), ('The router', router_place or ('', True))]
                if not encrypted]
    # What was written to the driver's profile: kinds only, never the values.
    saved = sorted({a.get('kind') for a in result.get('actions') or [] if a.get('type') == 'profile_saved' and a.get('kind')})
    return {'answered_by': answered, 'saved_to_profile': {'kinds': saved, 'where': 'this vehicle, encrypted in your profile'} if saved else None, 'router': {'where': router_place[0], 'encrypted_link': router_place[1]} if router_place else None,
            'tier': routing.get('tier'), 'tier_name': routing.get('tier_name'), 'decision': (routing.get('decision') or {}).get('reason_code'),
            'kept_local_because': result.get('routing_notice'), 'sent_to_larger_model': sent,
            'outside_services': [{'service': s, 'received': d} for s, d in services.items()], 'warnings': warnings}


def log(result, receipt):
    """Append the receipt (no content) to JLR_RECEIPT_LOG when set; the file is private to the service user."""
    path = os.environ.get('JLR_RECEIPT_LOG')
    if not path: return
    entry = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'request_id': result.get('request_id'), 'status': result.get('status'), **receipt}
    path = Path(path)
    with _log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as out: out.write(json.dumps(entry, ensure_ascii=False) + '\n')
