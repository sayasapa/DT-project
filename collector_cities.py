#!/usr/bin/env python3
"""
Multi-City Air Quality Collector for Kazakhstan.
Uses the WORKING method proven in the UKG collector:
  - query stations by numeric UID via /feed/@{uid}/ AND /feed/A{uid}/ (quirk)
  - validate nested "data" is a real station (not an error inside status:ok)
  - weather from OpenWeather (PM sensors report no weather)

FILL IN real station UIDs per city (found on aqicn.org station pages),
the same way UKG UIDs (517390/517402/517507) were found.

SECRETS: WAQI_TOKEN, OPENWEATHER_KEY, TOMTOM_KEY
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request

WAQI_TOKEN      = os.environ.get("WAQI_TOKEN", "")
OPENWEATHER_KEY = os.environ.get("OPENWEATHER_KEY", "")
TOMTOM_KEY      = os.environ.get("TOMTOM_KEY", "")
OUT_CSV         = "data/cities_air_data.csv"

# Per city: real AQICN station UIDs + industrial sources.
# UIDs below are PLACEHOLDERS discovered from search snippets — verify & extend.
# Karaganda 89403/89417 came from aqicn station pages (sensor.community).
# UIDs are real AQICN station IDs found on aqicn.org station pages.
# 'stations' accepts int UIDs (tried as @UID then AUID) or str slugs
# (tried as /feed/{slug}/). Extend as more stations are confirmed.
CITIES = {
    "Karaganda": {"type": "coal_steel",
        "sources": {"SteelWorks": (50.0479, 73.0202), "KaragandaCHP": (49.7500, 73.1200)},
        "stations": [89403, 89417, 93571]},         # Майкудук, Терешковой, Майқұдық Шығыс-1
    "Temirtau":  {"type": "ferrous_metallurgy",
        "sources": {"Qarmet": (50.0318, 72.9949)},
        "stations": ["kazakhstan-temirtau-9-мкрн", "kazakhstan/temirtau-ss-16",
                     "kazakhstan/temirtau-pravyj-bereg"]},
    "Pavlodar":  {"type": "aluminium_petrochem",
        "sources": {"AluminaPlant": (52.2592, 77.0463), "Refinery": (52.2100, 76.9800)},
        "stations": [236608, 236602,                # КВД, ул.Ткачева
                     "kazakhstan/pavlodar/вкцм", "kazakhstan/pavlodar/квд"]},
    "Atyrau":    {"type": "oil_gas",
        "sources": {"AtyrauRefinery": (47.1000, 51.8800)},
        "stations": ["kazakhstan-atyrau-ак-шагала", "kazakhstan/atyrau"]},
    "Shymkent":  {"type": "control_transport",
        "sources": {"ShymkentRefinery": (42.3000, 69.6500)},
        "stations": ["kazakhstan/shymkent"]},
}

def geodist_km(a,b,c,d):
    x=(d-b)*math.cos(math.radians((a+c)/2))*111.32; y=(c-a)*111.32; return math.hypot(x,y)
def bearing_from(la_s,lo_s,la_t,lo_t):
    dlon=math.radians(lo_t-lo_s); la1=math.radians(la_s); la2=math.radians(la_t)
    y=math.sin(dlon)*math.cos(la2); x=math.cos(la1)*math.sin(la2)-math.sin(la1)*math.cos(la2)*math.cos(dlon)
    return (math.degrees(math.atan2(y,x))+360)%360
def is_downwind(sb,wd,tol=60):
    if wd is None: return 0
    return 1 if abs((sb-(wd+180)%360+180)%360-180)<=tol else 0
def fetch_json(url):
    try:
        req=urllib.request.Request(url, headers={"User-Agent":"kz-cities/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read().decode())
    except Exception as e:
        print(f"    fetch error: {e}"); return None

def fetch_station_feed(q):
    """Proven UKG method. q=int -> try @UID then AUID; q=str -> /feed/{slug}/.
    Validate nested data is a real station (not error inside status:ok)."""
    import urllib.parse
    if isinstance(q, int):
        candidates = [f"@{q}", f"A{q}"]
    else:
        candidates = [q]
    for c in candidates:
        url=f"https://api.waqi.info/feed/{urllib.parse.quote(c)}/?token={WAQI_TOKEN}"
        data=fetch_json(url)
        d=data.get("data") if data else None
        valid=isinstance(d,dict) and ("aqi" in d or "iaqi" in d or "city" in d)
        if data and data.get("status")=="ok" and valid:
            return d
        time.sleep(0.2)
    return None

def get_stations(items):
    out=[]
    for q in items:
        d=fetch_station_feed(q)
        if not d: 
            print(f"    station {q}: no valid feed"); continue
        uid=q
        iaqi=d.get("iaqi",{}); geo=d.get("city",{}).get("geo")
        lat=geo[0] if geo and len(geo)==2 else None
        lon=geo[1] if geo and len(geo)==2 else None
        def v(k): 
            x=iaqi.get(k); return x.get("v") if x else None
        out.append({"uid":uid,"lat":lat,"lon":lon,
            "name":d.get("city",{}).get("name","unknown"),
            "pm25":v("pm25"),"pm10":v("pm10"),"no2":v("no2"),"so2":v("so2"),
            "co":v("co"),"o3":v("o3"),"aqi":d.get("aqi"),
            "dominentpol":d.get("dominentpol"),"aqi_time":d.get("time",{}).get("iso")})
        time.sleep(0.3)
    return out

def get_weather(lat,lon):
    if not OPENWEATHER_KEY or lat is None: return {}
    d=fetch_json(f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_KEY}&units=metric")
    if d and d.get("main"):
        return {"temp_c":d["main"].get("temp"),"humidity":d["main"].get("humidity"),
                "wind_speed":d.get("wind",{}).get("speed"),"wind_deg":d.get("wind",{}).get("deg"),
                "weather":d.get("weather",[{}])[0].get("description","")}
    return {}
def get_traffic(lat,lon):
    if not TOMTOM_KEY or lat is None: return {}
    d=fetch_json(f"https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json?point={lat},{lon}&key={TOMTOM_KEY}")
    if d and d.get("flowSegmentData"):
        f=d["flowSegmentData"];cur=f.get("currentSpeed");free=f.get("freeFlowSpeed")
        cong=round(max(0,(1-cur/free))*100,1) if (cur is not None and free) else None
        return {"current_speed":cur,"congestion_percent":cong}
    return {}

def collect():
    ts=datetime.now(timezone.utc).isoformat(); cid=int(time.time())
    heating=1 if datetime.now().month in (10,11,12,1,2,3) else 0
    print(f"[{ts}] Multi-city (UID feed) cycle {cid}")
    rows=[]
    for cname,city in CITIES.items():
        sts=get_stations(city["stations"])
        print(f"  {cname}: {len(sts)} station(s) with data")
        for st in sts:
            w=get_weather(st["lat"],st["lon"]); wd=w.get("wind_deg")
            tr=get_traffic(st["lat"],st["lon"])
            sf={}
            for sname,(sla,slo) in city["sources"].items():
                dkm=geodist_km(sla,slo,st["lat"],st["lon"]); br=bearing_from(sla,slo,st["lat"],st["lon"])
                sf[f"dist_{sname}_km"]=round(dkm,2); sf[f"bearing_{sname}"]=round(br,1); sf[f"downwind_{sname}"]=is_downwind(br,wd)
            nearest=min(city["sources"].items(), key=lambda kv: geodist_km(kv[1][0],kv[1][1],st["lat"],st["lon"]))
            rows.append({"timestamp_utc":ts,"cycle_id":cid,"city":cname,"city_type":city["type"],
                "station_uid":st["uid"],"station_name":st["name"],"lat":st["lat"],"lon":st["lon"],
                "pm25":st["pm25"],"pm10":st["pm10"],"no2":st["no2"],"so2":st["so2"],"co":st["co"],"o3":st["o3"],
                "aqi":st["aqi"],"dominentpol":st["dominentpol"],"aqi_time":st["aqi_time"],
                "current_speed":tr.get("current_speed"),"congestion_percent":tr.get("congestion_percent"),
                "nearest_source":nearest[0],**sf,
                "temp_c":w.get("temp_c"),"humidity":w.get("humidity"),"wind_speed":w.get("wind_speed"),
                "wind_deg":wd,"weather_desc":w.get("weather"),"heating_season":heating})
    if rows:
        os.makedirs(os.path.dirname(OUT_CSV),exist_ok=True)
        allk=[]
        for r in rows:
            for k in r:
                if k not in allk: allk.append(k)
        ex=os.path.isfile(OUT_CSV)
        with open(OUT_CSV,"a" if ex else "w",newline="",encoding="utf-8") as f:
            wr=csv.DictWriter(f,fieldnames=allk,extrasaction="ignore")
            if not ex: wr.writeheader()
            for r in rows: wr.writerow(r)
        print(f"  Wrote {len(rows)} rows to {OUT_CSV}")
    else:
        print("  No data collected")

if __name__=="__main__":
    if not WAQI_TOKEN: print("ERROR: set WAQI_TOKEN")
    else: collect()
