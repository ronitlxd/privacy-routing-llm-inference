"""Fastest visiting order for a multi-stop navigation request, computed on the vehicle.

"Navigate me home then to work then to gym, compare the routes and give me the fastest":
the plan visits a set of places; when the driver asks for the fastest (or best) route,
every visiting order from the starting point is compared here with driving times from
OSRM (coordinates only, as for any route), and the navigation steps are rebuilt in the
fastest order. The comparison is part of the plan, so the confirmation card shows it and
the driver approves the order. Places without stored coordinates (public names looked up
at execution) leave the order as proposed.
"""
import re
from itertools import permutations

FASTEST = re.compile(r'\b(?:fastest|quickest|shortest|best (?:order|route|way)|optimi[sz]e|most efficient)\b|\bcompare\b.*\broutes?\b', re.I)
NAVIGATION = ('navigation.start', 'navigation.add_stop')
MAX_PLACES = 5


def visits(steps):
    """Places in visiting order: the stops as added, then the destination."""
    destination = [s['arguments']['destination'] for s in steps if s['capability'] == 'navigation.start']
    stops = [s['arguments']['stop'] for s in steps if s['capability'] == 'navigation.add_stop']
    return stops + destination


def stated_order(places, prompt, entities):
    """The order in which the driver mentioned the places (by any of their names)."""
    text = prompt.casefold()
    def first(label):
        names = [label, *entities.get(label, {}).get('aliases', [])]
        found = [m.start() for name in names if name for m in [re.search(r'(?<!\w)' + re.escape(name.casefold()) + r'(?!\w)', text)] if m]
        return min(found) if found else len(text)
    return sorted(places, key=first)


def total_minutes(order, index, durations):
    legs = [0] + [index[p] for p in order]
    seconds = sum(durations[a][b] for a, b in zip(legs, legs[1:]))
    return round(seconds / 60)


def reorder(steps, prompt, coordinates, entities, origin, travel_matrix):
    """(new steps, comparison), or None when the order is left as proposed."""
    if not FASTEST.search(prompt): return None
    nav = [i for i, s in enumerate(steps) if s['capability'] in NAVIGATION]
    starts = [i for i in nav if steps[i]['capability'] == 'navigation.start']
    places = visits([steps[i] for i in nav])
    if len(starts) != 1 or len(places) < 2 or len(set(places)) != len(places) or len(places) > MAX_PLACES: return None
    if any(p not in coordinates for p in places): return None
    points = [origin] + [{'latitude': coordinates[p][0], 'longitude': coordinates[p][1]} for p in places]
    durations = travel_matrix(points)['durations']
    if any(d is None for row in durations for d in row): return None
    index = {p: i + 1 for i, p in enumerate(places)}
    yours = stated_order(places, prompt, entities)
    # On a tie the driver's own order wins: it is only changed when another order is faster.
    options = sorted(((total_minutes(list(order), index, durations), list(order)) for order in permutations(places)),
                     key=lambda x: (x[0], x[1] != yours))
    fastest_minutes, fastest = options[0]
    new_nav = [{'capability': 'navigation.start', 'arguments': {'destination': fastest[-1]}}] + \
              [{'capability': 'navigation.add_stop', 'arguments': {'stop': p}} for p in fastest[:-1]]
    rebuilt = [s for i, s in enumerate(steps) if i < nav[0] and i not in nav] + new_nav + \
              [s for i, s in enumerate(steps) if i > nav[0] and i not in nav]
    comparison = {'fastest': {'order': fastest, 'minutes': fastest_minutes},
                  'your_order': {'order': yours, 'minutes': total_minutes(yours, index, durations)},
                  'orders_compared': len(options), 'from': 'your starting point',
                  'note': 'Driving times from OSRM using coordinates only; time at each stop not included.'}
    return rebuilt, comparison


def summary(comparison):
    fastest, yours = comparison['fastest'], comparison['your_order']
    text = f"Fastest order: {' → '.join(fastest['order'])}, about {fastest['minutes']} min"
    if yours['order'] != fastest['order']:
        text += f" (your order {' → '.join(yours['order'])}: about {yours['minutes']} min)"
    return text + f"; {comparison['orders_compared']} orders compared."
