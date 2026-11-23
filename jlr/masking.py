"""Request-local token allocation, minimization and restoration.

Private values are replaced by <PRIVATE_n> placeholders before the larger model.
With role-preserving masking (JLR_MASKING_MODE=roles or roles_facts, the default)
the larger model is also told what each placeholder *is* ("your mother", "your
gym", "a phone number"): the role a saved entity plays, taken from a fixed table
in the privacy policy, never its name, address or value.
"""
import json
import os
import re
from .contract import ContractError
from .privacy_policy import assess, load_policy

MODES=('plain','roles','roles_facts')

def masking_mode():
    mode=os.environ.get('JLR_MASKING_MODE','roles_facts')
    return mode if mode in MODES else 'roles_facts'

def role_key(text):
    '''"My Mom" -> "mom", "the gym" -> "gym", "mom's house" -> "mom's house".'''
    return re.sub(r"^(?:my|our|the|your)\s+",'',' '.join(str(text).casefold().split()))

class PrivacyEnvelope:
    def __init__(self,mode=None):
        self.tokens={};self.reverse={};self.evidence_tokens={};self.legend={};self.sources={}
        self.mode=mode or masking_mode()
        self.roles=load_policy(os.environ.get('JLR_PRIVACY_POLICY',''))['role_descriptors']
    def token(self,raw):
        if raw not in self.reverse:
            token=f'<PRIVATE_{len(self.tokens)+1}>';self.tokens[token]=raw;self.reverse[raw]=token
        return self.reverse[raw]
    def describe(self,kind,names):
        '''The role a placeholder plays, from the policy table only (never from the values themselves).'''
        roles=self.roles;withhold=set(roles['withhold'])
        generic=roles['generic'].get(kind,roles['generic']['other'])
        table=roles.get(kind,{})
        for name in names:
            key=role_key(name)
            if any(word in withhold for word in re.findall(r"[a-z]+",key)):return generic
            if key in table:return table[key]
            owner=re.fullmatch(r"(.+?)'s (?:house|place|home)",key)
            if kind=='location' and owner and role_key(owner[1]) in roles['person']:
                return roles['person'][role_key(owner[1])]+"'s home"
        return generic
    def register(self,raw,kind,names):
        '''Placeholder for a private value, with its role in the legend.'''
        token=self.token(raw)
        if self.mode!='plain' and token not in self.legend:self.legend[token]=self.describe(kind,names)
        return token
    def prepare(self,prompt,context):
        private={}
        # source[value] = (kind, names that may describe its role, field)
        source={}
        for e in context['entities'].values():
            if not e.get('private'):continue
            names=[*e.get('aliases',[]),e.get('id','').split(':')[-1]]
            if e.get('one_off'):names=[]
            def collect(v,field=None):
                if isinstance(v,str) and v:
                    private[v]=v
                    kind=e['type'] if field in (None,'alias','label','value') else ('email' if field=='email' else 'address' if field=='address' else e['type'])
                    source.setdefault(v,(kind,names))
                elif isinstance(v,dict):
                    for k,item in v.items():
                        if k!='id':collect(item,k)
                elif isinstance(v,list):
                    for item in v:collect(item,field)
            collect(e['value'])
            source.setdefault(e['label'],(e['type'],names))
            for alias in e.get('aliases',[]):
                if alias:private[alias]=e['label']
        for f in context['findings']:
            if f['status']=='resolved' and f.get('source') in {'private_db','session'}:
                private[f['text']]=context['entities'][f['entity_id']]['label']
            elif f['type'] in {'person','contact_detail','personal_detail','preference'}:
                # An explicit health fact or account handle is still private
                # without a possessive pronoun. Ordinary public subjects use
                # other_private/uncertain and are not blindly tokenized.
                private[f['text']]=f['text'];source.setdefault(f['text'],(f['type'],[]))
        # Contact details in the request (email, phone, street address) are found by the privacy
        # policy's own patterns, so detection and masking can never disagree.
        for item in assess(prompt)['items']:
            if item['detector']=='pattern' and item['maskable']:
                private[item['text']]=item['text'];source.setdefault(item['text'],(item['category'],[]))
        if '<PRIVATE_' in prompt:raise ContractError('Reserved placeholder syntax in request')
        self.sources=source;self.private=private
        # One longest-first substitution pass prevents overlap and token collisions.
        pairs=sorted(private,key=len,reverse=True)
        # Whole words only: a short private value ("me") must not match inside "recommend".
        pattern=re.compile(r'(?<!\w)(?:'+'|'.join(re.escape(v) for v in pairs)+r')(?!\w)',re.I) if pairs else None
        folded={k.casefold():v for k,v in private.items()}
        def tokenize(text):
            value=folded[text.casefold()];kind,names=source.get(value,('other',[]))
            return self.register(value,kind,[text,*names])
        def mask(text):
            return pattern.sub(lambda m:tokenize(m.group()),text) if pattern else text
        safe_entities={key:{'type':e['type'],'label':mask(e['label'])} for key,e in context['entities'].items()}
        # IDs passed externally are opaque, not aliases containing private data.
        ids={key:f'entity_{i}' for i,key in enumerate(safe_entities,1)}
        self.entity_ids={v:k for k,v in ids.items()}
        safe={'entities':{ids[k]:v for k,v in safe_entities.items()},'simulation':True}
        def mask_prompt(match):
            token=tokenize(match.group())
            self.evidence_tokens.setdefault(token,match.group())
            return token
        sanitized=pattern.sub(mask_prompt,prompt) if pattern else prompt
        if self.legend:safe['placeholders']=self.legend
        self.check_outgoing(json.dumps(safe)+' '+sanitized)
        return sanitized,safe
    def check_outgoing(self,text):
        '''Fail closed if any private value is still present in text that leaves the device.
        Role words from the policy table ("home", "gym", "mom") are descriptions, not identities.'''
        roles={key for table in ('person','location') for key in self.roles[table]}
        for raw in getattr(self,'private',{}):
            if len(raw)>3 and role_key(raw) not in roles and re.search(r'(?<!\w)'+re.escape(raw)+r'(?!\w)',text,re.I):
                raise ContractError('Private data remained in outgoing payload')
    def restore(self,value):
        if isinstance(value,str):
            # Restore in a single pass; substituted private values are not rescanned.
            if value in getattr(self,'entity_ids',{}):return self.entity_ids[value]
            def replace(m):
                if m.group() not in self.tokens:raise ContractError('Unknown privacy placeholder')
                return self.tokens[m.group()]
            stripped=re.sub(r'<PRIVATE_\d+>', '',value)
            if '<PRIVATE_' in stripped:raise ContractError('Malformed privacy placeholder')
            return re.sub(r'<PRIVATE_\d+>',replace,value)
        if isinstance(value,list):return [self.restore(v) for v in value]
        if isinstance(value,dict):
            result={}
            for k,v in value.items():
                if k=='evidence' and isinstance(v,str):
                    restored=self.restore(v)  # validates unknown/malformed tokens
                    result[k]=re.sub(r'<PRIVATE_\d+>',lambda m:self.evidence_tokens.get(m.group(),self.tokens[m.group()]),v)
                else:result[k]=v if k in {'kind','capability','slot'} else self.restore(v)
            return result
        return value
