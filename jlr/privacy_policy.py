"""Privacy sensitivity tiers and the complexity x privacy routing decision.

Every request gets a sensitivity tier (P0 public .. P3 restricted) from four
detectors: deterministic patterns (credentials, card/ID numbers with checksums,
contact details), a content-category lexicon (health, finance, legal) that only
counts when the sentence is about the driver or someone they know, the router's
personal references, and saved-data matches from Context Resolution.

The final route combines the router's complexity judgment with that tier:
simple/uncertain requests stay local; complex requests may use the larger model
only when every sensitive item can be masked. The rules live in the versioned
privacy_policy.json, not in code.
"""
import json
import os
import re
from functools import lru_cache
from pathlib import Path

TIERS = ('P0', 'P1', 'P2', 'P3')
PERSONAL_MARKER = re.compile(r"\b(?:i|i'm|i've|im|me|my|mine|myself|we|our|ours)\b", re.I)
# "us" is personal only in lowercase: the all-caps "US" is the country ("Compare US mortgage rates").
PERSONAL_US = re.compile(r"(?<![A-Za-z])us(?![A-Za-z])")


def personal(sentence):
    return bool(PERSONAL_MARKER.search(sentence) or PERSONAL_US.search(sentence))
CATEGORY_NAMES = {'health': 'health', 'finance': 'financial', 'legal': 'legal', 'credential': 'credential',
                  'payment_card': 'payment card', 'government_id': 'government ID', 'bank_account': 'bank account',
                  'email': 'email', 'phone': 'phone', 'address': 'address'}
REASONS = {
    'local_suitable': 'A routine request fits the local model.',
    'local_uncertain': 'The request is unclear, so it stays local by policy.',
    'local_policy_override': 'Routing was held local by policy.',
    'stronger_public': 'Needs stronger reasoning and contains no personal data, so it goes to the larger model unmasked.',
    'stronger_masked': 'Needs stronger reasoning; personal data can be masked, so it goes to the larger model with placeholders.',
    'sensitive_kept_local': 'Needs stronger reasoning, but sensitive content cannot be masked without losing its meaning, so it stays on the local model.',
    'restricted_kept_local': 'Contains restricted data that must never leave the device, so it stays on the local model.',
}


@lru_cache(maxsize=4)
def load_policy(path=''):
    file = Path(path) if path else Path(__file__).with_name('privacy_policy.json')
    return json.loads(file.read_text(encoding='utf-8'))


def _luhn(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return len(digits) >= 9 and total % 10 == 0


def _sentences(prompt):
    """Yield (start, end) spans of sentences/clauses separated by . ! ? ; or new lines."""
    start = 0
    for match in re.finditer(r'[.!?;\n]+', prompt):
        yield start, match.end()
        start = match.end()
    yield start, len(prompt)


def assess(prompt, references=(), findings=(), policy=None):
    """Return the request's sensitivity: highest tier plus every detected item."""
    policy = policy or load_policy(os.environ.get('JLR_PRIVACY_POLICY', ''))
    items, spans = [], []
    people = [r['text'] for r in references if r.get('type') == 'person' and r.get('text')]

    def overlaps(start, end):
        return any(start < b and a < end for a, b in spans)

    def add(text, start, end, category, rule, detector):
        if start is None:
            # Router/context items carry no span: locate them so a phrase that contains an
            # already-detected (stricter) item is absorbed instead of echoing its value.
            found = prompt.casefold().find(text.casefold())
            if found >= 0:
                start, end = found, found + len(text)
        if start is not None:
            if overlaps(start, end):
                return
            spans.append((start, end))
        key = text.casefold()
        for existing in items:
            if existing['text'].casefold() == key:
                if TIERS.index(rule['tier']) > TIERS.index(existing['tier']):
                    existing.update(category=category, tier=rule['tier'], maskable=rule['maskable'], detector=detector)
                return
        items.append({'text': text, 'category': category, 'tier': rule['tier'], 'maskable': rule['maskable'], 'detector': detector})

    # 1. Patterns, most restrictive first, so a card number is not also read as a phone number.
    for name, rule in sorted(policy['patterns'].items(), key=lambda kv: -TIERS.index(kv[1]['tier'])):
        for match in re.finditer(rule['regex'], prompt, re.I):
            if rule.get('checksum') == 'luhn' and not _luhn(re.sub(r'\D', '', match.group())):
                continue
            add(match.group().strip(), match.start(), match.end(), name, rule, 'pattern')

    # 2. Content categories: only when the sentence is about the driver or someone they know.
    for category, rule in policy['content_categories'].items():
        terms = sorted(rule['terms'], key=len, reverse=True)
        pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(term) for term in terms) + r')(?!\w)', re.I)
        for start, end in _sentences(prompt):
            sentence = prompt[start:end]
            if not (personal(sentence) or any(p.casefold() in sentence.casefold() for p in people)):
                continue
            for match in pattern.finditer(sentence):
                add(match.group(), start + match.start(), start + match.end(), category, rule, 'content_lexicon')

    # 3. Personal references flagged by the router.
    for ref in references:
        rule = policy['reference_types'].get(ref.get('type'), policy['reference_types']['other_private'])
        add(ref['text'], None, None, rule['category'], rule, 'router')

    # 4. Saved names and session references matched by Context Resolution.
    for finding in findings:
        rule = policy['context_sources'].get(finding.get('source'))
        if rule and finding.get('status') == 'resolved':
            add(finding['text'], None, None, rule['category'], rule, 'context')

    tier = max((item['tier'] for item in items), key=TIERS.index, default='P0')
    return {'policy_version': policy['version'], 'tier': tier, 'tier_name': policy['tiers'][tier]['name'],
            'items': items, 'unmaskable': [i for i in items if not i['maskable'] and i['tier'] in ('P2', 'P3')]}


def decide(routing, sensitivity):
    """Combine the router's complexity judgment with the sensitivity tier."""
    suitability = (routing.get('assessment') or {}).get('local_suitability')
    base = routing.get('reason_code')
    decision = {'complexity': suitability, 'tier': sensitivity['tier'], 'policy_version': sensitivity['policy_version'],
                'router_route': routing.get('next_route')}
    if routing.get('policy_override') or base == 'local_policy_override' or routing.get('next_route') != 'privacy_layer':
        code = base if base in REASONS else ('local_uncertain' if suitability == 'uncertain' else 'local_suitable')
        return {**decision, 'next_route': 'local_slm', 'reason_code': code, 'reason': REASONS[code], 'notice': None}
    blocked = sensitivity['unmaskable']
    if any(item['tier'] == 'P3' for item in blocked):
        code = 'restricted_kept_local'
    elif blocked:
        code = 'sensitive_kept_local'
    else:
        code = 'stronger_public' if sensitivity['tier'] == 'P0' else 'stronger_masked'
    route = 'local_slm' if code.endswith('kept_local') else 'privacy_layer'
    notice = None
    if route == 'local_slm':
        kinds = sorted({CATEGORY_NAMES.get(item['category'], item['category']) for item in blocked})
        notice = (f"Kept on the local model for privacy: this request contains {' and '.join(kinds)} information "
                  "that cannot be safely shared, so the answer may be less detailed.")
    return {**decision, 'next_route': route, 'reason_code': code, 'reason': REASONS[code], 'notice': notice}


def outbound_blocklist(sensitivity):
    """Texts that must never appear in any external tool request (P3 and unmaskable P2)."""
    return {item['text'].casefold() for item in sensitivity.get('unmaskable', []) if len(item['text'].strip()) >= 3}
