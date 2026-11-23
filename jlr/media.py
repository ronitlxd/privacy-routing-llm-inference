"""YouTube lookup for the driver screen, run only after the driver taps "Play on YouTube".

The search text (for example a saved music preference) is shown to the driver before
anything is sent. Personal or restricted text (phone numbers, credentials, health
details) is refused here, whatever the driver taps. The page then plays the video
through the privacy-enhanced youtube-nocookie embed.
"""
import re
import urllib.parse
import urllib.request

from .privacy_policy import assess

MAX_QUERY = 100
VIDEO_ID = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


class MediaError(ValueError):
    pass


def check_query(query):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_QUERY:
        raise MediaError(f'Search text must be 1-{MAX_QUERY} characters')
    if assess(query)['tier'] in ('P2', 'P3'):
        raise MediaError('This search text contains personal information, so it is not sent to YouTube.')
    return query.strip()


def search_url(query):
    return 'https://www.youtube.com/results?' + urllib.parse.urlencode({'search_query': query})


def _download(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (JLR-V2-Research-Prototype)', 'Accept-Language': 'en'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(3_000_000).decode('utf-8', 'replace')


MAX_CANDIDATES = 10


def lookup(query, fetch=None):
    """Videos for the search text, best first, or only a search link if YouTube cannot be read.

    Several candidates are returned because many official music videos refuse to play
    on other sites (embed error 150); the page tries the next one when that happens.
    """
    query = check_query(query)
    url = search_url(query)
    try:
        ids = list(dict.fromkeys(VIDEO_ID.findall((fetch or _download)(url))))[:MAX_CANDIDATES]
    except Exception:
        ids = []
    return {'query': query, 'video_ids': ids, 'video_id': ids[0] if ids else None, 'search_url': url,
            'service': 'YouTube search', 'received': 'the music search text you approved'}
