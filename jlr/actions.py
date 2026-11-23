"""Driver-screen actions: what the browser that sent a request should show once it has run.

Built only from receipts of steps that Validate & Execute approved and the adapters
executed, so a refused, declined or failed plan never produces an action. The browser
is the driver's own head unit (the trusted local side): it may show real names and
places, and it contacts outside services only as the receipt says (map tiles through
this server; YouTube only after the driver taps Play).
"""
from urllib.parse import urlencode


def _point(place):
    return {'name': place.get('name') or place.get('label') or '', 'address': place.get('address') or '',
            'lat': float(place['latitude']), 'lon': float(place['longitude'])}


def google_maps_url(origin, destination, stops):
    """A plain directions link (no API key); only opened if the driver taps it."""
    q = {'api': 1, 'origin': f"{origin['lat']},{origin['lon']}", 'destination': f"{destination['lat']},{destination['lon']}",
         'travelmode': 'driving'}
    if stops: q['waypoints'] = '|'.join(f"{s['lat']},{s['lon']}" for s in stops)
    return 'https://www.google.com/maps/dir/?' + urlencode(q, safe=',|')


def _route(navigation, shape):
    origin, destination = _point(navigation['origin']), _point(navigation['destination'])
    stops = [_point(s) for s in navigation.get('stops') or []]
    shape = shape or {}
    return {'type': 'show_route', 'origin': origin, 'destination': destination, 'stops': stops,
            'distance_m': navigation.get('distance_m'), 'duration_seconds': navigation.get('estimated_duration_seconds'),
            'geometry': shape.get('geometry') or [], 'steps': shape.get('steps') or [],
            'google_maps_url': google_maps_url(origin, destination, stops)}


def build(steps, receipts, shapes=()):
    shapes = list(shapes) + [None] * (len(receipts) - len(shapes))
    actions, route = [], None
    for step, receipt, shape in zip(steps, receipts, shapes):
        if receipt.get('status') != 'completed': continue
        cap = step['capability']
        if cap in {'navigation.start', 'navigation.add_stop'}:
            # The latest route wins (an added stop supersedes the first route), shown once.
            if receipt.get('navigation'): route = _route(receipt['navigation'], shape)
            else: actions.append({'type': 'info', 'title': 'Navigation', 'text': receipt.get('message', '')})
        elif cap == 'media.play': actions.append({'type': 'media', 'command': 'play', 'query': receipt.get('query', '')})
        elif cap == 'media.pause': actions.append({'type': 'media', 'command': 'pause'})
        elif cap == 'media.next_track': actions.append({'type': 'media', 'command': 'next'})
        elif cap == 'phone.call': actions.append({'type': 'call', 'state': 'active', 'contact': receipt.get('contact', '')})
        elif cap == 'phone.end_call': actions.append({'type': 'call', 'state': 'ended'})
        elif cap.startswith('profile.save_'):
            actions.append({'type': 'profile_saved', 'kind': receipt.get('saved', ''), 'profile': receipt.get('profile', ''),
                            'text': receipt.get('message', '')})
        elif cap == 'messaging.send':
            actions.append({'type': 'message_sent', 'contact': receipt.get('contact', ''), 'text': receipt.get('text', '')})
        else:
            titles = {'calendar.next_appointment': 'Next appointment', 'vehicle.get_trip_state': 'Vehicle',
                      'messages.latest': 'Latest message', 'navigation.get_destination': 'Destination'}
            actions.append({'type': 'info', 'title': titles.get(cap, 'Result'), 'text': receipt.get('message', '')})
    return ([route] if route else []) + actions
