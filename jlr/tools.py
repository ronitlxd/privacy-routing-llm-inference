"""Read-only map tools: OpenStreetMap search (Nominatim) and driving routes (OSRM)."""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .contract import ContractError

TOOL_SPECS = {
    'map.resolve_address': {'address'},
    'map.find_places': {'query'},
    'map.route': {'destination_id'},
}
MAX_TOOL_CALLS = 4
MAX_INFERENCE_CALLS = 3

def tool_schema():
    def obj(props):
        return {'type':'object','properties':props,'required':list(props),'additionalProperties':False}
    text={'type':'string'}
    return {'anyOf':[obj({'tool':{'type':'string','enum':[name]},'arguments':obj({key:text for key in args})})
                     for name,args in TOOL_SPECS.items()]}

def validate_tool_request(value):
    if not isinstance(value,dict) or set(value)!={'kind','request'} or value.get('kind')!='tool_request':
        raise ContractError('Invalid tool request')
    request=value['request']
    if not isinstance(request,dict) or set(request)!={'tool','arguments'}:raise ContractError('Invalid tool request fields')
    name=request['tool'];args=request['arguments']
    if name not in TOOL_SPECS or not isinstance(args,dict) or set(args)!=TOOL_SPECS[name]:
        raise ContractError('Unapproved tool or arguments')
    if any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in args.values()):
        raise ContractError('Invalid tool argument')
    return value

COORDINATES=re.compile(r'^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$')

def parse_coordinates(value):
    """Return (lat, lon) from 'lat, lon' text, or None."""
    match=COORDINATES.match(value) if isinstance(value,str) else None
    if not match:return None
    lat,lon=float(match[1]),float(match[2])
    return (lat,lon) if -90<=lat<=90 and -180<=lon<=180 else None

MANEUVERS={'depart':'Head {modifier}','arrive':'Arrive at your destination','turn':'Turn {modifier}','new name':'Continue',
           'merge':'Merge {modifier}','on ramp':'Take the ramp {modifier}','off ramp':'Take the exit {modifier}','fork':'Keep {modifier} at the fork',
           'end of road':'Turn {modifier} at the end of the road','continue':'Continue {modifier}','roundabout':'Enter the roundabout',
           'rotary':'Enter the roundabout','roundabout turn':'At the roundabout, turn {modifier}','notification':'Continue','use lane':'Continue {modifier}'}

def step_text(step):
    """Plain turn instruction from an OSRM step ('Turn left onto Ouellette Avenue')."""
    m=step.get('maneuver') or {};kind=m.get('type','');modifier=(m.get('modifier') or '').replace('straight','straight ahead')
    if kind=='arrive' and (m.get('modifier') in {'left','right'}):return f"Arrive at your destination on the {m['modifier']}"
    text=MANEUVERS.get(kind,'Continue {modifier}').format(modifier=modifier).replace('  ',' ').strip()
    name=step.get('name') or step.get('ref') or ''
    # OSRM's departure direction is relative to an unknown heading, so it is not spoken.
    if kind=='depart':return f'Start on {name}' if name else 'Start driving'
    if name and kind!='arrive':text+=' onto '+name
    return text

