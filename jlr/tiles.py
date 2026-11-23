"""Map tiles for the driver screen, fetched by this server instead of the driver's browser.

The tile server then sees this server (not the driver's device), and repeated views come
from a small in-memory cache, which keeps use well within the OpenStreetMap tile policy
(identifying User-Agent, no bulk downloads). A tile only reveals which map area is viewed.
"""
import os
import threading
import urllib.request
from collections import OrderedDict

from .tools import OsmTools

MAX_ZOOM, CACHE_TILES = 19, 3000
_cache, _lock = OrderedDict(), threading.Lock()


class TileError(ValueError):
    pass


def tile_url(z, x, y):
    template = os.environ.get('JLR_TILE_URL', 'https://tile.openstreetmap.org/{z}/{x}/{y}.png')
    return template.format(z=z, x=x, y=y)


def parse(path):
    """'/tiles/12/1100/1500.png' -> (12, 1100, 1500); anything else is refused."""
    parts = path.split('?', 1)[0].split('/')
    if len(parts) != 5 or parts[1] != 'tiles' or not parts[4].endswith('.png'):
        raise TileError('Not a tile path')
    try:
        z, x, y = int(parts[2]), int(parts[3]), int(parts[4][:-4])
    except ValueError:
        raise TileError('Not a tile path') from None
    if not 0 <= z <= MAX_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise TileError('Tile out of range')
    return z, x, y


def _download(url):
    request = urllib.request.Request(url, headers={'User-Agent': OsmTools.USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(1_000_001)


def get(z, x, y, fetch=None):
    key = (z, x, y)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    data = (fetch or _download)(tile_url(z, x, y))
    if not data or len(data) > 1_000_000:
        raise TileError('Tile unavailable')
    with _lock:
        _cache[key] = data
        while len(_cache) > CACHE_TILES:
            _cache.popitem(last=False)
    return data
