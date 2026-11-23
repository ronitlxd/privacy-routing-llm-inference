"""Prompt cleanup: fix format and spelling before the router sees the prompt.

Runs locally and deterministically, before the privacy tier is known, so it
never calls a model or an external service.

Only real non-words are corrected. A word is "real" when it is in the bundled
English dictionary (english_words.txt, from SCOWL) or a regular form of one;
capitalised dictionary names (Dave, Wendy) count only when typed capitalised.
A non-word is corrected towards a small target vocabulary: driving commands and
place types, common brands, the privacy policy's sensitive terms and the
signed-in driver's own saved names. The words right after call/text/message/...
name a person, so they are corrected only towards the driver's saved names.
Numbers, e-mail addresses, URLs, quoted text, credentials and message bodies
("... saying <text>") are left exactly as typed.
"""
from __future__ import annotations
import difflib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

HERE=Path(__file__).resolve().parent
WORD=re.compile(r"[A-Za-z][A-Za-z'&-]*")
# Everything after one of these is the driver's own message text.
MESSAGE_BODY=re.compile(r"\b(?:saying|that says|which says|to say|telling (?:him|her|them))\b",re.I)
PROTECTED=[re.compile(r'"[^"]*"'),re.compile(r"\S+@\S+"),re.compile(r"\bhttps?://\S+",re.I),re.compile(r"\S*\d\S*")]
# The words after these verbs name a person ("call Dave", "text my wiife").
PERSON_VERB=re.compile(r"\b(?:call|ring|dial|phone|text|message|tell|email|e-mail|ask|remind)\s+(?:my\s+|our\s+)?",re.I)

def letters(text):return re.sub(r'[^a-z]','',text.casefold())

@lru_cache(maxsize=1)
def lexicon():
    data=json.loads((HERE/'cleanup_lexicon.json').read_text(encoding='utf-8'))
    policy=json.loads((HERE/'privacy_policy.json').read_text(encoding='utf-8'))
    PROTECTED.extend(re.compile(p['regex'],re.I) for p in policy['patterns'].values() if p.get('regex'))
    sensitive={w for c in policy['content_categories'].values() for term in c['terms'] for w in term.split()}
    words=set(data['commands'].split())|sensitive
    brands={}
    for brand in data['brands']:brands.setdefault(len(WORD.findall(brand)),{})[letters(brand)]=brand
    entries=[w for w in (HERE/'english_words.txt').read_text(encoding='utf-8').split('\n') if w and not w.startswith('#')]
    dictionary={w for w in entries if w.islower()}|set(data['common_words'].split())|words
    names={w.casefold() for w in entries if not w.islower()}
    return {'version':data['version'],'words':words,'brands':brands,'brands_flat':list(data['brands']),'dictionary':dictionary,'names':names}

def distance(a,b):
    '''Edit distance counting a swap of neighbouring letters as one edit.'''
    d=[[i+j if not i*j else 0 for j in range(len(b)+1)] for i in range(len(a)+1)]
    for i in range(1,len(a)+1):
        for j in range(1,len(b)+1):
            d[i][j]=min(d[i-1][j]+1,d[i][j-1]+1,d[i-1][j-1]+(a[i-1]!=b[j-1]))
            if i>1 and j>1 and a[i-1]==b[j-2] and a[i-2]==b[j-1]:d[i][j]=min(d[i][j],d[i-2][j-2]+1)
    return d[-1][-1]

def skeleton(word):
    '''First letter plus consonants, repeats collapsed: how a name sounds when vowels are guessed.'''
    return re.sub(r'(.)\1+',r'\1',word[:1]+re.sub(r'[aeiouy]','',word[1:]))

def known_word(word,known,names=frozenset()):
    '''A real word, or a regular form of one (plural, -ed, -ing, -ly, -er, missing apostrophe).
    A dictionary name (Dave) counts only when typed with a capital letter.'''
    if word[:1].isupper() and word.casefold().replace("'",'') in names:return True
    word=word.casefold().replace("'",'')
    if word in known:return True
    for suffix in ('s','es','ed','ing','ly','er','nt','t','d','m','re','ve','ll'):
        stem=word[:-len(suffix)]
        if word.endswith(suffix) and len(stem)>=2 and (stem in known or stem+'e' in known or (len(stem)>2 and stem[-1]==stem[-2] and stem[:-1] in known)):return True
    return False

def unique_best(scored):
    if not scored:return None
    scored.sort(key=lambda x:x[0]);best=scored[0][0]
    choices={r for d,r in scored if d==best}
    return choices.pop() if len(choices)==1 else None

