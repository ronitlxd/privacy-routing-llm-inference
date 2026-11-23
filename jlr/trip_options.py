"""Trip options computed on the vehicle, so the model explains a correct result instead of doing the arithmetic.

From a travel question and the driving minutes between the places it mentions,
work out the start, the end and the places to visit, then list every visiting
order with its total, the direct route when there is a start and an end, and
whether each option fits a stated time budget. Rules are about wording shape,
never about particular places:

  start   a place mentioned right after "from", "starting from", "leave (from)"; "after X" only when
          no explicit start is named
          (exactly one; otherwise the trip starts at START, or at the end for a round trip)
  end     a place mentioned right after "back (to)", "return to", "end at", "be (at)",
          or as Y in "from X to Y"
  visits  every other mentioned place
  budget  "have / within / in / under / only N minutes"; other "N-minute" phrases are
          time spent at the stops

Only the placeholder names (or labels on the local route) and minutes are used.
"""
from __future__ import annotations
import re
from itertools import permutations

FROM_CUE = re.compile(r"\b(?:from|starting at|start at|leave|leaving|leave from|leaving from)\s+(?:the\s+|my\s+)?$", re.I)
# "after work" can mean the start ("After work, compare…") or a stop ("before work or after work"),
# so it counts only when no explicit start is named.
AFTER_CUE = re.compile(r"\bafter\s+(?:the\s+|my\s+)?$", re.I)
END_CUE = re.compile(r"\b(?:back|back to|return to|returning to|end at|end up at|finish at|be|be at|be back at|get back to)\s+(?:the\s+|my\s+)?$"
                     r"|\bfrom\s+(?:the\s+|my\s+)?[\w'-]+(?:\s+[\w'-]+)?\s+to\s+(?:the\s+|my\s+)?$", re.I)
ORDER_CUE = re.compile(r"\b(?:order|first|before|then|stop|stops|stopping|on the way|route|visit|visiting|fit|plan|sequence|"
                       r"directly|straight)\b", re.I)
BUDGET = re.compile(r"\b(?:have|within|in|under|only|got)\s+(?:only\s+|about\s+)?(\d{1,3})\s*-?\s*min(?:ute)?s?\b", re.I)
DURATION = re.compile(r"\b(\d{1,3})\s*-?\s*min(?:ute)?s?\b", re.I)
MAX_VISITS = 4


def mentions(prompt, names):
    """Every (start, end) span where one of the names occurs as whole words."""
    spans = []
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        spans += [m.span() for m in re.finditer(r'(?<!\w)' + re.escape(name) + r"(?:'s)?(?!\w)", prompt, re.I)]
    return sorted(spans)


def trip_options(prompt, places, minutes):
    """places: {display_name: [words that mention it]}; minutes: {(a, b): driving minutes}, including 'START'."""
    if not ORDER_CUE.search(prompt): return None
    spans = {key: mentions(prompt, names) for key, names in places.items()}
    spans = {key: s for key, s in spans.items() if s}
    starts = [k for k, s in spans.items() if any(FROM_CUE.search(prompt[:a]) for a, _ in s)] or \
             [k for k, s in spans.items() if any(AFTER_CUE.search(prompt[:a]) for a, _ in s)]
    ends = [k for k, s in spans.items() if any(END_CUE.search(prompt[:a]) for a, _ in s) and k not in starts]
    start = starts[0] if len(starts) == 1 else None
    end = ends[0] if len(ends) == 1 else None
    if start is None: start = end or 'START'          # "be back home" with no start: a round trip from home
    visits = [k for k in spans if k not in (start, end)][:MAX_VISITS]
    one_way = end is not None and end != start
    if not visits or (len(visits) < 2 and not one_way): return None

    def total(route):
        legs = [minutes.get((a, b)) for a, b in zip(route, route[1:])]
        return None if any(leg is None for leg in legs) else sum(legs)

    routes = [[start, *order] + ([end] if end else []) for order in permutations(visits)]
    if one_way: routes.append([start, end])
    budget_match = BUDGET.search(prompt)
    budget = int(budget_match.group(1)) if budget_match else None
    stays = [int(m.group(1)) for m in DURATION.finditer(prompt) if not (budget_match and m.start() >= budget_match.start(1) and m.end() <= budget_match.end())]
    options = []
    for route in routes:
        driving = total(route)
        if driving is None: continue
        option = {'route': route, 'driving_minutes': driving}
        if len(route) == 2: option['direct'] = True
        if budget is not None:
            spent = driving + (sum(stays) if len(route) > 2 else 0)
            option.update(total_with_stops_minutes=spent, fits_budget=spent <= budget)
        options.append(option)
    if not options: return None
    options.sort(key=lambda o: (o['driving_minutes'], len(o['route'])))
    result = {'start': start, 'end': end, 'visits': visits, 'options': options[:8], 'fastest': options[0]['route']}
    if budget is not None: result.update(time_budget_minutes=budget, minutes_at_stops=sum(stays))
    return result
