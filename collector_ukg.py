#!/usr/bin/env python3
"""
Ust-Kamenogorsk (Oskemen) Air Quality Collector
Industrial city — sources: Kazzinc (lead-zinc), UMZ (Ulba metallurgical), CHP.
Collects: air quality (AQICN/WAQI), weather (OpenWeather), traffic (TomTom),
industrial-source context (distance/bearing/downwind per source).
Runs 24/7 via GitHub Actions.

SECRETS (GitHub Actions):
  WAQI_TOKEN       - AQICN/WAQI token   (https://aqicn.org/data-platform/token/)
  OPENWEATHER_KEY  - OpenWeather key     (https://openweathermap.org/api)
  TOMTOM_KEY       - TomTom traffic key  (https://developer.tomtom.com/)
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request

# ---------------- SECRETS (match your GitHub secret names) ----------------
WAQI_TOKEN      = os.environ.get("WAQI_TOKEN", "")
OPENWEATHER_KEY = os.environ.get("OPENWEATHER_KEY", "")
TOMTOM_KEY      = os.environ.get("TOMTOM_KEY", "")
OUT_CSV         = "ukg_air_data.csv"

# City center (Ust-Kamenogorsk / Oskemen)
CITY = {"name": "Ust-Kamenogorsk", "lat": 49.9714, "lon": 82.6059}

# Industrial pollution sources (verified coordinates)
SOURCES = {
    "Kazzinc": {"lat": 49.9800, "lon": 82.6170, "type": "lead_zinc_copper_smelter"},
    "UMZ":     {"lat": 49.9550, "lon": 82.6060, "type": "metallurgical_uranium_beryllium"},
    "CHP":     {"lat": 49.9400, "lon": 82.6300, "type": "thermal_power_plant"},
}

# ---------------- HELPERS ----------------
def geodist_km(lat1, lon1, lat2, lon2):
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)) * 111.32
    y = (lat2 - lat1) * 111.32
    return math.hypot(x, y)

def bearing_from(lat_src, lon_src, lat_st, lon_st):
    dlon = math.radians(lon_st - lon_src)
    la1, la2 = math.radians(lat_src), math.radians(lat_st)
    y = math.sin(dlon) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def is_downwind(source_bearing, wind_deg, tol=60):
    if wind_deg is None:
        return 0
    plume_to = (wind_deg + 180) % 360
    diff = abs((source_bearing - plume_to + 180) % 360 - 180)
    return 1 if diff <= tol else 0

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ukg-collector/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  fetch error: {e}")
        return None

# ---------------- AQICN/WAQI ----------------
def get_aqicn_stations():
    lat1, lon1 = CITY["lat"]-0.3, CITY["lon"]-0.4
    lat2, lon2 = CITY["lat"]+0.3, CITY["lon"]+0.4
    url = f"https://api.waqi.info/map/bounds/?latlng={lat1},{lon1},{lat2},{lon2}&token={WAQI_TOKEN}"
    data = fetch_json(url)
    stations = []
    if data and data.get("status") == "ok":
        for s in data["data"]:
            stations.append({"uid": s.get("uid"), "lat": s.get("lat"),
                             "lon": s.get("lon"),
                             "name": s.get("station", {}).get("name", "unknown"),
                             "aqi": s.get("aqi")})
    return stations

def get_aqicn_detail(uid):
    url = f"https://api.waqi.info/feed/@{uid}/?token={WAQI_TOKEN}"
    data = fetch_json(url)
    if data and data.get("status") == "ok":
        d = data["data"]; iaqi = d.get("iaqi", {})
        return {"pm25": iaqi.get("pm25", {}).get("v"),
                "pm10": iaqi.get("pm10", {}).get("v"),
                "no2":  iaqi.get("no2", {}).get("v"),
                "so2":  iaqi.get("so2", {}).get("v"),   # metallurgy marker
                "co":   iaqi.get("co", {}).get("v"),
                "o3":   iaqi.get("o3", {}).get("v"),
                "aqi":  d.get("aqi"),
                "time": d.get("time", {}).get("iso")}
    return None

# ---------------- Weather (OpenWeather) ----------------
def get_weather(lat, lon):
    if not OPENWEATHER_KEY:
        return {}
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_KEY}&units=metric"
    d = fetch_json(url)
    if d and d.get("main"):
        return {"temp_c": d["main"].get("temp"), "humidity": d["main"].get("humidity"),
                "pressure": d["main"].get("pressure"),
                "wind_speed": d.get("wind", {}).get("speed"),
                "wind_deg": d.get("wind", {}).get("deg"),
                "weather": d.get("weather", [{}])[0].get("description", "")}
    return {}

# ---------------- Traffic (TomTom) ----------------
def get_traffic(lat, lon):
    if not TOMTOM_KEY:
        return {}
    url = (f"https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
           f"?point={lat},{lon}&key={TOMTOM_KEY}")
    d = fetch_json(url)
    if d and d.get("flowSegmentData"):
        f = d["flowSegmentData"]
        cur = f.get("currentSpeed"); free = f.get("freeFlowSpeed")
        congestion = None
        if cur is not None and free:
            congestion = round(max(0, (1 - cur/free)) * 100, 1)
        return {"current_speed": cur, "free_flow_speed": free,
                "congestion_percent": congestion,
                "current_travel_time": f.get("currentTravelTime"),
                "free_flow_travel_time": f.get("freeFlowTravelTime")}
    return {}

# ---------------- MAIN ----------------
def collect():
    ts = datetime.now(timezone.utc).isoformat()
    cycle_id = int(time.time())
    print(f"[{ts}] Collecting Ust-Kamenogorsk (cycle {cycle_id})")

    weather = get_weather(CITY["lat"], CITY["lon"])
    wind_deg = weather.get("wind_deg")
    heating_season = 1 if datetime.now().month in (10,11,12,1,2,3) else 0

    stations = get_aqicn_stations()
    print(f"  Found {len(stations)} AQICN stations")

    rows = []
    for st in stations:
        if st["lat"] is None or st["lon"] is None:
            continue
        detail = get_aqicn_detail(st["uid"]) or {}
        time.sleep(0.5)
        traffic = get_traffic(st["lat"], st["lon"])
        time.sleep(0.3)

        src_feats = {}
        for sname, s in SOURCES.items():
            d = geodist_km(s["lat"], s["lon"], st["lat"], st["lon"])
            b = bearing_from(s["lat"], s["lon"], st["lat"], st["lon"])
            src_feats[f"dist_{sname}_km"] = round(d, 2)
            src_feats[f"bearing_{sname}"] = round(b, 1)
            src_feats[f"downwind_{sname}"] = is_downwind(b, wind_deg)

        nearest = min(SOURCES.items(),
                      key=lambda kv: geodist_km(kv[1]["lat"], kv[1]["lon"], st["lat"], st["lon"]))
        row = {"timestamp_utc": ts, "cycle_id": cycle_id,
               "station_uid": st["uid"], "station_name": st["name"],
               "lat": st["lat"], "lon": st["lon"],
               "pm25": detail.get("pm25"), "pm10": detail.get("pm10"),
               "no2": detail.get("no2"), "so2": detail.get("so2"),
               "co": detail.get("co"), "o3": detail.get("o3"),
               "aqi": detail.get("aqi"), "aqi_time": detail.get("time"),
               "current_speed": traffic.get("current_speed"),
               "free_flow_speed": traffic.get("free_flow_speed"),
               "congestion_percent": traffic.get("congestion_percent"),
               "nearest_source": nearest[0],
               "nearest_source_dist_km": round(geodist_km(nearest[1]["lat"], nearest[1]["lon"], st["lat"], st["lon"]), 2),
               **src_feats,
               "temp_c": weather.get("temp_c"), "humidity": weather.get("humidity"),
               "pressure": weather.get("pressure"), "wind_speed": weather.get("wind_speed"),
               "wind_deg": wind_deg, "weather_desc": weather.get("weather"),
               "heating_season": heating_season}
        rows.append(row)

    if rows:
        file_exists = os.path.isfile(OUT_CSV)
        with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            if not file_exists:
                w.writeheader()
            w.writerows(rows)
        print(f"  Wrote {len(rows)} rows to {OUT_CSV}")
    else:
        print("  No data collected this cycle")

if __name__ == "__main__":
    if not WAQI_TOKEN:
        print("ERROR: set WAQI_TOKEN (get free token at aqicn.org/data-platform/token/)")
    else:
        collect()
