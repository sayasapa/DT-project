#!/usr/bin/env python3
"""
Multi-city Air Quality Collector (Kazakhstan) — AQICN/WAQI edition
Covers: Ust-Kamenogorsk, Karaganda, Pavlodar, Temirtau, Astana
(Almaty is collected separately by collector.py — kept as-is.)

Architecture note: this replaces the earlier one-script-per-city approach
(collector_ukg.py) with a single config-driven collector. Adding a new city
means adding an entry to CITIES below — no new script/workflow needed.

Station UIDs were found by individually fetching aqicn.org station pages
(not guessed) — see comments per city. A known WAQI quirk: some station UIDs
require the "A" feed-URL prefix instead of "@" (both are tried per UID).

SECRETS (GitHub Actions):
  WAQI_TOKEN       - AQICN/WAQI token (free)      (https://aqicn.org/data-platform/token/)
  OPENWEATHER_KEY  - OpenWeather key (optional)    (https://openweathermap.org/api)
  TOMTOM_KEY       - TomTom traffic key (optional) (https://developer.tomtom.com/)
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request

WAQI_TOKEN      = os.environ.get("WAQI_TOKEN", "")
OPENWEATHER_KEY = os.environ.get("OPENWEATHER_KEY", "")
TOMTOM_KEY      = os.environ.get("TOMTOM_KEY", "")
OUT_CSV         = "data/cities_air_data.csv"

# ---------------- CITY CONFIG ----------------
# "sources" = named industrial/point sources for downwind analysis (optional —
# leave {} for cities without a clear single point source; downwind_* columns
# just won't be produced for those cities' rows).
CITIES = {
    "Ust-Kamenogorsk": {
        "lat": 49.9714, "lon": 82.6059,
        "station_uids": [517390, 517402, 517507],
        "search_keywords": ["Ust-Kamenogorsk", "Oskemen"],
        "sources": {
            "Kazzinc": {"lat": 49.9800, "lon": 82.6170, "type": "lead_zinc_copper_smelter"},
            "UMZ":     {"lat": 49.9550, "lon": 82.6060, "type": "metallurgical_uranium_beryllium"},
            "CHP":     {"lat": 49.9400, "lon": 82.6300, "type": "thermal_power_plant"},
        },
    },
    "Karaganda": {
        "lat": 49.8047, "lon": 73.1094,
        # 114505 (Ситимол) confirmed dead ("no such station") — dropped.
        "station_uids": [506290, 517423],
        "search_keywords": ["Karaganda", "Karagandy"],
        "sources": {},  # no single dominant point source identified yet
    },
    "Pavlodar": {
        "lat": 52.2873, "lon": 76.9674,
        # All 4 originally-found IDs (150022/236602/231715/236608) are
        # AirKaz.org-network stations and consistently returned "no such
        # station" via the public WAQI token — same failure pattern as
        # Ust-Kamenogorsk's AirKaz IDs earlier. Rely on search fallback
        # until a confirmed working Kazhydromet-network UID is found.
        "station_uids": [],
        "search_keywords": ["Pavlodar"],
        "sources": {
            "AluminiumSmelter": {"lat": 52.3130, "lon": 77.0500, "type": "aluminium_smelter_chpp"},
        },
    },
    "Temirtau": {
        "lat": 50.0546, "lon": 72.9648,
        # 36254881 / 3218686 (AirKaz.org) confirmed dead — dropped, keep the
        # one confirmed-responding station.
        "station_uids": [114529],
        "search_keywords": ["Temirtau"],
        "sources": {
            "Qarmet": {"lat": 50.031766, "lon": 72.994863, "type": "integrated_steel_plant"},
        },
    },
    "Astana": {
        "lat": 51.1694, "lon": 71.4491,
        # H10497 (US Embassy), 98310 (sensor.community), 32149779 (AirKaz.org)
        # all failed. Rely entirely on search fallback for now.
        "station_uids": [],
        "search_keywords": ["Astana", "Nur-Sultan"],
        "sources": {},  # traffic/heating profile, not a point-source city
    },
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

def data_age_hours(aqi_time_iso, collected_at_iso):
    if not aqi_time_iso:
        return None
    try:
        t = aqi_time_iso.replace("Z", "+00:00")
        aqi_dt = datetime.fromisoformat(t)
        if aqi_dt.tzinfo is None:
            aqi_dt = aqi_dt.replace(tzinfo=timezone.utc)
        collected_dt = datetime.fromisoformat(collected_at_iso)
        return round((collected_dt - aqi_dt).total_seconds() / 3600, 1)
    except Exception:
        return None

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kz-air-collector/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    fetch error: {e}")
        return None

def fetch_station_feed(uid, city_lat, city_lon):
    """uid can be a bare number (try both '@' and 'A' prefixes) or a string
    that already includes its own prefix (e.g. 'H10497' for embassy feeds)."""
    prefixes = [""] if isinstance(uid, str) and not uid.isdigit() else ("@", "A")
    for prefix in prefixes:
        url = f"https://api.waqi.info/feed/{prefix}{uid}/?token={WAQI_TOKEN}"
        data = fetch_json(url)
        d = data.get("data") if data else None
        looks_valid = isinstance(d, dict) and ("aqi" in d or "iaqi" in d or "city" in d)
        if data and data.get("status") == "ok" and looks_valid:
            print(f"    feed {prefix}{uid}: ok, aqi={d.get('aqi')}, city={d.get('city', {}).get('name')}")
            return d
        else:
            print(f"    feed {prefix}{uid}: not valid. outer_status="
                  f"{data.get('status') if data else 'no_response'} "
                  f"raw={json.dumps(d)[:200] if d is not None else data}")
        time.sleep(0.2)
    return None

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
                "weather_desc": d.get("weather", [{}])[0].get("description", "")}
    return {}

def get_traffic(lat, lon):
    if not TOMTOM_KEY:
        return {}
    url = (f"https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
           f"?point={lat},{lon}&key={TOMTOM_KEY}")
    d = fetch_json(url)
    if d and d.get("flowSegmentData"):
        f = d["flowSegmentData"]
        cur = f.get("currentSpeed"); free = f.get("freeFlowSpeed")
        congestion = round(max(0, (1 - cur/free)) * 100, 1) if (cur is not None and free) else None
        return {"current_speed": cur, "free_flow_speed": free, "congestion_percent": congestion}
    return {}

def search_stations(keyword, seen_uids):
    """Fallback discovery via WAQI's search endpoint — finds real, queryable
    UIDs by city name instead of guessing them from scraped page references."""
    import urllib.parse
    url = f"https://api.waqi.info/search/?token={WAQI_TOKEN}&keyword={urllib.parse.quote(keyword)}"
    data = fetch_json(url)
    found = []
    if data and data.get("status") == "ok":
        for s in data.get("data", []):
            uid = s.get("uid")
            if uid in seen_uids:
                continue
            found.append(uid)
    else:
        print(f"    search '{keyword}' non-ok status: {data.get('status') if data else 'no_response'}")
    return found

# ---------------- MAIN ----------------
def collect_city(city_name, cfg, ts, cycle_id, heating_season):
    print(f"  --- {city_name} ---")
    weather = get_weather(cfg["lat"], cfg["lon"])
    wind_deg = weather.get("wind_deg")

    rows = []
    seen_uids = set()
    for uid in cfg["station_uids"]:
        d = fetch_station_feed(uid, cfg["lat"], cfg["lon"])
        seen_uids.add(uid)
        if not d:
            continue
        rows.append(_build_row(city_name, uid, d, ts, cycle_id, weather, wind_deg,
                                heating_season, cfg))

    # Auto-fallback: if none of the known UIDs worked, search by city name for
    # real, currently-registered stations instead of relying on manually
    # scraped IDs (which can be stale/wrong, as seen with several AirKaz refs).
    if not rows and cfg.get("search_keywords"):
        for kw in cfg["search_keywords"]:
            for uid in search_stations(kw, seen_uids):
                seen_uids.add(uid)
                d = fetch_station_feed(uid, cfg["lat"], cfg["lon"])
                if d:
                    rows.append(_build_row(city_name, uid, d, ts, cycle_id, weather,
                                            wind_deg, heating_season, cfg))
                time.sleep(0.2)
            if rows:
                break

    print(f"    -> {len(rows)} station reading(s)")
    return rows

def _build_row(city_name, uid, d, ts, cycle_id, weather, wind_deg, heating_season, cfg):
    iaqi = d.get("iaqi", {})
    geo = d.get("city", {}).get("geo")
    lat = geo[0] if geo and len(geo) == 2 else cfg["lat"]
    lon = geo[1] if geo and len(geo) == 2 else cfg["lon"]

    row = {
        "city": city_name, "timestamp_utc": ts, "cycle_id": cycle_id,
        "station_uid": uid, "station_name": d.get("city", {}).get("name", "unknown"),
        "lat": lat, "lon": lon,
        "pm25": iaqi.get("pm25", {}).get("v"), "pm10": iaqi.get("pm10", {}).get("v"),
        "no2": iaqi.get("no2", {}).get("v"), "so2": iaqi.get("so2", {}).get("v"),
        "co": iaqi.get("co", {}).get("v"), "o3": iaqi.get("o3", {}).get("v"),
        "aqi": d.get("aqi"), "dominentpol": d.get("dominentpol"),
        "aqi_time": d.get("time", {}).get("iso"),
        "data_age_hours": data_age_hours(d.get("time", {}).get("iso"), ts),
        "temp_c": weather.get("temp_c"), "humidity": weather.get("humidity"),
        "pressure": weather.get("pressure"), "wind_speed": weather.get("wind_speed"),
        "wind_deg": wind_deg, "weather_desc": weather.get("weather_desc"),
        "heating_season": heating_season,
    }

    traffic = get_traffic(lat, lon)
    row["current_speed"] = traffic.get("current_speed")
    row["congestion_percent"] = traffic.get("congestion_percent")

    if cfg["sources"]:
        nearest = min(cfg["sources"].items(),
                      key=lambda kv: geodist_km(kv[1]["lat"], kv[1]["lon"], lat, lon))
        row["nearest_source"] = nearest[0]
        row["nearest_source_dist_km"] = round(
            geodist_km(nearest[1]["lat"], nearest[1]["lon"], lat, lon), 2)
        for sname, s in cfg["sources"].items():
            b = bearing_from(s["lat"], s["lon"], lat, lon)
            row[f"dist_{sname}_km"] = round(geodist_km(s["lat"], s["lon"], lat, lon), 2)
            row[f"bearing_{sname}"] = round(b, 1)
            row[f"downwind_{sname}"] = is_downwind(b, wind_deg)

    return row

def collect():
    ts = datetime.now(timezone.utc).isoformat()
    cycle_id = int(time.time())
    heating_season = 1 if datetime.now().month in (10, 11, 12, 1, 2, 3) else 0
    print(f"[{ts}] Collecting {len(CITIES)} cities (cycle {cycle_id})")

    all_rows = []
    for city_name, cfg in CITIES.items():
        all_rows.extend(collect_city(city_name, cfg, ts, cycle_id, heating_season))

    if not all_rows:
        print("No data collected this cycle")
        return

    # Union of all fieldnames across cities (different cities have different
    # source-feature columns), keep a stable, readable column order.
    base_cols = ["city","timestamp_utc","cycle_id","station_uid","station_name",
                 "lat","lon","pm25","pm10","no2","so2","co","o3","aqi","dominentpol",
                 "aqi_time","data_age_hours","current_speed","congestion_percent",
                 "nearest_source","nearest_source_dist_km"]
    extra_cols = sorted({k for r in all_rows for k in r.keys()} - set(base_cols) -
                         {"temp_c","humidity","pressure","wind_speed","wind_deg",
                          "weather_desc","heating_season"})
    fieldnames = base_cols + extra_cols + ["temp_c","humidity","pressure","wind_speed",
                                            "wind_deg","weather_desc","heating_season"]

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    file_exists = os.path.isfile(OUT_CSV)
    if file_exists:
        with open(OUT_CSV, "r", encoding="utf-8") as f:
            existing_header = f.readline().strip().split(",")
        if existing_header != fieldnames:
            archive_path = OUT_CSV.replace(".csv", f"_archive_{int(time.time())}.csv")
            os.rename(OUT_CSV, archive_path)
            print(f"CSV schema changed, archived old file to {archive_path}")
            file_exists = False

    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerows(all_rows)
    print(f"Wrote {len(all_rows)} rows total to {OUT_CSV}")

if __name__ == "__main__":
    if not WAQI_TOKEN:
        print("ERROR: set WAQI_TOKEN (get free token at aqicn.org/data-platform/token/)")
    else:
        collect()
