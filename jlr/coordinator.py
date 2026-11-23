"""V2 revision 2: one coordinator for routed, local-only simulated requests."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass,field
from .ollama import request_json
from .routing_pipeline import analyze_routing_only, get_routing_classifier
from .runtime import runner_options
from .worker_prompt import SYSTEM as WORKER_SYSTEM
from .contract import NotRequested,CAPABILITIES,VERSION,ContractError,worker_schema,normalize_wire,validate_worker,validate_intent,grounded_in,clarification_grounded,validate_answer_grounding,unsupported_action,task_text,informational_request,travel_question,EXPLICIT_ACTION
from .context import EntityStore,context_for,bind_argument,explicit_entity,one_off_contact
from .profiles import ProfileRegistry,guest_store
from .cleanup import clean_prompt
from .masking import PrivacyEnvelope,masking_mode
from .trip_options import trip_options
from . import receipt as privacy_receipt
from . import actions as driver_actions
from . import profile_saves
from . import route_order
from . import models
from .tools import ToolGateway,TOOL_SPECS,MAX_TOOL_CALLS,MAX_INFERENCE_CALLS,parse_coordinates
from .privacy_policy import assess as assess_sensitivity,decide as decide_route,outbound_blocklist

V2_SCHEMA='jlr-end-to-end-v2'
MAX_TURNS=20
MAX_CONTEXT_ROUNDS=2
V2Error=ContractError

@dataclass
class V2Session:
    session_id:str
    touched:float=field(default_factory=time.time)
    generation:int=0
    turns:list=field(default_factory=list)
    references:dict=field(default_factory=dict)
    clarified_references:dict=field(default_factory=dict)
    state:dict=field(default_factory=lambda:{'playback':None,'destination':None,'outbox':[],'active_call':None,'last_draft':None})
    pending:dict|None=None
    request_cache:dict=field(default_factory=dict)
    lock:object=field(default_factory=threading.RLock,repr=False)
    # The signed-in driver's private database; every lookup for this session uses it only.
    store:object=field(default=None,repr=False)
    profile:dict=field(default_factory=lambda:{'id':'default','name':'Sample data','protected':False})

class V2Sessions:
    '''Sessions start on the default store: the guest (no private data) when profiles are
    configured, otherwise the single sample database.'''
    def __init__(self,store=None,profiles=None):
        self.profiles=profiles
        if profiles is not None and store is None:store=guest_store()
        self.store=store if isinstance(store,EntityStore) else EntityStore(getattr(store,'path',None) or os.environ.get('JLR_V2_DB'))
        self.items={};self.lock=threading.RLock()
    def create(self):
        with self.lock:
            self.items={k:v for k,v in self.items.items() if time.time()-v.touched<1800}
            if len(self.items)>=256:raise V2Error('Session capacity reached')
            s=V2Session(uuid.uuid4().hex,store=self.store)
            if getattr(self.store,'profile',None):s.profile=dict(self.store.profile)
            self.items[s.session_id]=s;return s
    def switch(self,sid,store):
        '''Bind a session to another driver's store. Everything remembered in the session
        belongs to the previous driver, so it is cleared first.'''
        s=self.reset(sid)
        with s.lock:s.store=store;s.profile=dict(store.profile)
        return s
    def get(self,sid):
        with self.lock:
            s=self.items.get(sid)
            if not s or time.time()-s.touched>1800:
                self.items.pop(sid,None);raise V2Error('Session expired or does not exist')
            s.touched=time.time();return s
    def reset(self,sid):
        s=self.get(sid)
        with s.lock:
            s.generation+=1;s.pending=None;s.turns.clear();s.references.clear();s.clarified_references.clear();s.request_cache.clear()
            s.state=V2Session(sid).state
        return s

class ModelWorker:
    def __init__(self,model='qwen3:4b-instruct-2507-q4_K_M',*,transport=request_json,deadline_seconds=90,role=None):
        # role: a jlr.models role (endpoint, model, options, timeout); explicit arguments are for tests and older callers.
        if role is not None:model,transport,deadline_seconds,self.provider=role.model,role.transport,role.timeout,role.endpoint.provider
        self.role=role
        self.model=model;self.transport=transport;self.deadline_seconds=deadline_seconds;self.last_timing={};self.last_output=None
    def plan(self,prompt,context,*,sanitized_prompt=None,deadline_seconds=None):
        self.last_output=None
        text=sanitized_prompt if sanitized_prompt is not None else prompt
        reply=self.transport('/api/generate',{'model':self.model,'system':WORKER_SYSTEM,
            'prompt':json.dumps({'driver_request':text,'context':context},ensure_ascii=False),
            'format':worker_schema(),'stream':False,'keep_alive':-1,
            'options':self.role.options(512) if self.role is not None else runner_options(self.model,512)},timeout=min(self.deadline_seconds,deadline_seconds) if deadline_seconds else self.deadline_seconds)
        self.last_timing={k:v for k,v in reply.items() if k.endswith('_duration') or k.endswith('_count')}
        if reply.get('done_reason')=='length':raise V2Error('Worker output truncated')
        try:value=json.loads(reply['response'])
        except (ValueError,KeyError,TypeError) as exc:raise V2Error('Invalid worker JSON') from exc
        self.last_output=value
        return normalize_wire(value)

UNSUPPORTED_REPLY=("I can't do that in this prototype. I can play music, navigate (including stops on the way), "
                   "find nearby places, make calls and send messages, all simulated, remember your usual music and drink, "
                   "contacts and places, and answer general questions.")

def private_coordinates(entity):
    value=entity.get('value')
    if isinstance(value,dict) and value.get('latitude') is not None and value.get('longitude') is not None:
        return float(value['latitude']),float(value['longitude'])
    return parse_coordinates(entity.get('label')) or parse_coordinates(value)

class V2Adapters:
    def __init__(self,store=None,tools=None):self.store=store;self.tools=tools
    def _locate(self,text,request,use_selected=False):
        '''Resolve a destination or stop to a mapped point, or None if it is not a map place.

        Saved private places use stored coordinates only. "nearest/nearby/closest X"
        is decided from the argument itself (or the words right before it in the
        request), never from elsewhere in the prompt, so a stop cannot hijack the
        destination.
        '''
        request=request if request is not None else {}
        private=(request.get('private_destinations') or {}).get(text)
        if private:
            coords=private_coordinates(private)
            if not coords:raise V2Error(f'{text} has no stored coordinates. Private addresses are never sent to external map services; reply with coordinates such as 42.3149, -83.0364 or save them for this place.')
            value=private.get('value') if isinstance(private.get('value'),dict) else {}
            return {'id':f'coords:{coords[0]},{coords[1]}','name':'your saved location' if parse_coordinates(text) else text,
                    'address':value.get('address') or f'{coords[0]}, {coords[1]}','latitude':coords[0],'longitude':coords[1],'private':True}
        if use_selected and request.get('selected_place'):return request['selected_place']
        name=' '.join(re.sub(r'\b(?:the|my|a|an|nearest|nearby|closest)\b',' ',text,flags=re.I).split()).strip(' .?')
        relative=re.search(r'\b(?:nearest|nearby|closest)\b',text,re.I) or (name and re.search(
            r'\b(?:nearest|nearby|closest)\s+(?:\w+\s+){0,2}?'+re.escape(name)+r'\b',request.get('prompt',''),re.I))
        if relative and name:
            found=self.tools.execute({'tool':'map.find_places','arguments':{'query':name}})
            if not found.get('nearest'):raise V2Error(f'No mapped place matching {name} was found within 30 km')
            request['map_search']=found
            if use_selected:request['selected_place']=found['nearest']
            return found['nearest']
        if ',' in text or re.search(r'\d',text):
            resolved=self.tools.execute({'tool':'map.resolve_address','arguments':{'address':text}})
            if resolved.get('status')!='resolved':raise V2Error(f'The address {text} is missing or ambiguous')
            request['address_resolution']=resolved
            return resolved['place']
        return None
    def execute(self,cap,args,session,request=None):
        s=session.state;out={'status':'completed','simulation':True}
        if cap=='media.play':s['playback']=args['query'];out.update(query=args['query'],message='Playing '+args['query']+'.')
        elif cap=='media.pause':s['playback']=None;out['message']='Music paused.'
        elif cap=='media.next_track':out['message']='Skipped to the next track.'
        elif cap=='navigation.start':
            destination=args['destination'];route=None
            point=self._locate(destination,request,use_selected=True) if self.tools else None
            if point:
                with_shape=getattr(self.tools,'route_with_shape',None)
                route=with_shape(point['id']) if with_shape else self.tools.execute({'tool':'map.route','arguments':{'destination_id':point['id']}})
                if point.get('private'):route['destination']=dict(route['destination'],name=point['name'],address=point['address'])
                if request is not None:request['calculated_route']=route
                destination=route['destination'].get('name') or route['destination'].get('address') or destination
            s['destination']=destination;s['stops']=[]
            s['destination_point']={k:route['destination'][k] for k in ('name','address','latitude','longitude')} if route else None
            if route:
                preview={'contract':'Sample infotainment API—not a JLR integration.','method':'POST','path':'/api/v1/navigation/routes',
                    'headers':{'Content-Type':'application/json','Idempotency-Key':(request or {}).get('request_id','sample-request')},
                    'body':{'request_id':(request or {}).get('request_id','sample-request'),'destination':{
                        'label':destination,'address':route['destination']['address'],'latitude':route['destination']['latitude'],'longitude':route['destination']['longitude']},'stops':[],'start_guidance':True}}
                navigation={'origin':route['origin'],'destination':route['destination'],'stops':[],'distance_m':route['distance_m'],
                    'estimated_duration_seconds':route['duration_seconds'],'data_source':route['dataset']}
                receipt={'status':'guidance_started','simulation':True,'destination':destination}
                out.update(destination=destination,navigation=navigation,api_preview=preview,receipt=receipt,route_shape=route.get('shape'),
                           message='Simulation: navigation set to '+destination+' at '+route['destination']['address']+'.')
            else:out.update(destination=destination,message='Navigation started to '+destination+'.')
            # "without tolls", "avoid highways": accepted as route preferences, which this simulation cannot apply.
            option=re.search(r'\b(?:without|avoid(?:ing)?)\s+(?:the\s+)?(tolls?|highways?|motorways?|ferries|traffic|construction)\b',(request or {}).get('prompt',''),re.I)
            if option:out['route_options_ignored']=option.group();out['message']+=f' Route option "{option.group()}" is not applied in this simulation.'
        elif cap=='navigation.add_stop':
            # Stops are inserted before the current destination, in the order requested.
            final=s.get('destination_point')
            if not self.tools or not final:raise V2Error('Start navigation to a mapped destination before adding a stop.')
            point=self._locate(args['stop'],request)
            if not point:raise V2Error(f"Could not find a mapped place for {args['stop']}.")
            stop={k:point.get(k) for k in ('name','address','latitude','longitude')};stop['name']=stop['name'] or args['stop']
            stops=[*s.get('stops',[]),stop]
            try:route=self.tools.route_via([*stops,final],shape=True)
            except TypeError:route=self.tools.route_via([*stops,final])
            s['stops']=stops
            preview={'contract':'Sample infotainment API—not a JLR integration.','method':'POST','path':'/api/v1/navigation/routes',
                'headers':{'Content-Type':'application/json','Idempotency-Key':(request or {}).get('request_id','sample-request')},
                'body':{'request_id':(request or {}).get('request_id','sample-request'),
                    'destination':{'label':final['name'],'address':final['address'],'latitude':final['latitude'],'longitude':final['longitude']},
                    'stops':[{'label':x['name'],'address':x['address'],'latitude':x['latitude'],'longitude':x['longitude']} for x in stops],'start_guidance':True}}
            navigation={'origin':route['origin'],'destination':final,'stops':stops,'distance_m':route['distance_m'],
                'estimated_duration_seconds':route['duration_seconds'],'data_source':route['dataset']}
            path_text=' → '.join(['Start',*[x['name'] for x in stops],final['name']])
            out.update(destination=final['name'],stops=[x['name'] for x in stops],navigation=navigation,api_preview=preview,route_shape=route.get('shape'),
                       receipt={'status':'route_updated','simulation':True,'destination':final['name'],'stops':[x['name'] for x in stops]},
                       message=f"Simulation: added a stop at {stop['name']} ({stop['address']}). Route: {path_text}, {route['distance_m']/1000:.1f} km.")
        elif cap=='navigation.get_destination':out.update(destination=s['destination'],message='Current destination: '+s['destination']+'.' if s['destination'] else 'No destination is set.')
        elif cap=='phone.call':s['active_call']=args['contact'];out.update(contact=args['contact'],message='Simulated call started with '+args['contact']+'.')
        elif cap=='phone.end_call':s['active_call']=None;out['message']='Ended the simulated call.'
        elif cap=='messaging.send':s['outbox'].append(dict(args));out.update(contact=args['contact'],text=args['message'],message='Simulated message sent to '+args['contact']+': '+args['message'])
        elif cap=='calendar.next_appointment':out.update(appointment={'label':'Synthetic appointment','time':'tomorrow 14:00','location':'Ottawa'},message='Your sample appointment is tomorrow at 14:00 in Ottawa.')
        elif cap=='vehicle.get_trip_state':out.update(battery_percent=35,message='Your sample battery level is 35%.')
        elif cap=='messages.latest':
            store=session.store or self.store
            matches=store.find(args['contact'],'person')
            msg=store.latest_message(matches[0]) if matches else None
            out.update(message=msg['body'] if msg else 'No saved message from this contact.',received_message=msg)
        else:raise V2Error('Unsupported adapter')
        return out

SECOND_PERSON={'my':'your','our':'your','me':'you','mine':'yours','myself':'yourself','i':'you'}

def addressed(phrase):
    '''The driver's words as the system says them back: "my usual songs" -> "your usual songs".'''
    return re.sub(r"\b(?:my|our|me|mine|myself|i)\b",lambda m:SECOND_PERSON[m.group().casefold()],phrase,flags=re.I)

def be(phrase):
    '''"are" for a plural noun phrase ("your usual songs"), "is" otherwise; names ("Chris") stay singular.'''
    last=(phrase.split() or [''])[-1].casefold()
    plural=re.match(r'(?:my|our|your|the|these|those)\s',phrase,re.I) and last.endswith('s') and not last.endswith(('ss','us','is'))
    return 'are' if plural else 'is'

def slot_question(reference,kind,candidates=None,no_contacts=False):
    '''The system's own clarification questions, in plain words (never internal type names).'''
    ref=addressed((reference or '').strip())
    if candidates:
        noun=re.sub(r'^(?:your|the)\s+','',ref,flags=re.I)
        return f'Which {noun or "one"} do you mean: '+', '.join(c['label'] for c in candidates)+'?'
    if kind=='person':
        ask=f'Who {be(ref)} {ref}?' if ref else 'Who should I contact?'
        return ('There are no saved contacts in this profile. '+ask+' Type their name or number.') if no_contacts else ask+' Type a saved contact, a name or a number.'
    if kind=='location':return f'Where {be(ref)} {ref}?' if ref else 'Where to?'
    if kind=='preference':return f'What {be(ref)} {ref}?'
    if kind=='session_reference':return f'What does "{ref}" refer to?'
    if kind=='text':return 'What should the message say?'
    return f'Could you tell me more about "{ref}"?' if ref else 'Could you say that differently?'

class MissingSlot(V2Error):
    def __init__(self,reference,kind,question=None,candidates=None,no_contacts=False):
        self.slot={'reference':reference,'type':kind,'question':question or slot_question(reference,kind,candidates,no_contacts),'candidates':candidates or []}
        super().__init__(self.slot['question'])

class V2Coordinator:
    def __init__(self,sessions=None,router=None,adapters=None,local_worker=None,larger_worker=None,**unused):
        self.sessions=sessions or V2Sessions()
        # Endpoints and models come from jlr.models (models.toml, or the .env settings). The router and the local
        # worker must be on an on_vehicle endpoint (they see unmasked text); the larger model only ever gets the masked payload.
        roles=models.config().roles if not (router and local_worker and larger_worker) else {}
        self.router=router or get_routing_classifier(os.environ.get('JLR_ROUTING_PROFILE',''),deadline_seconds=roles['router'].timeout,role=roles['router'])
        self.local_worker=local_worker or ModelWorker(role=roles['local'])
        self.larger_worker=larger_worker or ModelWorker(role=roles['larger'])
        self.larger_worker.provider=getattr(self.larger_worker,'provider',roles['larger'].endpoint.provider if roles else 'ollama')
        self.tools=unused.get('tools')
        if self.tools is None and os.environ.get('JLR_INTERNET_TOOLS')=='1':self.tools=ToolGateway()
        self.adapters=adapters or V2Adapters(self.sessions.store,self.tools)
    def event(self,t,id,status,input=None,output=None,ms=None):
        e={'id':id,'label':id.replace('_',' ').title(),'status':status,'input':input or {},'output':output or {}}
        if ms is not None:e['duration_ms']=round(ms,3)
        t['result']['trace']=[x for x in t['result']['trace'] if x['id']!=id]+[e]
    def run(self,sid,prompt,*,idempotency_key=None,cancel_event=None):
        s=self.sessions.get(sid)
        with s.lock:
            if idempotency_key in s.request_cache:
                old=s.request_cache[idempotency_key]
                if old['prompt']!=prompt:raise V2Error('Idempotency key conflict')
                return copy.deepcopy(old['result'])
            if s.pending:raise V2Error('A request is pending; answer, confirm or cancel it first')
            t={'prompt':prompt,'request_id':uuid.uuid4().hex,'generation':s.generation,'bindings':{},'round':0,
               'started':time.perf_counter(),'step_started':time.perf_counter(),'idempotency_key':idempotency_key,'cancel_event':cancel_event}
            t['result']={'schema_version':V2_SCHEMA,'contract_revision':2,'request_id':t['request_id'],'session_id':sid,
                'prompt':prompt,'simulation':True,'cloud_authorized':False,'execution_ready':False,'downstream_invoked':False,
                'routing_invoked':False,'model_invoked':False,'privacy_invoked':False,'trace':[]}
            self.event(t,'driver_prompt','complete',{'prompt':prompt})
            # Everything after this point (router, context, models, grounding checks) reads the cleaned prompt.
            phase=time.perf_counter();cleaned=clean_prompt(prompt,s.store.vocabulary())
            t.update(prompt=cleaned['text'],original_prompt=prompt)
            t['result'].update(cleaned_prompt=cleaned['text'],prompt_cleanup={'changes':cleaned['changes'],'lexicon_version':cleaned['lexicon_version']})
            self.event(t,'prompt_cleanup','complete',{'prompt':prompt},{'text':cleaned['text'],'changes':cleaned['changes']},(time.perf_counter()-phase)*1000)
            prompt=cleaned['text']
            s.pending=t
        try:
            if unsupported_action(prompt):return self.finish(s,t,'unsupported',answer='This prototype does not support purchases, payments, locks or climate actions.')
            route=analyze_routing_only(prompt,self.router,local_only=os.environ.get('JLR_LOCAL_ONLY')=='1' or bool(re.search(r'\bkeep (?:this|it) local\b',prompt,re.I)))
            t['route']=route;t['result'].update(routing=route,routing_invoked=True,next_route=route.get('next_route'),reason=route.get('reason'))
            t['result']['trace'].extend(route.get('trace',[]))
            self.event(t,'routing_policy','complete',output={'next_route':route.get('next_route')})
            if route.get('routing_status')=='error' or not route.get('next_route'):return self.finish(s,t,'failed',error=route.get('reason','Routing failed'))
            return self.resume(s,t)
        except Exception as exc:return self.finish(s,t,'failed',error=str(exc))
    def cancelled(self,s,t):return s.generation!=t['generation'] or (t.get('cancel_event') is not None and t['cancel_event'].is_set()) or t.get('cancelled',False)
    def resume(self,s,t):
        if self.cancelled(s,t):return self.finish(s,t,'cancelled')
        prompt=t['prompt'];r=t['result'];phase=time.perf_counter()
        ctx=context_for(prompt,t['route'].get('references',[]),s,s.store,t['bindings']);t['context']=ctx
        r['context']={k:v for k,v in ctx.items() if k in {'status','findings','missing'}}
        self.event(t,'context_resolver',ctx['status'],{'references':t['route'].get('references',[])},r['context'],(time.perf_counter()-phase)*1000)
        db=[f for f in ctx['findings'] if f['source']=='private_db' and f.get('status')=='resolved']
        self.event(t,'private_database','complete' if db else 'not_reached',output={'lookups':db})
        self.event(t,'session_store','complete',output={'generation':s.generation,'references':list(s.references)})
        self.event(t,'context_gate',ctx['status'],output={'allowed':not ctx['missing']})
        if ctx['missing']:
            f=ctx['missing'][0]
            no_contacts=f['type']=='person' and not any(e['type']=='person' for e in s.store.entities())
            return self.ask(s,t,MissingSlot(f['text'],f['type'],candidates=f.get('candidates'),no_contacts=no_contacts))
        # Final route = router complexity x privacy sensitivity, decided once context is known.
        sensitivity=assess_sensitivity(task_text(prompt),t['route'].get('references',[]),ctx['findings'])
        decision=decide_route(t['route'],sensitivity);t['sensitivity']=sensitivity;t['decision']=decision
        def shown(item):
            if item['tier']!='P3':return item
            digits=re.sub(r'\D','',item['text'])
            return dict(item,text=f'•••• {digits[-4:]}' if item['category']=='payment_card' else f'[hidden {item["category"].replace("_"," ")}]')
        r['privacy_routing']={'tier':sensitivity['tier'],'tier_name':sensitivity['tier_name'],'policy_version':sensitivity['policy_version'],
            'items':[shown(i) for i in sensitivity['items']],'decision':{k:decision[k] for k in ('complexity','router_route','next_route','reason_code','reason')}}
        r.update(next_route=decision['next_route'],reason=decision['reason'])
        if decision['notice']:r['routing_notice']=decision['notice']
        self.event(t,'privacy_policy','complete',input={'complexity':decision['complexity'],'router_route':decision['router_route']},
            output={'tier':sensitivity['tier'],'items':r['privacy_routing']['items'],'next_route':decision['next_route'],'reason':decision['reason']})
        try:
            value=t.get('proposal')
            if value is None:
                route=decision['next_route'];worker=self.larger_worker if route=='privacy_layer' else self.local_worker
                # The worker sees what each reference means (type + label), never privacy flags or stored
                # records: privacy is enforced by the system, so the model must not reason about it.
                projection={'entities':{key:{'type':e.get('type'),'label':e.get('label')} for key,e in ctx['entities'].items()},
                            'bindings':t['bindings'],'session_state':ctx['session_state'],'simulation':True}
                envelope=None
                if route=='privacy_layer':
                    envelope=PrivacyEnvelope();safe,projection=envelope.prepare(task_text(prompt),ctx)
                else:safe=task_text(prompt)
                # Facts the task needs, computed here from private data; only derived values reach the model.
                facts=self.task_facts(s,prompt,ctx,envelope)
                if facts:
                    projection['facts']=facts;r['task_facts']=facts
                    if envelope:envelope.check_outgoing(json.dumps(facts)+' '+json.dumps(envelope.legend))
                    self.event(t,'task_facts','complete',output=facts)
                if envelope:
                    if envelope.legend:projection['placeholders']=envelope.legend
                    # Show which typed words became which placeholder. Saved values that were masked
                    # stay hidden; only their count is reported.
                    order=lambda token:int(re.sub(r'\D','',token) or 0)
                    masked=[{'placeholder':token,'original':envelope.evidence_tokens[token]} for token in sorted(envelope.evidence_tokens,key=order)]
                    r['privacy_invoked']=True;r['privacy']={'sanitized_prompt':safe,'token_count':len(envelope.tokens),
                        'masked_words':masked,'hidden_saved_values':len(envelope.tokens)-len(masked),
                        'masking_mode':envelope.mode,'placeholders':dict(envelope.legend)}
                    self.event(t,'privacy_processing','complete',output=r['privacy'])
                stage='larger_worker' if route=='privacy_layer' else 'local_worker'
                self.event(t,stage,'running');phase=time.perf_counter();r['model_invoked']=True
                r['worker']={'provider':getattr(worker,'provider','ollama'),'model':worker.model,'sanitized':envelope is not None}
                projection['available_tools']=list(TOOL_SPECS if self.tools else {})
                projection['tool_results']=[];tool_calls=0;inference_calls=0
                # The budget covers this run of the model loop only: time the driver spends answering
                # a clarification or confirming is not counted against it.
                budget=float(os.environ.get('JLR_V2_REQUEST_DEADLINE','90'));loop_started=time.perf_counter()
                while True:
                    if time.perf_counter()-loop_started>budget:raise V2Error(f'Request exceeded the {budget:g}-second deadline')
                    if inference_calls>=MAX_INFERENCE_CALLS:raise V2Error('Worker exceeded the three-inference limit')
                    inference_calls+=1
                    # No raw prompt is supplied to the stronger-worker interface.
                    remaining=budget-(time.perf_counter()-loop_started)
                    if remaining<=0:raise V2Error(f'Request exceeded the {budget:g}-second deadline')
                    if isinstance(worker,ModelWorker):value=validate_worker(worker.plan(safe,projection,deadline_seconds=remaining))
                    else:value=validate_worker(worker.plan(safe,projection))
                    if self.cancelled(s,t):return self.finish(s,t,'cancelled')
                    if value['kind']=='clarification' and not clarification_grounded(value,safe):
                        # The model asked for information the request already gives. Correct it once
                        # (within the inference budget) rather than passing the question to the driver.
                        r.setdefault('worker_corrections',[]).append({'rejected':copy.deepcopy(value),'reason':'clarification_not_grounded'})
                        projection['feedback']=('Your previous reply asked a clarification, but the request already supplies that '
                                                'information. Do not ask. Reply with kind=answer using the information given, '
                                                'or kind=plan if an approved action was requested.')
                        continue
                    if (value['kind']=='unsupported' and informational_request(safe)
                            and not any(c['reason']=='unsupported_for_question' for c in r.get('worker_corrections',[]))):
                        # "Unsupported" is for actions the catalogue lacks. A question, comparison or
                        # recommendation always gets an answer, even without live data. Correct it once.
                        r.setdefault('worker_corrections',[]).append({'rejected':copy.deepcopy(value),'reason':'unsupported_for_question'})
                        projection['feedback']=('Your previous reply was kind=unsupported, but the request asks a question or for a '
                                                'comparison or recommendation, not an action. Reply with kind=answer: answer what you '
                                                'can and, where data is missing, say which data is needed and how to compare it.')
                        continue
                    # Actions, or reads the request never asked about ("No destination is set" for a
                    # travel-time comparison), do not answer a question.
                    if (value['kind']=='plan' and travel_question(safe) and not EXPLICIT_ACTION.search(safe)
                            and any(CAPABILITIES[x['capability']][1]!='read' or not re.search(CAPABILITIES[x['capability']][2],safe,re.I)
                                    for x in value['steps'])
                            and not any(c['reason']=='plan_for_question' for c in r.get('worker_corrections',[]))):
                        # A comparison or plan request asks for advice, not for actions to run (the intent
                        # rules would refuse them). Ask once for the answer instead of failing the request.
                        r.setdefault('worker_corrections',[]).append({'rejected':copy.deepcopy(value),'reason':'plan_for_question'})
                        projection['feedback']=('Your previous reply proposed actions, but the request asks for a comparison, plan or '
                                                'recommendation, not for actions to be carried out. Do not propose actions. Reply with '
                                                'kind=answer: give the comparison or recommendation in words.')
                        continue
                    invented=sorted(set(re.findall(r'<PRIVATE_[^>]*>',json.dumps(value)))-set(envelope.tokens)) if envelope else []
                    if invented and not any(c['reason']=='unknown_placeholder' for c in r.get('worker_corrections',[])):
                        # The model made up a placeholder (<PRIVATE_9>, <PRIVATE_home>) that cannot be restored.
                        # Ask once for a reply that uses only the given ones, instead of failing the request.
                        r.setdefault('worker_corrections',[]).append({'rejected':copy.deepcopy(value),'reason':'unknown_placeholder'})
                        projection['feedback']=('Your previous reply used placeholders that do not exist ('+', '.join(invented)+'). Use only '
                                                'the placeholders listed in context.placeholders, exactly as written, or plain words.')
                        continue
                    if value['kind']!='tool_request':break
                    if not self.tools:raise V2Error('Read-only tools are unavailable')
                    if tool_calls>=MAX_TOOL_CALLS:raise V2Error('Worker exceeded the four-tool limit')
                    request=copy.deepcopy(value['request'])
                    if envelope and request['tool'].startswith('map.'):
                        request=envelope.restore(request)
                    if request['tool'].startswith('map.'):
                        outbound=json.dumps(request['arguments'],ensure_ascii=False).casefold()
                        # Include every text field of saved rows (address, label), not only string values.
                        private_values={str(field).casefold() for entity in ctx.get('entities',{}).values() if entity.get('private')
                                        for field in (entity.get('label'),*(entity['value'].values() if isinstance(entity.get('value'),dict) else (entity.get('value'),)))
                                        if isinstance(field,str) and len(field.strip())>=3}
                        # Restricted and unmaskable sensitive content never goes to external tools either.
                        private_values|=outbound_blocklist(t.get('sensitivity',{}))
                        if any(value in outbound for value in private_values):raise V2Error('Private context cannot be sent to map tools')
                    tool_calls+=1;tool_phase=time.perf_counter()
                    result=self.tools.execute(request)
                    projection['tool_results'].append({'request':request,'result':result,'untrusted':True})
                    r.setdefault('tool_activity',[]).append({'request':request,'result':result})
                    if request['tool']=='map.find_places' and result.get('nearest'):t['selected_place']=result['nearest']
                    self.event(t,'tool_gateway','complete',input=request,output=result,ms=(time.perf_counter()-tool_phase)*1000)
                    self.event(t,request['tool'].replace('.','_'),'complete',input=request['arguments'],output=result)
                self.event(t,stage,'complete',output={'kind':value['kind'],'proposal':copy.deepcopy(value),'model':worker.model,'model_timing':getattr(worker,'last_timing',{})},ms=(time.perf_counter()-phase)*1000)
                if envelope:
                    value=envelope.restore(value);validate_worker(value)
                    self.event(t,'placeholder_restore','complete',output={'restored':True})
                t['proposal']=value
            # Refusals use system wording; the model's reason stays in the trace for inspection only.
            if value['kind']=='unsupported':return self.finish(s,t,'unsupported',answer=UNSUPPORTED_REPLY,unsupported_detail=value['reason'])
            if value['kind']=='clarification':
                t.pop('proposal',None);return self.ask(s,t,MissingSlot(value['reference'],value['slot'],value['question']))
            if value['kind']=='answer':
                validate_answer_grounding(prompt)
                self.event(t,'plan_validation','complete',output={'kind':'answer'})
                s.state['last_draft']=value['answer'] if re.search(r'\b(?:draft|write)\b',prompt,re.I) else s.state.get('last_draft')
                return self.finish(s,t,'completed',answer=value['answer'])
            steps=self.bind_plan(t,ctx,s.store)
            if t['unsaved_contacts']:r['unsaved_contacts']=sorted(set(t['unsaved_contacts']))
            if t.get('dropped_steps'):
                r['dropped_steps']=t['dropped_steps'];self.event(t,'plan_validation','complete',output={'left_out_not_requested':t['dropped_steps']})
            comparison=None
            ordered=self.fastest_order(t,steps)
            if ordered:
                steps,comparison=ordered;self.event(t,'route_order','complete',output=comparison)
            # Saves are checked now, before any confirmation card: the exact record joins the plan,
            # so the plan hash the driver approves covers what will be written.
            for step in steps:
                if step['capability'] in profile_saves.SAVE_CAPABILITIES:
                    step['save']=profile_saves.prepare(step['capability'],step['arguments'],s,t['prompt'])
            plan={'schema_version':'plan-v2','contract_revision':2,'steps':steps}
            if comparison:plan['route_order']=comparison
            # Resolve read dependencies before hashing/approval; never approve a
            # message containing an unevaluated expression.
            read_outputs={}
            for i,step in enumerate(steps):
                for key,value in list(step['arguments'].items()):
                    dep=re.fullmatch(r'\$step\[(\d+)\]\.([a-z_]+)',value)
                    if dep:
                        target=int(dep[1]);parent=steps[target]
                        if target not in read_outputs:
                            with s.lock:
                                if self.cancelled(s,t):return self.finish(s,t,'cancelled')
                                # Read adapters may retain demo search results; do not
                                # mutate the live session while preparing approval.
                                snapshot=V2Session(s.session_id,state=copy.deepcopy(s.state),store=s.store)
                                read_outputs[target]=self.adapters.execute(parent['capability'],parent['arguments'],snapshot)
                        resolved=read_outputs[target].get(dep[2])
                        if not isinstance(resolved,str) or not resolved:raise V2Error('Dependency result unavailable before approval')
                        step['arguments'][key]=resolved
            t['read_outputs']=read_outputs
            plan['hash']=hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k!='hash'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            r['plan']=plan;self.event(t,'capability_registry','complete',output={'version':VERSION})
            self.event(t,'plan_validation','complete',output=plan)
            self.event(t,'action_gate','complete',output={'action_requested':any(CAPABILITIES[x['capability']][1]!='read' for x in steps)})
            unverified=[u for u in t.get('unverified_steps',[]) if CAPABILITIES[u['capability']][1]!='read']
            if t.get('unverified_steps'):r['interpretation_check']=t['unverified_steps']
            if any(CAPABILITIES[x['capability']][1] in ('confirm','save') for x in steps) or unverified or comparison:
                # The card leads with what most needs checking; every step is listed either way.
                reason=('saves_private_data' if any(x.get('save') for x in steps) else 'fastest_order' if comparison
                        else 'interpretation_check' if unverified else 'capability_requires_confirmation')
                self.event(t,'confirmation','awaiting',output={'required':True,'reason':reason,'unverified_steps':unverified})
                return self.finish(s,t,'awaiting_confirmation',confirmation_required=True,confirmation_reason=reason,plan_hash=plan['hash'])
            return self.execute(s,t)
        except MissingSlot as exc:return self.ask(s,t,exc)
        except Exception as exc:
            for event in r['trace']:
                if event['status']=='running':event.update(status='failed',output={'error':str(exc),'worker_output':getattr(locals().get('worker'),'last_output',None)})
            self.event(t,'plan_validation','failed',output={'error':str(exc)})
            return self.finish(s,t,'failed',error=str(exc))
    def task_facts(self,s,prompt,ctx,envelope):
        '''Driving times between the saved places a comparison or plan mentions (data minimisation).

        Computed on the server from stored coordinates (OSRM receives coordinates only, as for
        any route). The model gets rounded minutes between placeholders, never the places.
        A person's place ("mom's house") counts when the person is mentioned ("visit mom").
        For order and fit questions the visiting options and their totals are computed here
        too (trip_options), so the model explains a correct result instead of adding up legs.
        '''
        if masking_mode()!='roles_facts' or not hasattr(self.tools,'travel_matrix') or not travel_question(prompt):return None
        places={}
        for e in ctx['entities'].values():
            if e.get('type')=='location' and isinstance(e.get('value'),dict):
                coords=private_coordinates(e)
                if coords:places.setdefault(e['label'],(e,coords,[]))
            elif e.get('type')=='person' and isinstance(e.get('value'),dict) and e['value'].get('alias'):
                for suffix in ("'s house","'s place","'s home"):
                    for home in s.store.find(e['value']['alias']+suffix,'location'):
                        coords=private_coordinates(home)
                        # The person's own names mention their place too ("visit mom").
                        if coords:places.setdefault(home['label'],(home,coords,[*e.get('aliases',[]),e['label']]))
        if not places:return None
        origin=self.tools.maps.origin()
        points=[('START',None,(origin['latitude'],origin['longitude']),[])]+[(label,e,c,people) for label,(e,c,people) in places.items()][:6]
        try:matrix=self.tools.travel_matrix([{'latitude':c[0],'longitude':c[1]} for _,_,c,_ in points])
        except Exception:return None
        names=[label if label=='START' or not envelope else envelope.register(label,'location',[*e.get('aliases',[]),label])
               for label,e,_,_ in points]
        trips=[];minutes={}
        for i,a in enumerate(names):
            for j,b in enumerate(names):
                duration=matrix['durations'][i][j] if i!=j else None
                if duration is None:continue
                distance=matrix['distances'][i][j];minutes[(a,b)]=max(5,5*round(duration/300))
                trips.append({'from':a,'to':b,'minutes':minutes[(a,b)],'km':round(distance/1000) if distance is not None else None})
        if not trips:return None
        facts={'driving_times':trips,'note':'Computed on the vehicle from saved places, rounded to 5 minutes. START is the current starting point.'}
        mentioned={name:[*e.get('aliases',[]),e['label'],*people] for name,(label,e,_,people) in zip(names,points) if label!='START'}
        options=trip_options(prompt,mentioned,minutes)
        if options:facts['trip_options']=options
        return facts
    def fastest_order(self,t,steps):
        '''Rebuild the navigation steps in the fastest visiting order when the driver asks for it (route_order).'''
        if not self.tools or not hasattr(self.tools,'travel_matrix') or not route_order.FASTEST.search(t['prompt']):return None
        places=t.get('private_destinations',{})
        coordinates={label:private_coordinates(e) for label,e in places.items() if private_coordinates(e)}
        try:
            origin=self.tools.maps.origin()
            return route_order.reorder(steps,t['prompt'],coordinates,places,{'latitude':origin['latitude'],'longitude':origin['longitude']},self.tools.travel_matrix)
        except Exception as exc:
            self.event(t,'route_order','failed',output={'error':str(exc)});return None
    def bind_plan(self,t,ctx,store):
        '''Bind arguments and record which steps the system could not verify.

        A step is verified when its evidence uses recognised wording AND every
        argument comes from the request text or resolved context. Unverified
        write steps are not rejected; the coordinator asks the driver to confirm.
        '''
        steps=[];t['unverified_steps']=[];t['unsaved_contacts']=[]
        for index,step in enumerate(t['proposal']['steps']):
            try:recognised=validate_intent(step,t['prompt'])
            except NotRequested:
                # Proposed from a comparison clause only, while the driver asked for other actions: left out.
                t.setdefault('dropped_steps',[]).append({'step':index,'capability':step['capability'],'evidence':step['evidence']});continue
            cap=step['capability'];args={};ungrounded=[]
            for key,kind in CAPABILITIES[cap][0].items():
                value=step['arguments'][key]
                dep=re.fullmatch(r'\$step\[(\d+)\]\.([a-z_]+)',value)
                if dep:
                    if int(dep[1])>=index:raise V2Error('Invalid forward dependency')
                    if CAPABILITIES[t['proposal']['steps'][int(dep[1])]['capability']][1]!='read':raise V2Error('Dependency must reference a read result')
                    args[key]=value;continue
                # A value to save is kept exactly as proposed; profile_saves checks it is the driver's own words.
                if kind=='literal':args[key]=value;continue
                # Clarified field bindings take precedence over unresolved original phrases.
                binding=t['bindings'].get(f'{index}:{key}') or bind_argument(value,kind,ctx,store)
                if binding:
                    if kind=='location' and binding.get('private'):t.setdefault('private_destinations',{})[binding['label']]=binding
                    if binding.get('one_off'):t['unsaved_contacts'].append(binding['label'])
                    args[key]=binding['label'];continue
                if kind in {'person','location'}:
                    if kind=='location' and value and not re.search(r'\b(?:my|home|work|there|that|discussed)\b',value,re.I):
                        if not grounded_in(value,t['prompt']):ungrounded.append(key)
                        args[key]=value;continue
                    exc=MissingSlot(value,kind);exc.slot.update(step=index,argument=key);raise exc
                if not value.strip():
                    exc=MissingSlot(value,'text');exc.slot.update(step=index,argument=key);raise exc
                if cap=='media.play':
                    pref=next((e for e in ctx['entities'].values() if e['type']=='preference'),None)
                    if pref and re.search(r'usual|favou?rite|preferred',t['prompt'],re.I):args[key]=pref['label'];continue
                if not grounded_in(value,t['prompt']):ungrounded.append(key)
                args[key]=value
            if not recognised or ungrounded:
                t['unverified_steps'].append({'step':index,'capability':cap,'recognised_wording':recognised,'ungrounded_arguments':ungrounded})
            steps.append({'capability':cap,'arguments':args})
        if t.get('dropped_steps') and (not steps or '$step[' in json.dumps(steps)):
            raise V2Error('The request mixes a comparison with actions in a way that cannot be split safely. Ask for the action on its own.')
        return steps
    def ask(self,s,t,exc):
        if t['round']>=MAX_CONTEXT_ROUNDS:return self.finish(s,t,'failed',error='Context remains unresolved after two answers. Start a new request.')
        slot=exc.slot;slot['slot_id']=f"slot-{t['round']+1}";t['slot']=slot
        self.event(t,'clarification','selected',output=slot)
        return self.finish(s,t,'awaiting_clarification',clarification={**slot,'round':t['round'],'request_id':t['request_id']})
    def clarify(self,sid,answer,request_id=None,slot_id=None,cancel_event=None):
        s=self.sessions.get(sid)
        with s.lock:
            t=s.pending
            if not t or t['result']['status']!='awaiting_clarification':raise V2Error('No clarification pending')
            if request_id and request_id!=t['request_id']:raise V2Error('Wrong pending request')
            slot=t['slot']
            if slot_id and slot_id!=slot['slot_id']:raise V2Error('Stale clarification field')
            t['round']+=1;t['cancel_event']=cancel_event;t['result']['status']='running';t['step_started']=time.perf_counter()
            matches=s.store.find(answer,slot['type'] if slot['type'] in {'person','location','preference'} else None)
            candidates={item['id'] for item in slot.get('candidates',[])}
            if not matches and candidates:
                matches=[e for e in t.get('context',{}).get('entities',{}).values()
                         if e['id'] in candidates and answer.casefold() in {e['id'].casefold(),e['label'].casefold()}]
            if len(matches)==1:e=matches[0]
            elif slot['type'] in {'text','preference'}:e=explicit_entity(answer,slot['type'])
            elif slot['type']=='location' and answer.strip() and not re.search(r'\b(?:there|my|that)\b',answer,re.I):e=explicit_entity(answer,'location')
            # No saved match: a typed name or number is a one-off contact (calls and messages are still confirmed).
            elif slot['type']=='person' and not matches and one_off_contact(answer):e=one_off_contact(answer)
            else:return self.ask(s,t,MissingSlot(slot['reference'],slot['type'],candidates=[{'id':m['id'],'label':m['label']} for m in matches]))
            # An answer for a personal place (home, work, my ...) is private for this request too.
            if slot['type']=='location' and re.search(r'\b(?:my|our|home|work)\b',slot['reference'],re.I):e=dict(e,private=True)
            t['bindings'][slot['reference'].casefold()]=e
            if slot['reference'].strip() and slot['type'] in {'person','location','preference'}:
                s.clarified_references[(slot['type'],slot['reference'].strip().casefold())]=dict(copy.deepcopy(e),source='session',private=True)
            if 'step' in slot:t['bindings'][f"{slot['step']}:{slot['argument']}"]=e
            t['result'].pop('clarification',None)
        return self.resume(s,t)
    def confirm(self,sid,request_id,plan_hash,approved,cancel_event=None):
        s=self.sessions.get(sid)
        with s.lock:
            t=s.pending
            if not t or t['result']['status']!='awaiting_confirmation':raise V2Error('No confirmation pending')
            if request_id!=t['request_id'] or plan_hash!=t['result']['plan']['hash'] or not isinstance(approved,bool):raise V2Error('Confirmation does not match the exact pending plan')
            t['cancel_event']=cancel_event;t['result']['status']='running';t['step_started']=time.perf_counter()
            if not approved:return self.finish(s,t,'cancelled',confirmation_required=False)
            self.event(t,'confirmation','complete',output={'approved':True})
        return self.execute(s,t)
    def execute(self,s,t):
        receipts=[]
        for step_index,step in enumerate(t['result']['plan']['steps']):
            with s.lock:
                if self.cancelled(s,t):return self.finish(s,t,'cancelled',receipts=receipts)
                try:
                    args=dict(step['arguments'])
                    for key,value in args.items():
                        dep=re.fullmatch(r'\$step\[(\d+)\]\.([a-z_]+)',value)
                        if dep:
                            value=receipts[int(dep[1])].get(dep[2])
                            if not isinstance(value,str) or not value:raise V2Error('Dependency result unavailable')
                            args[key]=value
                    if step.get('save'):
                        # A typed address is looked up only now, after the driver approved the card that said so.
                        resolve=(lambda address:self.tools.execute({'tool':'map.resolve_address','arguments':{'address':address}})) if self.tools else None
                        receipt=profile_saves.write(step['save'],s,resolve,outbound_blocklist(t.get('sensitivity',{})))
                        if receipt.get('address_lookup'):
                            self.event(t,'map_resolve_address','complete',input={'address':step['save']['lookup']['address']},output=receipt.pop('address_lookup'))
                        else:receipt.pop('address_lookup',None)
                    else:receipt=t.get('read_outputs',{}).get(step_index) or self.adapters.execute(step['capability'],args,s,t)
                    receipts.append(receipt)
                    if receipt.get('navigation'):
                        self.event(t,'tool_gateway','complete',output={'mode':'validated local map tools'})
                        if t.get('map_search'):self.event(t,'map_find_places','complete',output=t['map_search'])
                        if t.get('address_resolution'):self.event(t,'map_resolve_address','complete',output=t['address_resolution'])
                        self.event(t,'map_route','complete',output=receipt['navigation'])
                        self.event(t,'sample_api','complete',output=receipt.get('api_preview',{}))
                    for key,kind in [('destination','location'),('playback','preference'),('active_call','person')]:
                        if s.state.get(key):s.references['last_contact' if key=='active_call' else key]=explicit_entity(s.state[key],kind,'session')
                except Exception as exc:return self.finish(s,t,'partially_completed' if receipts else 'failed',error=str(exc),receipts=receipts,downstream_invoked=bool(receipts))
        # The route line and turn steps are for the driver's screen only: kept out of receipts, trace and model input.
        shapes=[x.pop('route_shape',None) for x in receipts]
        self.event(t,'simulated_adapters','complete',output={'receipts':receipts})
        # The latest route wins: later steps (e.g. added stops) supersede the first route.
        navigation=next((x.get('navigation') for x in reversed(receipts) if x.get('navigation')),None)
        preview=next((x.get('api_preview') for x in reversed(receipts) if x.get('api_preview')),None)
        order=t['result']['plan'].get('route_order')
        answer='\n'.join(([route_order.summary(order)] if order else [])+[x.get('message','') for x in receipts])
        return self.finish(s,t,'completed',receipts=receipts,receipt=receipts[-1],answer=answer,
            navigation=navigation,api_preview=preview,execution_ready=True,downstream_invoked=True,confirmation_required=False,
            actions=driver_actions.build(t['result']['plan']['steps'],receipts,shapes))
    def cancel(self,sid,request_id):
        s=self.sessions.get(sid)
        with s.lock:
            t=s.pending
            if not t or t['request_id']!=request_id:raise V2Error('No matching pending request')
            t['cancelled']=True
            return self.finish(s,t,'cancelled')
    def finish(self,s,t,status,**extra):
        with s.lock:
            r=t['result'];r.update(status=status,**extra)
            # total includes time spent waiting for the driver; this_step is the system's own time.
            now=time.perf_counter()
            r['timing_ms']={'total':round((now-t['started'])*1000,3),'this_step':round((now-t.get('step_started',t['started']))*1000,3)}
            self.event(t,'request_coordinator','complete' if status=='completed' else status,output={'status':status})
            self.event(t,'response','complete',output={'status':status,'answer':r.get('answer'),'error':r.get('error')})
            # What happened to the driver's data in this request (no content): Block details panel + local log.
            r['privacy_receipt']=privacy_receipt.build(r)
            try:privacy_receipt.log(r,r['privacy_receipt'])
            except OSError:pass
            if s.generation==t['generation']:
                r['state']=copy.deepcopy(s.state)
                s.turns=[x for x in s.turns if x['request_id']!=t['request_id']]
                self.event(t,'session_update','complete',output={'turn_count':min(20,len(s.turns)+1)})
                s.turns.append(copy.deepcopy(r));s.turns=s.turns[-20:]
                if t.get('idempotency_key'):
                    s.request_cache[t['idempotency_key']]={'prompt':t.get('original_prompt',t['prompt']),'result':copy.deepcopy(r)}
                    while len(s.request_cache)>100:s.request_cache.pop(next(iter(s.request_cache)))
                if status not in {'awaiting_confirmation','awaiting_clarification','running'} and s.pending is t:s.pending=None
            return copy.deepcopy(r)

COORDINATOR=None
INIT_LOCK=threading.Lock()
def get_coordinator():
    global COORDINATOR
    with INIT_LOCK:
        if COORDINATOR is None:
            root=os.environ.get('JLR_PROFILES_DIR')
            COORDINATOR=V2Coordinator(sessions=V2Sessions(profiles=ProfileRegistry(root)) if root else None)
    return COORDINATOR
