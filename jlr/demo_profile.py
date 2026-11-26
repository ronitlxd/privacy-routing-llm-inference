"""Demo driver Alex, with sample contacts, places and preferences.

When the site starts it creates the profile Alex (password 1234) with the data below, unless a profile named Alex
already exists, and every new session starts signed in as Alex. Switching to Guest or another driver still works.

  JLR_DEMO_PROFILE=0         turn the demo driver off: sessions start as Guest
  JLR_DEMO_PASSWORD=...      another password for a newly created Alex (default 1234)

The demo password is published, so this profile holds sample data only: synthetic names and example.invalid
e-mails, and public Windsor landmarks as places. Never save real private data in it.
"""
from __future__ import annotations
import os
import sys
from .profiles import ProfileError

NAME = 'Alex'
DEFAULT_PASSWORD = '1234'
DATA = [
    ('place', {'alias': 'home', 'label': 'Home', 'address': '350 City Hall Square W, Windsor', 'latitude': 42.3171, 'longitude': -83.0349}),
    ('place', {'alias': 'work', 'label': 'Work', 'address': '401 Sunset Ave, Windsor', 'latitude': 42.3048, 'longitude': -83.0660}),
    ('place', {'alias': 'gym', 'label': 'Gym', 'address': '8787 McHugh St, Windsor', 'latitude': 42.3180, 'longitude': -82.9306}),
    ('contact', {'alias': 'my wife', 'label': 'Taylor Morgan', 'email': 'taylor@example.invalid'}),
    ('contact', {'alias': 'alex', 'label': 'Alex Chen', 'email': 'alex@example.invalid'}),
    ('contact', {'alias': 'my brother', 'label': 'Sam Chen', 'email': 'sam@example.invalid'}),
    ('contact', {'alias': 'mom', 'label': 'Jordan Lee', 'email': 'jordan@example.invalid'}),
    ('preference', {'key': 'usual_playlist', 'value': 'Jazz'}),
    ('preference', {'key': 'usual_drink', 'value': 'Oat latte'}),
]


def enabled(environ=os.environ):
    return environ.get('JLR_DEMO_PROFILE', '1').strip().casefold() not in {'0', 'false', 'no', 'off'}


def open_demo(registry, environ=os.environ):
    '''Alex's store, created with the demo data the first time; None when turned off or when it cannot be opened.'''
    if not enabled(environ): return None
    password = environ.get('JLR_DEMO_PASSWORD') or DEFAULT_PASSWORD
    try:
        for profile in registry.list():
            if profile['protected'] and profile['name'].casefold() == NAME.casefold():
                return registry.open(profile['id'], password)
        store = registry.create(NAME, password)
        for kind, record in DATA: store.put(kind, dict(record))
        return store
    except ProfileError as exc:
        print(f'JLR V2 warning: demo driver {NAME} not opened ({exc}); sessions start as Guest.', file=sys.stderr)
        return None
