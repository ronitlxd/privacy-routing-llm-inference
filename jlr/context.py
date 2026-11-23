"""Typed local entity lookup and conversational bindings for V2."""
from contextlib import contextmanager
import copy
import json
import re
import sqlite3
import threading
from .demo_store import DemoStore

DEFAULT_PREFERENCE_ALIASES={
    'usual_playlist':['my usual playlist','my favourite music','my favorite music','my favourite songs','my favorite songs','my favourite song','my favorite song','my music','my usual music','my preferred music'],
    'usual_drink':['my usual drink','my usual coffee']}
EDITABLE={'contact':('contacts','person'),'place':('places','location'),'preference':('preferences','preference')}

class EntityStore:
    """Private entities for one driver.

    File mode opens the SQLite file per call; connection mode works on one
    in-memory database (a decrypted profile, or the empty guest store).
    """
    def __init__(self,path=None,*,connection=None,seed=True):
        self.lock=threading.RLock();self.shared=connection
        seed_store=DemoStore(path,connection=connection,seed=seed)
        self.path=seed_store.path
        seed_store.close()
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS v2_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entity_aliases(kind TEXT,alias TEXT,entity_key TEXT,PRIMARY KEY(kind,alias,entity_key));''')
            db.execute("INSERT OR IGNORE INTO v2_metadata VALUES ('schema','2')")
            # Saved places carry coordinates so routing never sends private address text off the server.
            columns={row[1] for row in db.execute('PRAGMA table_info(places)')}
            for column in ('latitude','longitude'):
                if column not in columns:db.execute(f'ALTER TABLE places ADD COLUMN {column} REAL')
            for kind,table in [('person','contacts'),('location','places')]:
                for row in db.execute('SELECT alias,label FROM '+table).fetchall():
                    self._alias(db,kind,row['alias'],row['label'])
            for key,aliases in DEFAULT_PREFERENCE_ALIASES.items():
                for alias in aliases:db.execute('INSERT OR IGNORE INTO entity_aliases VALUES (?,?,?)',('preference',alias,key))
    @staticmethod
    def _alias(db,kind,alias,label):
        for name in [alias,label]:
            db.execute('INSERT OR IGNORE INTO entity_aliases VALUES (?,?,?)',(kind,name.casefold(),alias))
    @contextmanager
    def connect(self):
        if self.shared is not None:
            with self.lock:
                try:
                    yield self.shared
                    self.shared.commit()
                except Exception:
                    self.shared.rollback();raise
            return
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback();raise
        finally:db.close()
    def close(self):
        if self.shared is not None:self.shared.close()
    def saved(self):
        """Called after an edit; profile stores persist themselves here."""
    def put(self,kind,record):
        """Add or replace one contact, place or preference (aliases follow the row)."""
        table,entity_kind=EDITABLE[kind]
        with self.connect() as db:
            if kind=='preference':
                db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)',(record['key'],record['value']))
            else:
                alias=record['alias'].strip().casefold()
                db.execute('DELETE FROM entity_aliases WHERE kind=? AND entity_key=?',(entity_kind,alias))
                if kind=='contact':
                    db.execute('INSERT OR REPLACE INTO contacts(alias,label,email) VALUES (?,?,?)',(alias,record['label'].strip(),record.get('email','').strip()))
                else:
                    db.execute('INSERT OR REPLACE INTO places(alias,label,address,latitude,longitude) VALUES (?,?,?,?,?)',
                               (alias,record['label'].strip(),record.get('address','').strip(),record['latitude'],record['longitude']))
                self._alias(db,entity_kind,alias,record['label'].strip())
        self.saved()
    def remove(self,kind,key):
        table,entity_kind=EDITABLE[kind];key=key.strip().casefold() if kind!='preference' else key
        with self.connect() as db:
            if kind=='preference':db.execute('DELETE FROM preferences WHERE key=?',(key,))
            else:
                db.execute(f'DELETE FROM {table} WHERE alias=?',(key,))
                db.execute('DELETE FROM entity_aliases WHERE kind=? AND entity_key=?',(entity_kind,key))
        self.saved()
    def vocabulary(self):
        """Every saved name and alias, for spelling correction of this driver's own words."""
        with self.connect() as db:
            return sorted({row[0] for row in db.execute('SELECT alias FROM entity_aliases')}|
                          {row[0].casefold() for table in ('contacts','places') for row in db.execute('SELECT label FROM '+table)})
    def entities(self):
        result=[]
        with self.connect() as db:
            for kind,table in [('person','contacts'),('location','places'),('preference','preferences')]:
                for row in db.execute('SELECT * FROM '+table):
                    value=dict(row);key=value.get('alias',value.get('key'))
                    aliases=[r[0] for r in db.execute('SELECT alias FROM entity_aliases WHERE kind=? AND entity_key=?',(kind,key))]
                    aliases += [value.get('label',value.get('value',''))]
                    result.append({'id':f'{kind}:{key}','type':kind,'source':'private_db','private':True,
                                   'label':value.get('label',value.get('value')),'value':value,'aliases':aliases})
        return result
    def find(self,text,kind=None):
        # "my sister" finds a contact saved as "sister", and "mom" one saved as "my mom".
        own=lambda value:re.sub(r'^(?:my|our)\s+','',value.strip().casefold())
        text=text.strip().casefold()
        return [e for e in self.entities() if (not kind or kind==e['type']) and (text==e['id'].casefold() or own(text) in [own(a) for a in e['aliases'] if a])]
    def latest_message(self,entity):
        with self.connect() as db:
            row=db.execute('SELECT sender,body,created FROM messages WHERE lower(sender)=lower(?) ORDER BY created DESC LIMIT 1',(entity['label'],)).fetchone()
        return dict(row) if row else None

def explicit_entity(value,kind,source='user'):
    return {'id':f'{source}:{kind}:{value.casefold()}', 'type':kind,'source':source,
            'private':source=='session','label':value,'value':value,'aliases':[value]}

# A relationship ("mom", "my boss") points at a saved person and must be resolved; a typed
# name ("Dave", "Priya Shah") or number names the person itself.
RELATIONSHIP=re.compile(r"\b(?:my|our|mom|mum|mother|dad|father|parents?|wife|husband|partner|spouse|boss|manager|"
                        r"colleague|coworker|brother|sister|sibling|son|daughter|kids?|child|children|friend|girlfriend|"
                        r"boyfriend|grand(?:ma|pa|mother|father)|aunt|uncle|cousin|nephew|niece|neighbou?r|doctor|dentist)\b",re.I)
PERSON_NAME=re.compile(r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,2}")
PHONE_NUMBER=re.compile(r"\+?\d[\d ()\-]{6,}\d")

def one_off_contact(text):
    '''A contact the driver named or typed that is not saved: used once, still confirmed before any call or message.'''
    text=text.strip()
    if RELATIONSHIP.search(text) or not (PERSON_NAME.fullmatch(text) or PHONE_NUMBER.fullmatch(text)):return None
    return dict(explicit_entity(text,'person'),private=True,one_off=True)

PERSONAL=re.compile(r'\b(?:my|our|usual|favou?rite|preferred)\b',re.I)
PREFERENCE_KEYS=[('usual_playlist',re.compile(r'\b(?:songs?|music|playlists?|tracks?|tunes?)\b',re.I)),
                 ('usual_drink',re.compile(r'\b(?:drinks?|coffee|latte|beverage)\b',re.I))]

def preference_key(text):
    '''Map wording such as "my usual songs" or "our favourite tunes" to a saved preference key.'''
    if not PERSONAL.search(text):return None
    return next((key for key,pattern in PREFERENCE_KEYS if pattern.search(text)),None)

ANAPHORA=re.compile(r"\b(?:it|its|that|this|these|those|there|then|them|they|again|same|earlier|previous|previously|before|last|discussed|above|mentioned|him|her|his|hers)\b",re.I)

def supplied_in_request(text,prompt):
    '''True when the request itself states the value: "<reference> is/are/was/: <value>".'''
    if re.search(r'\b(?:is|are|was|were)\s+\S|[:=]\s*\S',text):return True
    return bool(re.search(r'(?<!\w)'+re.escape(text)+r'\s*(?:is|are|was|were|=|:)\s*(?:about|around|roughly|only|still)?\s*[\w$]',prompt,re.I))

# Asking to keep something in the driver's profile ("remember to …" is a reminder, not a save).
SAVE_REQUEST=re.compile(r"(?:remember(?!\s+to\b)|save|store|update|change|set)\b",re.I)

def defined_here(text,prompt):
    '''True when the reference sits in a clause that asks to save it ("Save this place as my gym",
    "Remember Priya Shah is my sister"), not merely somewhere near the word "save".'''
    from .contract import clauses,clause_body,ACTION_START
    spans=clauses(prompt);saving=False
    for a,b in spans:
        body=clause_body(prompt[a:b])
        # A clause continues the save before it ("Save 42.31, -83.03 as my gym") unless it starts a new action.
        if SAVE_REQUEST.match(body):saving=True
        elif ACTION_START.match(body):saving=False
        if saving and text.casefold() in prompt[a:b].casefold():return True
    return False

def resolve_reference(text,kind,source,session,store):
    remembered=session.clarified_references.get((kind,text.strip().casefold()))
    if remembered:return [copy.deepcopy(remembered)]
    matches=store.find(text,kind if kind in {'person','location','preference'} else None)
    if matches:return matches
    key=preference_key(text) if kind not in {'person','location'} else None
    if key:
        matches=store.find('preference:'+key,'preference')
        if matches:return matches
    normalized=text.casefold().strip()
    if source=='session' or normalized in {'there','it','that','him','her','them','that song','that song again','my destination','my current destination'}:
        key = ('last_contact' if kind=='person' or normalized in {'him','her','them'} else
               'destination' if kind=='location' or re.search(r'destination|route|place|there',normalized) else
               'last_draft' if re.search(r'draft|message|text',normalized) else
               'playback' if kind=='preference' or re.search(r'song|music|playlist',normalized) else None)
        if key is None:
            # A bare pronoun must not silently select music when it could mean
            # a destination or a drafted message. Multiple candidates clarify.
            return [explicit_entity(session.state[k],typ,'session')
                    for k,typ in [('last_draft','text'),('destination','location'),('playback','preference')]
                    if isinstance(session.state.get(k),str) and session.state[k]]
        entity=session.references.get(key)
        if entity:return [dict(entity,source='session')]
        value=session.state.get(key)
        if isinstance(value,str) and value:return [explicit_entity(value, 'location' if key=='destination' else 'text' if key=='last_draft' else 'preference','session')]
    return []

def context_for(prompt,refs,session,store,bindings=None):
    bindings=bindings or {};references=list(refs or []);known=store.entities()
    # Retain explicitly clarified reference meanings within this session only.
    # Do not carry arbitrary action arguments (e.g. a message body) between tasks.
    for (kind,alias),entity in session.clarified_references.items():
        match=re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',prompt,re.I)
        if match and not any(r['text'].casefold()==alias for r in references):
            references.append({'text':match.group(),'type':kind,'context_source':'session','detector':'session_binding'})
    # Data-driven aliases, longest first. Do not pretend the router detected them.
    for entity in known:
        for alias in sorted(set(entity['aliases']),key=len,reverse=True):
            if not alias:continue
            match=re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',prompt,re.I)
            if match and not any(r['text'].casefold()==match.group().casefold() for r in references):
                references.append({'text':match.group(),'type':entity['type'],'context_source':'private_db','detector':'database_alias'})
    findings=[];entities={};missing=[]
    for ref in references:
        text=ref['text'];kind=ref['type'];source=ref.get('context_source','uncertain')
        matches=[bindings[text.casefold()]] if text.casefold() in bindings else resolve_reference(text,kind,source,session,store)
        record={'text':text,'type':kind,'source':source,'status':'not_required','detector':ref.get('detector','router')}
        if len(matches)==1:
            e=matches[0];entities[e['id']]=e
            record.update(status='resolved',source=e['source'],entity_id=e['id'],label=e['label'])
        elif len(matches)>1:
            entities.update({e['id']:e for e in matches})
            record.update(status='ambiguous',candidates=[{'id':e['id'],'label':e['label']} for e in matches]);missing.append(record)
        else:
            required=source=='session' or kind=='person' or (kind in {'location','preference'} and bool(re.search(r'\b(?:my|our|usual|favou?rite|home|work|there|that)\b',text,re.I)))
            # "my nearest/closest X" is a relative public place search from the
            # configured origin.  The possessive does not imply a saved place.
            relative_place=kind=='location' and bool(re.search(r'\b(?:nearest|nearby|closest)\b',prompt,re.I))
            if relative_place:
                required=False;e=explicit_entity(text,'location');entities[e['id']]=e
                record.update(status='resolved',entity_id=e['id'],source='user',label=text)
            # First-person subjects identify the current driver, not a missing
            # contact. Application-state references can be read even when unset.
            if text.casefold() in {'i','me','myself','the call','current call','the current call'}:required=False
            prefix=re.match(r'\s*(?:ignore\b[^:]*routing[^:]*|use\b[^:]*cloud[^:]*|keep (?:this|it) local)\s*:',prompt,re.I)
            if prefix and text in prefix.group() and text not in prompt[prefix.end():]:required=False
            if kind=='person' and text.casefold() in {'my family','my team'}:required=False
            if re.search(r'current destination|where.*going',prompt,re.I) and ('destination' in text.casefold()):required=False
            if kind=='personal_detail' and re.search(r'appointment|battery|vehicle|calendar',text,re.I):required=False
            # A session reference must point back at something ("it", "that", "there", "we discussed"...);
            # a phrase without a pointing word ("recommend one for me") has nothing to resolve.
            if source=='session' and kind!='person' and not ANAPHORA.search(text):required=False
            # A personal fact whose value the driver states in this request ("my salary is 52000",
            # "my blood pressure is 150 over 95") needs no lookup. It stays personal: the privacy
            # layer still detects and masks it.
            if kind!='person' and supplied_in_request(text,prompt):
                required=False;e=explicit_entity(text,kind);entities[e['id']]=e
                record.update(status='resolved',entity_id=e['id'],source='user',label=text,supplied=True)
            # "Save this place as my gym", "Remember Priya Shah is my sister": the request defines the
            # reference, so there is nothing to look up. The save itself is checked and confirmed later.
            if required and source!='session' and defined_here(text,prompt):
                required=False;e=explicit_entity(text,kind);entities[e['id']]=e
                record.update(status='resolved',entity_id=e['id'],source='user',label=text,supplied=True)
            # "Call Dave" with no saved Dave: a one-off contact, not a question the driver cannot answer.
            person=one_off_contact(text) if required and kind=='person' and source!='session' else None
            if person:
                required=False;entities[person['id']]=person
                record.update(status='resolved',entity_id=person['id'],source='user',label=text,one_off=True)
            if required:record['status']='missing';missing.append(record)
            # Explicit public locations are valid task parameters, not missing DB entries.
            elif kind=='location' and source=='none':
                e=explicit_entity(text,'location');entities[e['id']]=e;record.update(status='resolved',entity_id=e['id'],source='user',label=text)
        findings.append(record)
    for text,e in bindings.items():entities[e['id']]=e
    return {'status':'ambiguous' if any(f['status']=='ambiguous' for f in missing) else 'missing' if missing else 'resolved',
            'findings':findings,'missing':missing,'entities':entities,'bindings':bindings,
            'session_state':copy.deepcopy(session.state)}

def bind_argument(value,kind,context,store):
    if not value.strip():return None
    e=context['entities'].get(value)
    if e and (e['type']==kind or (kind=='text')):return e
    for f in context['findings']:
        if value.casefold()==f['text'].casefold() and f.get('entity_id'):
            e=context['entities'][f['entity_id']]
            if e['type']==kind or kind=='text':return e
    e=context.get('bindings',{}).get(value.casefold())
    if e and (e['type']==kind or kind=='text'):return e
    matches=store.find(value,kind)
    return matches[0] if len(matches)==1 else None
