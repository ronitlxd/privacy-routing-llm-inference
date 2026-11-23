"""Saving private data from the conversation: profile.save_preference, save_contact, save_place.

A save changes the signed-in driver's encrypted profile, so it is held to stricter rules
than other actions:
- signed-in profiles only (the guest has no private database);
- every saved value is the driver's own words in this request, never a model's guess,
  a tool result or a placeholder; a value that is not is refused, not just confirmed;
- a place comes from coordinates already on this server (the current destination), typed
  coordinates, or a typed address; an address is looked up on the map search only after the
  driver approves (the card says so), and the search is not told what the place is;
- the exact record is part of the plan, so the driver's confirmation (bound to the plan
  hash) approves exactly what is written.
The write itself goes through the profile store, which re-encrypts the profile file at once.
"""
import re

from .contract import ContractError, grounded_in
from .context import PHONE_NUMBER
from .tools import parse_coordinates

SAVE_CAPABILITIES = frozenset({'profile.save_preference', 'profile.save_contact', 'profile.save_place'})
KIND = {'profile.save_preference': 'preference', 'profile.save_contact': 'contact', 'profile.save_place': 'place'}
# Saved preferences the assistant uses (the profile editor offers the same two).
PREFERENCES = {
    'usual_playlist': ('usual music', re.compile(r'\b(?:songs?|music|playlists?|tracks?|tunes?|bands?|artists?)\b', re.I)),
    'usual_drink': ('usual drink', re.compile(r'\b(?:drinks?|coffee|latte|tea|beverage)\b', re.I)),
}
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# Words that point at the place on screen: the current destination.
CURRENT_PLACE = re.compile(r'(?:here|there|it|this|that|(?:this|that|the|my)(?: current)? (?:place|location|destination|spot)|current destination)', re.I)
MAX_LENGTH = {'preference': 80, 'name': 80, 'alias': 40, 'detail': 120}


class SaveError(ContractError):
    pass


def own_words(value, prompt, label, limit, required=True):
    """The value, if it is the driver's own words in this request."""
    value = ' '.join(str(value or '').split()).strip(' .,!?;:"\'')
    if not value:
        if required: raise SaveError(f'Tell me the {label} to save.')
        return ''
    if len(value) > limit: raise SaveError(f'The {label} is too long to save (at most {limit} characters).')
    if '<PRIVATE_' in value or not grounded_in(value, prompt):
        raise SaveError(f'Nothing was saved: the {label} must be in your own words in this request.')
    return value


def alias_of(text):
    """What the driver calls a person or place: "my sister" -> "sister"."""
    return re.sub(r'^(?:my|our|the)\s+', '', text, flags=re.I).strip().casefold()


def current_value(store, kind, key):
    found = store.find(key if kind != 'preference' else 'preference:' + key, {'preference': 'preference', 'contact': 'person', 'place': 'location'}[kind])
    return found[0]['label'] if found else None


