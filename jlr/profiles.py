"""Driver profiles: a guest with no private data, and password-protected profiles.

Each profile is one SQLite database encrypted at rest (format v2):

  password key = Argon2id(password, salt)                (memory-hard; settings stored in the header)
  data key     = 32 random bytes, created once per profile
  file         = header | AES-256-GCM(password key, data key) | AES-256-GCM(data key, database)

The header (format, Argon2id settings, salt) is authenticated by both layers, and
the database layer also authenticates the wrapped key, so no part can be swapped
or altered undetected. The password itself is never stored; a wrong password
fails to unwrap the data key. While a driver is signed in, only the data key and
the decrypted database are kept, in memory. Saves re-encrypt with the data key (no
key derivation); a password change re-wraps the data key only.

Older v1 files (scrypt, one key) open as before and are rewritten as v2 at once.

File layout: profiles/index.json (ids and display names only) and profiles/<id>.jlrp.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
import weakref
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.low_level import Type as Argon2Type, hash_secret_raw
from .context import EntityStore

MAGIC_V1,MAGIC=b'JLRP1',b'JLRP2'
KDF_ARGON2ID=1
# header: magic, KDF id, memory (KiB), passes, lanes, salt
HEADER=struct.Struct('>5sBIIB16s')
WRAPPED=12+32+16   # wrap nonce + data key + GCM tag
GUEST={'id':'guest','name':'Guest','protected':False}
MAX_PASSWORD=256
# Five wrong passwords lock a profile; each further lockout doubles, up to an hour.
MAX_FAILURES,LOCKOUT_SECONDS,MAX_LOCKOUT_SECONDS=5,60,3600
CREATIONS_PER_HOUR=5

def min_password():
    '''4 characters by default (a short PIN is allowed); JLR_MIN_PASSWORD raises it.'''
    return max(1,int(os.environ.get('JLR_MIN_PASSWORD','4')))

def check_password(password):
    if not isinstance(password,str) or not min_password()<=len(password)<=MAX_PASSWORD:
        raise ProfileError(f'Use a password of {min_password()}–{MAX_PASSWORD} characters.')

class ProfileError(ValueError):
    def __init__(self,message,code=400):super().__init__(message);self.code=code

def argon2_settings():
    '''(memory KiB, passes, lanes). Time-optimised default: 64 MiB, 3 passes, 4 lanes (about half a second).'''
    return (int(os.environ.get('JLR_ARGON2_MEMORY_KIB','65536')),int(os.environ.get('JLR_ARGON2_PASSES','3')),
            int(os.environ.get('JLR_ARGON2_LANES','4')))

def password_key(password,salt,memory,passes,lanes):
    # argon2-cffi (reference libargon2, portable wheels); standard Argon2id (RFC 9106) raw output.
    return hash_secret_raw(password.encode(),salt,time_cost=passes,memory_cost=memory,parallelism=lanes,hash_len=32,type=Argon2Type.ID)

def wrap(password,data_key,settings=None):
    '''The file prefix: authenticated header plus the data key wrapped by the password key.'''
    memory,passes,lanes=settings or argon2_settings();salt=os.urandom(16)
    header=HEADER.pack(MAGIC,KDF_ARGON2ID,memory,passes,lanes,salt)
    nonce=os.urandom(12)
    return header+nonce+AESGCM(password_key(password,salt,memory,passes,lanes)).encrypt(nonce,data_key,header)

def seal(prefix,data_key,data):
    '''v2 file: prefix | nonce | database, authenticated together with the prefix.'''
    nonce=os.urandom(12)
    return prefix+nonce+AESGCM(data_key).encrypt(nonce,data,prefix)

def unseal(blob,password):
    '''(database, data key, prefix, needs rewrite). Raises ProfileError 403 on a wrong password, 500 on damage.'''
    if blob.startswith(MAGIC_V1):return _unseal_v1(blob,password)
    if not blob.startswith(MAGIC) or len(blob)<HEADER.size+WRAPPED+12+16:raise ProfileError('Profile file is damaged',500)
    _,kdf,memory,passes,lanes,salt=HEADER.unpack(blob[:HEADER.size])
    if kdf!=KDF_ARGON2ID:raise ProfileError('Profile file is damaged',500)
    header=blob[:HEADER.size];nonce=blob[HEADER.size:HEADER.size+12];wrapped=blob[HEADER.size+12:HEADER.size+WRAPPED]
    try:data_key=AESGCM(password_key(password,salt,memory,passes,lanes)).decrypt(nonce,wrapped,header)
    except Exception:raise ProfileError('Incorrect password.',403) from None
    prefix=blob[:HEADER.size+WRAPPED];body=blob[len(prefix):]
    try:data=AESGCM(data_key).decrypt(body[:12],body[12:],prefix)
    except Exception:raise ProfileError('Profile file is damaged',500) from None
    current=argon2_settings()
    weaker=memory<current[0] or passes<current[1]
    return data,data_key,prefix,weaker

def _unseal_v1(blob,password):
    '''Format v1 (scrypt key, no data key). Opened once to migrate: a new data key and v2 prefix are made.'''
    if len(blob)<len(MAGIC_V1)+28:raise ProfileError('Profile file is damaged',500)
    salt=blob[len(MAGIC_V1):len(MAGIC_V1)+16];nonce=blob[len(MAGIC_V1)+16:len(MAGIC_V1)+28]
    key=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
    try:data=AESGCM(key).decrypt(nonce,blob[len(MAGIC_V1)+28:],MAGIC_V1)
    except Exception:raise ProfileError('Incorrect password.',403) from None
    data_key=os.urandom(32)
    return data,data_key,wrap(password,data_key),True

def write_private(path,blob):
    '''Atomic write through a private temporary file in the same folder (fsync, then replace).'''
    path=Path(path)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=path.stem+'.',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(blob);out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    except BaseException:
        try:os.unlink(tmp)
        except OSError:pass
        raise

class ProfileStore(EntityStore):
    '''A decrypted profile database in memory; each edit is re-encrypted to its file with the data key.'''
    def __init__(self,profile,connection,data_key,prefix,file):
        self.profile=profile;self.data_key=data_key;self.prefix=prefix;self.file=Path(file)
        super().__init__(connection=connection,seed=False)
    def saved(self):
        # One writer at a time, each through its own temporary file, so concurrent edits from two
        # sessions on this profile all reach the file and a crash never leaves it half-written.
        with self.lock:write_private(self.file,seal(self.prefix,self.data_key,self.shared.serialize()))

def guest_store():
    store=EntityStore(connection=sqlite3.connect(':memory:',check_same_thread=False),seed=False)
    store.profile=dict(GUEST);return store

class ProfileRegistry:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        # Sessions signed in to the same profile share one decrypted copy, so edits never diverge.
        self.lock=threading.RLock();self.failures={};self.open_stores=weakref.WeakValueDictionary();self.created=[]
        self.max_profiles=int(os.environ.get('JLR_MAX_PROFILES','20'))
    def _index(self):
        path=self.root/'index.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    def list(self):
        return [GUEST]+[{'id':pid,'name':item['name'],'protected':True} for pid,item in sorted(self._index().items(),key=lambda x:x[1]['name'].casefold())]
    def create(self,name,password,*,source=None,profile_id=None):
        '''Create a profile; `source` is an optional SQLite file whose data it starts with.'''
        name=' '.join(str(name or '').split())
        if not 1<=len(name)<=40:raise ProfileError('Enter a profile name of 1–40 characters.')
        check_password(password)
        with self.lock:
            index=self._index()
            if any(item['name'].casefold()==name.casefold() for item in index.values()) or name.casefold()=='guest':raise ProfileError('A profile with that name already exists.',409)
            if len(index)>=self.max_profiles:raise ProfileError(f'This server already has {self.max_profiles} profiles.',429)
            self.created=[when for when in self.created if time.time()-when<3600]
            if len(self.created)>=CREATIONS_PER_HOUR:raise ProfileError('Too many new profiles in the last hour. Try again later.',429)
            self.created.append(time.time())
            base=profile_id or re.sub(r'[^a-z0-9]+','-',name.casefold()).strip('-') or 'profile';pid=base;n=2
            while pid in index or pid=='guest':pid=f'{base}-{n}';n+=1
            db=sqlite3.connect(':memory:',check_same_thread=False)
            if source:
                src=sqlite3.connect(source)
                try:src.backup(db)
                finally:src.close()
            store=EntityStore(connection=db,seed=False)
            with store.connect() as conn:conn.execute('INSERT OR REPLACE INTO profile VALUES (?,?)',('driver',name))
            data_key=os.urandom(32)
            with store.lock:data=db.serialize()
            write_private(self.root/f'{pid}.jlrp',seal(wrap(password,data_key),data_key,data))
            index[pid]={'name':name,'created':int(time.time())}
            tmp=self.root/'index.tmp';tmp.write_text(json.dumps(index,indent=1),encoding='utf-8');os.replace(tmp,self.root/'index.json')
            store.close()
        return self.open(pid,password)
    def change_password(self,profile_id,old_password,new_password):
        '''Re-wrap the profile's data key under a new password (new salt); the database keeps its data key.'''
        check_password(new_password)
        store=self.open(profile_id,old_password)
        with self.lock,store.lock:
            # Signed-in sessions share this store, so their next save uses the new wrapping too.
            store.prefix=wrap(new_password,store.data_key);store.saved()
    def open(self,profile_id,password):
        if profile_id=='guest':return guest_store()
        with self.lock:
            index=self._index()
            if profile_id not in index:raise ProfileError('Unknown profile.',404)
            count,until,lockouts=self.failures.get(profile_id,(0,0,0))
            if until>time.time():raise ProfileError(f'Too many attempts. Try again in {int(until-time.time())+1} s.',429)
        if not isinstance(password,str) or not 1<=len(password)<=MAX_PASSWORD:raise ProfileError('Incorrect password.',403)
        file=self.root/f'{profile_id}.jlrp'
        try:data,data_key,prefix,rewrite=unseal(file.read_bytes(),password)
        except ProfileError as exc:
            if exc.code==403:
                with self.lock:
                    count,until,lockouts=self.failures.get(profile_id,(0,0,0));count+=1
                    if count>=MAX_FAILURES:
                        lockouts+=1;self.failures[profile_id]=(0,time.time()+min(LOCKOUT_SECONDS*2**(lockouts-1),MAX_LOCKOUT_SECONDS),lockouts)
                    else:self.failures[profile_id]=(count,until,lockouts)
            raise
        with self.lock:
            self.failures.pop(profile_id,None)
            store=self.open_stores.get(profile_id)
            if store is None:
                db=sqlite3.connect(':memory:',check_same_thread=False);db.deserialize(data)
                store=self.open_stores[profile_id]=ProfileStore({'id':profile_id,'name':index[profile_id]['name'],'protected':True},db,data_key,prefix,file)
                if rewrite:self._upgrade(store,password,data)
            return store
    def _upgrade(self,store,password,data):
        '''Rewrite a v1 file, or one with weaker Argon2id settings, as current v2; verified before it counts.'''
        if not store.prefix.startswith(MAGIC) or HEADER.unpack(store.prefix[:HEADER.size])[2:4]<argon2_settings()[:2]:
            store.prefix=wrap(password,store.data_key)
        with store.lock:write_private(store.file,seal(store.prefix,store.data_key,data))
        check,_,_,_=unseal(store.file.read_bytes(),password)
        if check!=data:raise ProfileError('Profile upgrade could not be verified',500)