def word_match(key,candidates,own):
    '''One typo for ordinary words (two from nine letters); three-letter words only towards the driver's own names.'''
    scored=[]
    for cand in candidates:
        budget=0 if len(key)<3 else (1 if cand in own else 0) if len(key)==3 else 1 if len(key)<=8 else 2
        if not budget or cand[:1]!=key[:1] or abs(len(cand)-len(key))>budget:continue
        dist=distance(key,cand)
        if dist<=budget:scored.append((dist,cand))
    return unique_best(scored)

def brand_match(key,brands):
    '''Brand names are distinctive, so they tolerate more: a few typos, or the same consonant sound.'''
    scored=[]
    for cand,brand in brands.items():
        if cand[:1]!=key[:1] or len(key)<4:continue
        budget=1 if len(key)<=5 else 2 if len(key)<=8 else 3
        dist=distance(key,cand)
        if dist<=budget:scored.append((dist,brand))
        elif len(key)>=8 and distance(skeleton(key),skeleton(cand))<=2 and difflib.SequenceMatcher(None,key,cand).ratio()>=0.7:scored.append((budget+1,brand))
    return unique_best(scored)

def match_case(original,word):
    if original.isupper() and len(original)>1:return word.upper()
    return word[:1].upper()+word[1:] if original[:1].isupper() else word

def tidy(text):
    text=unicodedata.normalize('NFKC',text).replace('’',"'").replace('‘',"'").replace('“','"').replace('”','"')
    text=' '.join(text.split())
    text=re.sub(r'\s+([,.!?;:])',r'\1',text)
    text=re.sub(r'([!?])\1+',r'\1',text)
    text=re.sub(r',(?=[A-Za-z])',', ',text)
    text=re.sub(r"(?<![\w'])i(?![\w'])",'I',text)
    if re.match(r'[a-z][a-z\s]',text):text=text[0].upper()+text[1:]
    return text

def name_slots(text):
    '''Start offsets of the (up to two) words that name a person after call/text/message/...'''
    slots=set()
    for verb in PERSON_VERB.finditer(text):
        for n,word in enumerate(WORD.finditer(text,verb.end())):
            if n==2 or text[verb.end():word.start()].strip(' ') and n==0:break
            slots.add(word.start())
            if n==0 and not word.group()[:1].isupper():break  # "call mom" names one word; "call Linda Rivera" two
    return slots

def clean_prompt(prompt,vocabulary=()):
    lex=lexicon();text=tidy(prompt);changes=[]
    if text!=' '.join(prompt.split()):changes.append({'kind':'format','from':prompt.strip(),'to':text})
    own={w.casefold() for phrase in vocabulary for w in WORD.findall(phrase)}
    known=lex['dictionary']|own
    real=lambda token:known_word(token.group(),known,lex['names'])
    candidates=lex['words']|own
    body=MESSAGE_BODY.search(text);limit=body.start() if body else len(text)
    blocked=[m.span() for pattern in PROTECTED for m in pattern.finditer(text)]
    tokens=[m for m in WORD.finditer(text) if m.end()<=limit and not any(a<m.end() and m.start()<b for a,b in blocked)]
    people=name_slots(text)
    edits=[];i=0
    while i<len(tokens):
        matched=False
        # Brand names first, up to three words ("tim hortan", "shopers drug mart"); never a person's name.
        for n in (3,2,1):
            window=tokens[i:i+n]
            if len(window)<n or any(t.start() in people for t in window):continue
            if any(text[a.end():b.start()]!=' ' for a,b in zip(window,window[1:])):continue
            original=text[window[0].start():window[-1].end()];key=letters(original)
            ordinary=all(real(t) for t in window)
            brand=lex['brands'].get(n,{}).get(key)
            # Fuzzy brand matching needs a non-word in the window: "burger now" is two real words.
            if brand is None and not ordinary:brand=brand_match(key,lex['brands'].get(n,{}))
            if brand is None:continue
            # A brand spelled with ordinary words ("best buy") is only re-cased when the driver capitalised it.
            if brand!=original and (not ordinary or any(t.group()[:1].isupper() for t in window)):
                edits.append((window[0].start(),window[-1].end(),brand,'name'))
            i+=n;matched=True;break
        if matched:continue
        token=tokens[i];word=token.group()
        if not real(token):
            # A person's name is corrected only towards the driver's own saved names.
            fixed=word_match(word.casefold(),own if token.start() in people else candidates,own)
            if fixed:edits.append((token.start(),token.end(),match_case(word,fixed),'spelling'))
        i+=1
    spelling=[]
    for start,end,replacement,kind in reversed(edits):
        spelling.insert(0,{'kind':kind,'from':text[start:end],'to':replacement})
        text=text[:start]+replacement+text[end:]
    return {'text':text,'original':prompt,'changes':changes+spelling,'lexicon_version':lex['version']}
