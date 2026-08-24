#!/usr/bin/env python3
"""
Ust-Kamenogorsk (Oskemen) Air Quality Collector — IQAir (AirVisual) edition
Industrial city — sources: Kazzinc (lead-zinc), UMZ (Ulba metallurgical), CHP.
Collects: air quality + weather (IQAir/AirVisual "nearest_city", Community tier),
traffic (TomTom), industrial-source context (distance/bearing/downwind per source).
Runs 24/7 via GitHub Actions.

Why IQAir instead of AQICN/WAQI: extensive testing (map/bounds discovery, search,
and direct city-feed lookups) found WAQI has no queryable station for this city
under our token. IQAir has confirmed, working monitoring coverage for
Ust-Kamenogorsk, so we switched providers.

Note on the free "Community" plan: IQAir only exposes a single city-level
aggregate reading per request (PM2.5, PM10, AQI, weather) — the per-station
list/nearest-station endpoints require a paid "Startup" plan. So each collection
cycle here writes ONE row (the city aggregate), not one row per station. SO2,
NO2, CO and O3 are not available on the Community plan.

SECRETS (GitHub Actions):
  IQAIR_KEY   - IQAir/AirVisual API key (Community tier, free)  (https://www.iqair.com/dashboard/api)
  TOMTOM_KEY  - TomTom traffic key (optional)                    (https://developer.tomtom.com/)
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request, urllib.parse

# ---------------- SECRETS (match your GitHub secret names) ----------------
IQAIR_KEY  = os.environ.get("IQAIR_KEY", "")
TOMTOM_KEY = os.environ.get("TOMTOM_KEY", "")
OUT_CSV    = "data/ukg_air_data.csv"

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

# ---------------- IQAir / AirVisual ----------------
def get_iqair_reading(lat, lon):
    url = ("https://api.airvisual.com/v2/nearest_city"
           f"?lat={lat}&lon={lon}&key={urllib.parse.quote(IQAIR_KEY)}")
    data = fetch_json(url)
    if not data:
        print("  IQAir: no response")
        return None
    if data.get("status") != "success":
        print(f"  IQAir non-success status: {data.get('status')} raw={data}")
        return None

    d = data["data"]
    cur = d.get("current", {})
    pol = cur.get("pollution", {})
    wea = cur.get("weather", {})
    coords = (d.get("location", {}) or {}).get("coordinates") or [None, None]  # [lon, lat]

    pm25 = (pol.get("p2") or {}).get("conc")
    pm10 = (pol.get("p1") or {}).get("conc")
    if pm25 is None and pm10 is None:
        # Debug: see exactly what the pollution block contains if concentration
        # fields are missing (Community tier may only expose AQI, not raw µg/m³).
        print(f"  IQAir: pm25/pm10 conc missing. pollution raw={json.dumps(pol)}")

    return {
        "city": d.get("city"), "state": d.get("state"), "country": d.get("country"),
        "lat": coords[1], "lon": coords[0],
        "aqi_us": pol.get("aqius"), "main_us": pol.get("mainus"),
        "aqi_cn": pol.get("aqicn"), "main_cn": pol.get("maincn"),
        "pm25": pm25, "pm10": pm10,
        "pollution_time": pol.get("ts"),
        "temp_c": wea.get("tp"), "pressure": wea.get("pr"), "humidity": wea.get("hu"),
        "wind_speed": wea.get("ws"), "wind_deg": wea.get("wd"), "weather_icon": wea.get("ic"),
    }

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

    reading = get_iqair_reading(CITY["lat"], CITY["lon"])
    if not reading:
        print("  No data collected this cycle")
        return

    lat = reading["lat"] if reading["lat"] is not None else CITY["lat"]
    lon = reading["lon"] if reading["lon"] is not None else CITY["lon"]
    wind_deg = reading["wind_deg"]
    heating_season = 1 if datetime.now().month in (10, 11, 12, 1, 2, 3) else 0

    traffic = get_traffic(lat, lon)

    src_feats = {}
    for sname, s in SOURCES.items():
        d = geodist_km(s["lat"], s["lon"], lat, lon)
        b = bearing_from(s["lat"], s["lon"], lat, lon)
        src_feats[f"dist_{sname}_km"] = round(d, 2)
        src_feats[f"bearing_{sname}"] = round(b, 1)
        src_feats[f"downwind_{sname}"] = is_downwind(b, wind_deg)

    nearest = min(SOURCES.items(),
                  key=lambda kv: geodist_km(kv[1]["lat"], kv[1]["lon"], lat, lon))

    row = {"timestamp_utc": ts, "cycle_id": cycle_id,
           "city": reading["city"], "state": reading["state"], "country": reading["country"],
           "lat": lat, "lon": lon,
           "pm25": reading["pm25"], "pm10": reading["pm10"],
           "aqi_us": reading["aqi_us"], "main_us": reading["main_us"],
           "aqi_cn": reading["aqi_cn"], "main_cn": reading["main_cn"],
           "pollution_time": reading["pollution_time"],
           "current_speed": traffic.get("current_speed"),
           "free_flow_speed": traffic.get("free_flow_speed"),
           "congestion_percent": traffic.get("congestion_percent"),
           "nearest_source": nearest[0],
           "nearest_source_dist_km": round(geodist_km(nearest[1]["lat"], nearest[1]["lon"], lat, lon), 2),
           **src_feats,
           "temp_c": reading["temp_c"], "humidity": reading["humidity"],
           "pressure": reading["pressure"], "wind_speed": reading["wind_speed"],
           "wind_deg": wind_deg, "weather_icon": reading["weather_icon"],
           "heating_season": heating_season}

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    fieldnames = list(row.keys())
    file_exists = os.path.isfile(OUT_CSV)

    if file_exists:
        with open(OUT_CSV, "r", encoding="utf-8") as f:
            existing_header = f.readline().strip().split(",")
        if existing_header != fieldnames:
            # Schema changed (e.g. switched data provider) — don't silently mix
            # incompatible rows under one header. Archive the old file instead.
            archive_path = OUT_CSV.replace(".csv", f"_archive_{int(time.time())}.csv")
            os.rename(OUT_CSV, archive_path)
            print(f"  CSV schema changed, archived old file to {archive_path}")
            file_exists = False

    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerow(row)
    print(f"  Wrote 1 row to {OUT_CSV}")

if __name__ == "__main__":
    if not IQAIR_KEY:
        print("ERROR: set IQAIR_KEY (get free key at https://www.iqair.com/dashboard/api)")
    else:
        collect()