def prepare(capability, arguments, session, prompt):
    """Check a proposed save and return exactly what would be written (shown on the confirmation card).

    Raises SaveError when it cannot be saved; nothing is written here.
    """
    profile = session.profile or {}
    if not profile.get('protected'):
        raise SaveError('Sign in to your profile to save private data. The guest profile keeps nothing.')
    kind = KIND[capability]; lookup = None
    if kind == 'preference':
        named = own_words(arguments['preference'], prompt, 'preference name', MAX_LENGTH['preference'])
        key = next((k for k, (_, pattern) in PREFERENCES.items() if pattern.search(named)), None)
        if not key: raise SaveError('I can remember your usual music or your usual drink.')
        value = own_words(arguments['value'], prompt, PREFERENCES[key][0], MAX_LENGTH['preference'])
        record = {'key': key, 'value': value}
        summary = f'{PREFERENCES[key][0]}: {value}'
    elif kind == 'contact':
        alias = alias_of(own_words(arguments['relation'], prompt, 'name you call them', MAX_LENGTH['alias'] + 4))
        name = own_words(arguments['name'], prompt, 'contact name', MAX_LENGTH['name'])
        detail = own_words(arguments['detail'], prompt, 'email or phone', MAX_LENGTH['detail'], required=False)
        if detail and not (EMAIL.fullmatch(detail) or PHONE_NUMBER.fullmatch(detail)):
            raise SaveError('Only an email address or phone number can be saved with a contact.')
        if not alias: raise SaveError('Tell me what you call this contact.')
        record = {'alias': alias, 'label': name, 'email': detail}
        summary = f'contact "{alias}": {name}' + (f' ({detail})' if detail else '')
        key = alias
    else:
        alias = alias_of(own_words(arguments['name'], prompt, 'place name', MAX_LENGTH['alias'] + 4))
        if not alias: raise SaveError('Tell me what to call this place.')
        where = ' '.join(str(arguments.get('place') or '').split()).strip(' .,!?')
        coordinates = parse_coordinates(where) if where else None
        if coordinates:
            own_words(where, prompt, 'coordinates', 60)
            record = {'alias': alias, 'label': alias.title(), 'address': '', 'latitude': coordinates[0], 'longitude': coordinates[1]}
            summary = f'place "{alias}" at {coordinates[0]}, {coordinates[1]}'
        elif not where or CURRENT_PLACE.fullmatch(where):
            if where: own_words(where, prompt, 'place', 60)
            point = (session.state or {}).get('destination_point')
            if not point or point.get('latitude') is None:
                raise SaveError('There is no current destination to save. Navigate there first, or tell me its address or coordinates.')
            label = point.get('name') or alias.title()
            record = {'alias': alias, 'label': label, 'address': point.get('address') or '',
                      'latitude': float(point['latitude']), 'longitude': float(point['longitude'])}
            summary = f'place "{alias}": {label}' + (f', {record["address"]}' if record['address'] else '')
        else:
            # A typed address needs coordinates for routing. It is looked up only after the driver
            # approves (the card says so), and the map search is not told what the place is.
            address = own_words(where, prompt, 'address', MAX_LENGTH['detail'])
            record = {'alias': alias, 'label': alias.title(), 'address': address, 'latitude': None, 'longitude': None}
            summary = f'place "{alias}": {address}'
            lookup = address
        key = alias
    replaces = current_value(session.store, kind, key)
    save = {'kind': kind, 'record': record, 'profile': profile.get('name', ''), 'summary': summary, 'replaces': replaces}
    if lookup:
        save['lookup'] = {'address': lookup, 'service': 'OpenStreetMap search (Nominatim)',
                          'notice': 'On Yes, only this address is looked up on OpenStreetMap search to find it on the map; '
                                    'the search is not told it is your ' + alias + '.'}
    return save


def write(save, session, resolve_address=None, blocked=()):
    """Write an approved save to the signed-in profile (re-encrypted to its file by the store).

    A typed address is looked up here, after approval, with resolve_address(address) -> map result.
    `blocked` holds restricted values that may never leave the server.
    """
    profile = session.profile or {}
    if not profile.get('protected') or profile.get('name') != save['profile']:
        raise SaveError('The signed-in profile changed, so nothing was saved.')
    record = dict(save['record']); found = None
    if save.get('lookup'):
        address = save['lookup']['address']
        if resolve_address is None: raise SaveError('Map search is not available, so the address could not be found. Nothing was saved.')
        if any(value and value in address.casefold() for value in blocked):
            raise SaveError('This address contains restricted information, so it was not looked up. Nothing was saved.')
        found = resolve_address(address)
        if found.get('status') != 'resolved':
            raise SaveError(f'The address {address} could not be found on the map, or matches several places. Nothing was saved.')
        place = found['place']
        # Street level only (the house number is not on the map): keep the driver's own address text.
        street = place.get('precision') == 'street'
        record.update(latitude=float(place['latitude']), longitude=float(place['longitude']),
                      address=address if street else place.get('address') or address)
    session.store.put(save['kind'], record)
    # A meaning the driver gave earlier in this session must not hide the saved one.
    alias = record.get('alias')
    if alias:
        kind = 'person' if save['kind'] == 'contact' else 'location'
        for reference in [alias, 'my ' + alias]: session.clarified_references.pop((kind, reference), None)
    summary = save['summary'] if not found else f'place "{alias}": {record["address"]}' + (
        ' (the house number is not on the map, so the map point is on the street, not the exact house)'
        if found['place'].get('precision') == 'street' else '')
    return {'status': 'completed', 'simulation': False, 'saved': save['kind'], 'profile': save['profile'],
            'address_lookup': found, 'message': f"Saved to {save['profile']}'s profile: {summary}."}