class OsmTools:
    """Free OpenStreetMap services: Nominatim and Photon (search), OSRM (driving).

    Public search text and typed public addresses go to the search services; OSRM
    receives coordinates only; saved private places arrive as `coords:lat,lon`.
    Nominatim matches names exactly ("McDonald's"); when it finds nothing, place
    search falls back to Photon, which tolerates spelling and punctuation
    ("Mcdonalds"). Both are throttled to one request per second with an
    identifying User-Agent, as their usage policies require.
    """
    USER_AGENT='JLR-V2-Research-Prototype/1.0 (University of Windsor student project)'
    _lock=threading.Lock();_last=0.0
    def __init__(self,nominatim=None,osrm=None,transport=None):
        self.nominatim=(nominatim or os.environ.get('JLR_NOMINATIM_URL','https://nominatim.openstreetmap.org')).rstrip('/')
        self.osrm=(osrm or os.environ.get('JLR_OSM_ROUTER_URL','https://router.project-osrm.org')).rstrip('/')
        self.photon=os.environ.get('JLR_PHOTON_URL','https://photon.komoot.io').rstrip('/')
        self.transport=transport or self._http_get
        self.places={};self.cache={}
    @classmethod
    def _http_get(cls,url):
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':cls.USER_AGENT}),timeout=12) as response:
            return json.loads(response.read(2_000_001))
    def origin(self):
        return {'id':'configured-origin','label':'Configured starting location',
                'address':os.environ.get('JLR_ORIGIN_ADDRESS','Windsor City Hall, 350 City Hall Square West, Windsor, Ontario, Canada'),
                'latitude':float(os.environ.get('JLR_ORIGIN_LATITUDE','42.317102946666665')),
                'longitude':float(os.environ.get('JLR_ORIGIN_LONGITUDE','-83.03487285333334')),
                'provenance':'configured starting location—not GPS','kind':'Configured starting location—not GPS'}
    RETRY_STATUS={429,500,502,503,504};RETRY_DELAY=1.5
    def _fetch(self,url):
        '''One retry for temporary failures (timeouts, dropped connections, busy servers); other errors fail at once.'''
        try:return self.transport(url)
        except ContractError:raise
        except urllib.error.HTTPError as exc:
            if exc.code not in self.RETRY_STATUS:raise
        except Exception:pass
        time.sleep(self.RETRY_DELAY);return self.transport(url)
    def _get(self,url,*,throttle=False):
        if url in self.cache:return self.cache[url]
        try:
            if throttle:
                with OsmTools._lock:
                    wait=1.05-(time.monotonic()-OsmTools._last)
                    if wait>0:time.sleep(wait)
                    OsmTools._last=time.monotonic();data=self._fetch(url)
            else:data=self._fetch(url)
        except ContractError:raise
        except urllib.error.HTTPError as exc:raise ContractError(f'OpenStreetMap request failed (HTTP {exc.code})') from None
        except Exception:raise ContractError('OpenStreetMap services are unavailable') from None
        if len(self.cache)>500:self.cache.clear()
        self.cache[url]=data;return data
    def _search(self,params):
        return self._get(self.nominatim+'/search?'+urllib.parse.urlencode({**params,'format':'jsonv2','countrycodes':'ca'}),throttle=True)
    def _photon(self,query,origin):
        '''Spelling-tolerant search, mapped to the same shape as Nominatim results (Canada only).'''
        data=self._get(self.photon+'/api/?'+urllib.parse.urlencode({'q':query,'lat':origin['latitude'],'lon':origin['longitude'],'limit':15}),throttle=True)
        items=[]
        for feature in data.get('features') or []:
            props=feature.get('properties') or {};point=(feature.get('geometry') or {}).get('coordinates') or []
            if props.get('countrycode','').upper()!='CA' or len(point)!=2 or not props.get('osm_id') or not props.get('name'):continue
            street=' '.join(str(props[k]) for k in ('housenumber','street') if props.get(k))
            address=', '.join(x for x in (props['name'],street,props.get('city'),props.get('state'),'Canada') if x)
            items.append({'osm_type':{'N':'node','W':'way','R':'relation'}.get(props.get('osm_type'),'node'),'osm_id':props['osm_id'],
                          'name':props['name'],'display_name':address,'lat':point[1],'lon':point[0]})
        return items
    def metadata(self):
        return {'source':'OpenStreetMap (Nominatim/Photon + OSRM)','attribution':'© OpenStreetMap contributors (ODbL)','country_filter':'CA',
                'retrieved_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    def _place(self,item):
        place={'id':f"osm:{item['osm_type'][0].upper()}{item['osm_id']}",'name':item.get('name') or item.get('display_name','').split(',')[0],
               'brand':'','address':item.get('display_name',''),'latitude':float(item['lat']),'longitude':float(item['lon']),
               'provenance':'OpenStreetMap via Nominatim'}
        self.places[place['id']]=place
        return place
    @staticmethod
    def _coords(point):return f"{point['longitude']:.6f},{point['latitude']:.6f}"
    def find_places(self,query):
        origin=self.origin();lat,lon=origin['latitude'],origin['longitude']
        found=self._search({'q':query,'limit':25,'bounded':1,'viewbox':f'{lon-0.35},{lat+0.27},{lon+0.35},{lat-0.27}'})
        if not found:found=self._photon(query,origin)
        rows=[self._place(item) for item in found if item.get('osm_id') and item.get('lat')]
        if rows:
            table=self._get(f"{self.osrm}/table/v1/driving/{self._coords(origin)};{';'.join(self._coords(r) for r in rows)}?sources=0&annotations=distance,duration")
            if table.get('code')=='Ok':
                distances=(table.get('distances') or [[]])[0][1:];durations=(table.get('durations') or [[]])[0][1:]
                for row,distance,duration in zip(rows,distances,durations):
                    if distance is not None:row.update(distance_m=round(float(distance),1),duration_seconds=round(float(duration),1) if duration is not None else None)
        routed=sorted((row for row in rows if 'distance_m' in row),key=lambda row:row['distance_m'])
        unique=[]
        for row in routed:
            if any(abs(row['latitude']-old['latitude'])<0.0002 and abs(row['longitude']-old['longitude'])<0.0002 for old in unique):continue
            unique.append(row)
        within=[r for r in unique if r['distance_m']<=10000] or [r for r in unique if r['distance_m']<=30000]
        return {'query':query,'origin':origin,'candidates':within[:8],'nearest':within[0] if within else None,
                'selection':'driving_distance','country_filter':'CA','dataset':self.metadata()}
    def resolve_address(self,address):
        rows=[]
        for item in self._search({'q':address,'limit':5}):
            place=self._place(item)
            # Nominatim often returns a building and its address node; treat co-located hits as one place.
            if not any(abs(place['latitude']-old['latitude'])<0.0005 and abs(place['longitude']-old['longitude'])<0.0005 for old in rows):rows.append(place)
        # A house number missing from the map makes Nominatim return the pieces of that one street.
        # That is one place, known only to street level, not a choice between places.
        # Each piece may carry its own postal code, so pieces are compared without it, and a postal
        # code the driver typed picks the matching piece.
        postal=re.compile(r'[A-Z]\d[A-Z] ?\d[A-Z]\d|\d{5}(?:-\d{4})?',re.I)
        parts=lambda text:[p.strip() for p in text.split(',') if not postal.fullmatch(p.strip())]
        if len(rows)>1 and len({tuple(parts(row['address'])) for row in rows})==1:
            typed={code.replace(' ','').upper() for code in postal.findall(address)}
            match=[row for row in rows if any(code.replace(' ','').upper() in typed for code in postal.findall(row['address']))]
            street=dict(match[0] if match else rows[len(rows)//2],precision='street')
            return {'status':'resolved','place':street,'dataset':self.metadata()}
        if len(rows)!=1:return {'status':'missing' if not rows else 'ambiguous','query':address,'candidates':rows[:5]}
        return {'status':'resolved','place':rows[0],'dataset':self.metadata()}
    def _destination(self,destination_id):
        coords=parse_coordinates(destination_id[7:]) if destination_id.startswith('coords:') else None
        if coords:
            return {'id':destination_id,'name':'Saved location','brand':'','address':'Coordinates only',
                    'latitude':coords[0],'longitude':coords[1],'provenance':'Coordinates from the local private database'}
        if destination_id in self.places:return self.places[destination_id]
        match=re.fullmatch(r'osm:([NWR])(\d+)',destination_id)
        if not match:raise ContractError('Mapped destination was not found')
        found=self._get(self.nominatim+'/lookup?'+urllib.parse.urlencode({'osm_ids':match[1]+match[2],'format':'jsonv2'}),throttle=True)
        if not found:raise ContractError('Mapped destination was not found')
        return self._place(found[0])
    ROUTE_DETAIL='overview=false&steps=false'
    SHAPE_DETAIL='overview=simplified&geometries=geojson&steps=true'
    @staticmethod
    def _shape(route):
        """Route line ([lat, lon] pairs) and turn-by-turn steps for the driver's map; never sent to a model."""
        line=[[round(lat,5),round(lon,5)] for lon,lat in (route.get('geometry') or {}).get('coordinates') or []]
        steps=[]
        for leg in route.get('legs') or []:
            for step in leg.get('steps') or []:
                text=step_text(step)
                if text:steps.append({'text':text,'distance_m':round(float(step.get('distance') or 0))})
        return {'geometry':line,'steps':steps}
    def route(self,destination_id,shape=False):
        origin=self.origin();destination=self._destination(destination_id)
        data=self._get(f"{self.osrm}/route/v1/driving/{self._coords(origin)};{self._coords(destination)}?{self.SHAPE_DETAIL if shape else self.ROUTE_DETAIL}")
        if data.get('code')!='Ok' or not data.get('routes'):raise ContractError('No driving route is available')
        route=data['routes'][0]
        out={'origin':origin,'destination':destination,'distance_m':round(float(route['distance']),1),
             'duration_seconds':round(float(route['duration']),1),'dataset':self.metadata()}
        if shape:out['shape']=self._shape(route)
        return out
    def travel_matrix(self,points):
        '''Driving duration (s) and distance (m) between every pair of points. Coordinates only.'''
        data=self._get(f"{self.osrm}/table/v1/driving/{';'.join(self._coords(p) for p in points)}?annotations=duration,distance")
        if data.get('code')!='Ok' or not data.get('durations'):raise ContractError('No driving times are available')
        return {'durations':data['durations'],'distances':data.get('distances') or [[None]*len(points) for _ in points]}
    def route_via(self,points,shape=False):
        '''Drive origin -> points[0..-2] as stops -> points[-1]. Points are coordinates only.'''
        origin=self.origin()
        data=self._get(f"{self.osrm}/route/v1/driving/{';'.join(self._coords(p) for p in [origin,*points])}?{self.SHAPE_DETAIL if shape else self.ROUTE_DETAIL}")
        if data.get('code')!='Ok' or not data.get('routes'):raise ContractError('No driving route is available')
        route=data['routes'][0]
        out={'origin':origin,'distance_m':round(float(route['distance']),1),'duration_seconds':round(float(route['duration']),1),'dataset':self.metadata()}
        if shape:out['shape']=self._shape(route)
        return out

class ToolGateway:
    def __init__(self,maps=None):self.maps=maps or OsmTools()
    def route_via(self,points,shape=False):
        '''System-only multi-stop routing (not a model tool): accepts coordinates, never labels or addresses.'''
        if not 1<=len(points)<=6:raise ContractError('A route can have at most five stops')
        clean=[{'latitude':float(p['latitude']),'longitude':float(p['longitude'])} for p in points]
        return self.maps.route_via(clean,shape=shape)
    def route_with_shape(self,destination_id):
        '''System-only (not a model tool): the route plus its line and turn steps for the driver's map.'''
        validate_tool_request({'kind':'tool_request','request':{'tool':'map.route','arguments':{'destination_id':destination_id}}})
        return self.maps.route(destination_id,shape=True)
    def travel_matrix(self,points):
        '''System-only (not a model tool): times between saved places, from coordinates only.'''
        if not 2<=len(points)<=7:raise ContractError('Travel times need 2 to 7 places')
        return self.maps.travel_matrix([{'latitude':float(p['latitude']),'longitude':float(p['longitude'])} for p in points])
    def execute(self,request):
        validate_tool_request({'kind':'tool_request','request':request})
        name,args=request['tool'],request['arguments']
        if name=='map.resolve_address':return self.maps.resolve_address(args['address'])
        if name=='map.find_places':return self.maps.find_places(args['query'])
        if name=='map.route':return self.maps.route(args['destination_id'])
        raise ContractError('Unapproved tool')
