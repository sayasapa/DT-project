#!/usr/bin/env python3
"""
Multi-City Air Quality Collector for Kazakhstan
Collects air quality + industrial-source context for 5 cities of different
pollution profiles, for comparative chemical-marker source attribution.

Cities & dominant source types:
  Karaganda  - coal + steel (Qarmet)          -> PM2.5, H2S
  Temirtau   - ferrous metallurgy (Qarmet)    -> phenol, H2S, SO2
  Pavlodar   - aluminium + oil refining       -> SO2, HF
  Atyrau     - oil & gas refining             -> H2S, hydrocarbons
  Shymkent   - control (transport + refinery) -> baseline

SECRETS (GitHub Actions):
  WAQI_TOKEN, OPENWEATHER_KEY, TOMTOM_KEY  (reuses existing secrets)
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request

WAQI_TOKEN      = os.environ.get("WAQI_TOKEN", "")
OPENWEATHER_KEY = os.environ.get("OPENWEATHER_KEY", "")
TOMTOM_KEY      = os.environ.get("TOMTOM_KEY", "")
OUT_CSV         = "data/cities_air_data.csv"

# ---------------- CITY & SOURCE CONFIG ----------------
CITIES = {
    "Karaganda": {
        "lat": 49.8047, "lon": 73.1094, "type": "coal_steel",
        "sources": {
            "SteelWorksCHP": {"lat": 50.0479, "lon": 73.0202, "type": "steel_power"},
            "KaragandaCHP2": {"lat": 49.7500, "lon": 73.1200, "type": "coal_chp"},
        }
    },
    "Temirtau": {
        "lat": 50.0546, "lon": 72.9648, "type": "ferrous_metallurgy",
        "sources": {
            "Qarmet":  {"lat": 50.0318, "lon": 72.9949, "type": "steel_plant"},
            "TemirtauCHP": {"lat": 50.0479, "lon": 73.0202, "type": "steel_power"},
        }
    },
    "Pavlodar": {
        "lat": 52.2870, "lon": 76.9674, "type": "aluminium_petrochem",
        "sources": {
            "AluminaPlant": {"lat": 52.2592, "lon": 77.0463, "type": "alumina_refinery"},
            "PavlodarRefinery": {"lat": 52.2100, "lon": 76.9800, "type": "oil_refinery"},
        }
    },
    "Atyrau": {
        "lat": 47.0945, "lon": 51.9238, "type": "oil_gas",
        "sources": {
            "AtyrauRefinery": {"lat": 47.1000, "lon": 51.8800, "type": "oil_refinery"},
        }
    },
    "Shymkent": {
        "lat": 42.3417, "lon": 69.5901, "type": "control_transport",
        "sources": {
            "ShymkentRefinery": {"lat": 42.3000, "lon": 69.6500, "type": "oil_refinery"},
        }
    },
}

# ---------------- HELPERS ----------------
def geodist_km(lat1, lon1, lat2, lon2):
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2)/2)) * 111.32
    y = (lat2 - lat1) * 111.32
    return math.hypot(x, y)

def bearing_from(lat_s, lon_s, lat_t, lon_t):
    dlon = math.radians(lon_t - lon_s)
    la1, la2 = math.radians(lat_s), math.radians(lat_t)
    y = math.sin(dlon)*math.cos(la2)
    x = math.cos(la1)*math.sin(la2) - math.sin(la1)*math.cos(la2)*math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def is_downwind(src_bearing, wind_deg, tol=60):
    if wind_deg is None: return 0
    plume_to = (wind_deg + 180) % 360
    return 1 if abs((src_bearing - plume_to + 180) % 360 - 180) <= tol else 0

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kz-cities-collector/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    fetch error: {e}")
        return None

def get_stations(city):
    lat1, lon1 = city["lat"]-0.3, city["lon"]-0.4
    lat2, lon2 = city["lat"]+0.3, city["lon"]+0.4
    url = f"https://api.waqi.info/map/bounds/?latlng={lat1},{lon1},{lat2},{lon2}&token={WAQI_TOKEN}"
    data = fetch_json(url)
    out = []
    if data and data.get("status") == "ok":
        for s in data["data"]:
            out.append({"uid": s.get("uid"), "lat": s.get("lat"), "lon": s.get("lon"),
                        "name": s.get("station", {}).get("name", "unknown"), "aqi": s.get("aqi")})
    return out

def get_detail(uid):
    url = f"https://api.waqi.info/feed/@{uid}/?token={WAQI_TOKEN}"
    data = fetch_json(url)
    if data and data.get("status") == "ok":
        d = data["data"]; iaqi = d.get("iaqi", {})
        def v(k): 
            x = iaqi.get(k); return x.get("v") if x else None
        return {"pm25": v("pm25"), "pm10": v("pm10"), "no2": v("no2"),
                "so2": v("so2"), "co": v("co"), "o3": v("o3"),
                "aqi": d.get("aqi"), "dominentpol": d.get("dominentpol"),
                "time": d.get("time", {}).get("iso")}
    return None

def get_weather(lat, lon):
    if not OPENWEATHER_KEY: return {}
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_KEY}&units=metric"
    d = fetch_json(url)
    if d and d.get("main"):
        return {"temp_c": d["main"].get("temp"), "humidity": d["main"].get("humidity"),
                "pressure": d["main"].get("pressure"),
                "wind_speed": d.get("wind", {}).get("speed"),
                "wind_deg": d.get("wind", {}).get("deg"),
                "weather": d.get("weather", [{}])[0].get("description", "")}
    return {}

def get_traffic(lat, lon):
    if not TOMTOM_KEY: return {}
    url = (f"https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
           f"?point={lat},{lon}&key={TOMTOM_KEY}")
    d = fetch_json(url)
    if d and d.get("flowSegmentData"):
        f = d["flowSegmentData"]; cur = f.get("currentSpeed"); free = f.get("freeFlowSpeed")
        cong = round(max(0,(1-cur/free))*100,1) if (cur is not None and free) else None
        return {"current_speed": cur, "free_flow_speed": free, "congestion_percent": cong}
    return {}

# ---------------- MAIN ----------------
def collect():
    ts = datetime.now(timezone.utc).isoformat()
    cycle_id = int(time.time())
    heating = 1 if datetime.now().month in (10,11,12,1,2,3) else 0
    print(f"[{ts}] Multi-city collection (cycle {cycle_id})")

    all_rows = []
    for cname, city in CITIES.items():
        weather = get_weather(city["lat"], city["lon"])
        wind_deg = weather.get("wind_deg")
        stations = get_stations(city)
        print(f"  {cname}: {len(stations)} stations")

        for st in stations:
            if st["lat"] is None or st["lon"] is None: continue
            detail = get_detail(st["uid"]) or {}
            time.sleep(0.4)
            traffic = get_traffic(st["lat"], st["lon"])
            time.sleep(0.3)

            src_feats = {}
            for sname, s in city["sources"].items():
                d = geodist_km(s["lat"], s["lon"], st["lat"], st["lon"])
                b = bearing_from(s["lat"], s["lon"], st["lat"], st["lon"])
                src_feats[f"dist_{sname}_km"] = round(d,2)
                src_feats[f"bearing_{sname}"] = round(b,1)
                src_feats[f"downwind_{sname}"] = is_downwind(b, wind_deg)
            nearest = min(city["sources"].items(),
                          key=lambda kv: geodist_km(kv[1]["lat"], kv[1]["lon"], st["lat"], st["lon"]))
            nd = round(geodist_km(nearest[1]["lat"], nearest[1]["lon"], st["lat"], st["lon"]),2)

            row = {"timestamp_utc": ts, "cycle_id": cycle_id,
                   "city": cname, "city_type": city["type"],
                   "station_uid": st["uid"], "station_name": st["name"],
                   "lat": st["lat"], "lon": st["lon"],
                   "pm25": detail.get("pm25"), "pm10": detail.get("pm10"),
                   "no2": detail.get("no2"), "so2": detail.get("so2"),
                   "co": detail.get("co"), "o3": detail.get("o3"),
                   "aqi": detail.get("aqi"), "dominentpol": detail.get("dominentpol"),
                   "current_speed": traffic.get("current_speed"),
                   "congestion_percent": traffic.get("congestion_percent"),
                   "nearest_source": nearest[0], "nearest_source_dist_km": nd,
                   **src_feats,
                   "temp_c": weather.get("temp_c"), "humidity": weather.get("humidity"),
                   "wind_speed": weather.get("wind_speed"), "wind_deg": wind_deg,
                   "weather_desc": weather.get("weather"), "heating_season": heating}
            all_rows.append(row)

    if all_rows:
        os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
        # union of all keys (cities have different source columns)
        allkeys = []
        for r in all_rows:
            for k in r:
                if k not in allkeys: allkeys.append(k)
        file_exists = os.path.isfile(OUT_CSV)
        # if file exists, align header
        mode = "a" if file_exists else "w"
        with open(OUT_CSV, mode, newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=allkeys, extrasaction="ignore")
            if not file_exists: w.writeheader()
            for r in all_rows: w.writerow(r)
        print(f"  Wrote {len(all_rows)} rows to {OUT_CSV}")
    else:
        print("  No data collected")

if __name__ == "__main__":
    if not WAQI_TOKEN:
        print("ERROR: set WAQI_TOKEN")
    else:
        collect()
