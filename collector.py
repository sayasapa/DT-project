#!/usr/bin/env python3
"""
Almaty Traffic & Air Quality — data collector (v2, scalable).

Design principles:
  1. REAL DATA ONLY. If required traffic OR air data is missing/incomplete for a
     station in a cycle, that station is SKIPPED and logged. No fallback values.
  2. FUTURE-PROOF. The full raw JSON of every API response is stored alongside
     the parsed fields, so new variables can be derived later without re-collecting.
  3. SCALABLE. Stations are a simple list — add rows to scale to more points.
     Output schema is flat and DB-friendly (easy to move CSV -> PostgreSQL later).

Env vars (GitHub Secrets): TOMTOM_KEY, OPENWEATHER_KEY, WAQI_TOKEN
Outputs:
  data/city_twin_data.csv       parsed, analysis-ready rows (real data only)
  data/raw/<timestamp>.json     full raw API responses per cycle (audit / future use)
"""

import os, sys, csv, json, time, datetime as dt
import requests

# ----------------------------------------------------------------------
# STATIONS — extend this list to scale (id must be unique, lowercase).
# Current 5 corridors from the published study; add more rows for grant-scale.
# ----------------------------------------------------------------------
STATIONS = [
    {"id": "chp",          "name": "CHP (TETs-2 area)",  "lat": 43.29161, "lon": 76.80437, "aq_id": 2812561},
    {"id": "shamiyevast",  "name": "Shamiyeva St.",      "lat": 43.28323, "lon": 76.92753, "aq_id": 2812649},
    {"id": "almaty_ref",   "name": "Almaty (reference)", "lat": 43.25285, "lon": 76.93118, "aq_id": 8876},
    {"id": "school169",    "name": "169 School",         "lat": 43.29713, "lon": 76.86110, "aq_id": 2812691},
    {"id": "school190",    "name": "School 190",         "lat": 43.15476, "lon": 76.89920, "aq_id": 2812753},
    {"id": "ryskulova81",  "name": "Ryskulova 81",       "lat": 43.27865, "lon": 76.90347, "aq_id": 2812749},
    {"id": "ritzpalace",   "name": "Ritz Palace",        "lat": 43.22833, "lon": 76.96028, "aq_id": 2812716},
    {"id": "mamyr3",       "name": "Mamyr-3",            "lat": 43.21431, "lon": 76.85550, "aq_id": 2812717},
    {"id": "nicolas",      "name": "Nicolas",            "lat": 43.19365, "lon": 76.90963, "aq_id": 2812769},
    {"id": "kbtu",         "name": "KBTU",               "lat": 43.25348, "lon": 76.94537, "aq_id": 2812687},
    {"id": "respublika4",  "name": "Respublika 4",       "lat": 43.23680, "lon": 76.94481, "aq_id": 2812784},
    {"id": "gimnaz152",    "name": "Gimnaziya 152",      "lat": 43.30173, "lon": 76.87217, "aq_id": 2819078},
    {"id": "asiafood",     "name": "AsiaFood",           "lat": 43.24283, "lon": 76.82872, "aq_id": 2812809},
    {"id": "qapparov",     "name": "Qapparov St.",       "lat": 43.21065, "lon": 76.94650, "aq_id": 3243331},
    {"id": "kokkainar",    "name": "Kokkainar Micro",    "lat": 43.29076, "lon": 76.84123, "aq_id": 2812651},
    {"id": "alatau",       "name": "Alatau (foothill)",  "lat": 43.17658, "lon": 76.89771, "aq_id": 2812620},
    {"id": "school175",    "name": "School 175",         "lat": 43.19957, "lon": 76.85428, "aq_id": 2812756},
    {"id": "school77",     "name": "School 77",          "lat": 43.21846, "lon": 76.94964, "aq_id": 2812751},
    {"id": "school137",    "name": "School 137",         "lat": 43.31676, "lon": 76.91084, "aq_id": 2812563},
    {"id": "school192",    "name": "School 192",         "lat": 43.17203, "lon": 76.85234, "aq_id": 2812775},
    {"id": "ippodrom",     "name": "Ippodrom",           "lat": 43.30778, "lon": 76.92324, "aq_id": 3124030},
    {"id": "ekopost",      "name": "EkoPost",            "lat": 43.24966, "lon": 76.80560, "aq_id": 2812576},
    {"id": "kotelnikova",  "name": "Kotelnikova St.",    "lat": 43.31040, "lon": 76.94217, "aq_id": 2812792},
    {"id": "hospital7",    "name": "Hospital-7",         "lat": 43.23396, "lon": 76.80013, "aq_id": 2812726},
    {"id": "school187",    "name": "School 187",         "lat": 43.18579, "lon": 76.82848, "aq_id": 2812690},
]

OUTPUT_CSV = os.path.join("data", "city_twin_data.csv")
RAW_DIR    = os.path.join("data", "raw")

ALMATY_POPULATION = 2_359_000
# --- Source-apportionment helpers (CHP / heating season / wind) ---
CHP_LAT, CHP_LON = 43.29161, 76.80437   # TETs-2 area reference point
import math as _math

def _dist_km(la, lo, la2, lo2):
    # rough equirectangular distance in km
    x = (lo2 - lo) * _math.cos(_math.radians((la + la2) / 2)) * 111.32
    y = (la2 - la) * 111.32
    return round(_math.hypot(x, y), 2)

def _bearing_deg(la, lo, la2, lo2):
    # bearing FROM (la,lo) TO (la2,lo2), degrees 0-360
    dlon = _math.radians(lo2 - lo)
    y = _math.sin(dlon) * _math.cos(_math.radians(la2))
    x = (_math.cos(_math.radians(la)) * _math.sin(_math.radians(la2))
         - _math.sin(_math.radians(la)) * _math.cos(_math.radians(la2)) * _math.cos(dlon))
    return (_math.degrees(_math.atan2(y, x)) + 360) % 360

def _downwind_of_chp(station_lat, station_lon, wind_deg):
    """True if the station is roughly downwind of CHP (within +-45 deg)."""
    if wind_deg is None or wind_deg == "":
        return ""
    # wind_deg = direction wind blows FROM. Direction plume travels = wind_deg+180.
    plume_to = (float(wind_deg) + 180) % 360
    # bearing from CHP to station:
    b = _bearing_deg(CHP_LAT, CHP_LON, station_lat, station_lon)
    diff = abs((plume_to - b + 180) % 360 - 180)
    return 1 if diff <= 45 else 0

MAX_DENSITY = 200
SEGMENT_LENGTH_KM = 1.0
EMISSION_FACTOR = 0.00012
REQUEST_TIMEOUT = 20
RETRIES = 2

CSV_FIELDS = [
    "timestamp_utc", "cycle_id", "station_id", "station_name", "district", "road", "lat", "lon",
    # traffic
    "speed_kmh", "free_flow_speed_kmh", "current_travel_time_s", "free_flow_travel_time_s",
    "density_cars_per_km", "cars_per_hour", "congestion_percent", "co2_kg_per_hour",
    # air
    "pm25_ugm3", "pm10_ugm3", "no2_ugm3", "o3_ugm3", "co_ugm3", "so2_ugm3", "nh3_ugm3", "air_index",
    "waqi_aqi",
    # weather (extended — for inversion / PBL proxies)
    "temperature_c", "feels_like_c", "humidity_percent", "pressure_hpa",
    "wind_speed_ms", "wind_deg", "clouds_percent", "visibility_m", "weather_desc",
    # derived
    "population_exposure_index",
    # source-apportionment features
    "dist_to_chp_km", "heating_season", "downwind_of_chp",
    # provenance
    "traffic_source", "air_source", "weather_source",
]


def _get(url, params):
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            print(f"    HTTP {r.status_code}: {url}")
            return None
        except requests.RequestException as e:
            print(f"    request error ({attempt+1}/{RETRIES+1}): {e}")
            time.sleep(2)
    return None


def fetch_traffic(lat, lon, key):
    url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
    data = _get(url, {"point": f"{lat},{lon}", "key": key})
    if not data or "flowSegmentData" not in data:
        return None, data
    fsd = data["flowSegmentData"]
    speed, free = fsd.get("currentSpeed"), fsd.get("freeFlowSpeed")
    if speed is None or free is None or free == 0:
        return None, data
    density = MAX_DENSITY * max(0.0, 1.0 - speed / free)
    vph = density * speed
    congestion = max(0.0, min(100.0, (1.0 - speed / free) * 100.0))
    co2 = vph * SEGMENT_LENGTH_KM * EMISSION_FACTOR * 1000
    parsed = {
        "speed_kmh": round(speed, 2), "free_flow_speed_kmh": round(free, 2),
        "current_travel_time_s": fsd.get("currentTravelTime"),
        "free_flow_travel_time_s": fsd.get("freeFlowTravelTime"),
        "density_cars_per_km": round(density, 2), "cars_per_hour": round(vph, 1),
        "congestion_percent": round(congestion, 1), "co2_kg_per_hour": round(co2, 1),
        "traffic_source": "tomtom",
    }
    return parsed, data


def fetch_air(lat, lon, ow_key, waqi_token):
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    data = _get(url, {"lat": lat, "lon": lon, "appid": ow_key})
    if not data or not data.get("list"):
        return None, data
    comp = data["list"][0].get("components", {})
    req = ["pm2_5", "pm10", "no2", "o3", "co"]
    if any(comp.get(k) is None for k in req):
        return None, data
    air_index = (0.34*comp["pm2_5"] + 0.18*comp["pm10"] + 0.22*comp["no2"]
                 + 0.08*comp["o3"] + 18*(comp["co"]/1000.0))
    air_source, waqi_aqi, waqi_raw = "openweathermap", "", None
    if waqi_token:
        w = _get(f"https://api.waqi.info/feed/geo:{lat};{lon}/", {"token": waqi_token})
        if w and w.get("status") == "ok":
            air_source = "openweathermap+waqi"
            waqi_aqi = w.get("data", {}).get("aqi", "")
            waqi_raw = w
    parsed = {
        "pm25_ugm3": round(comp["pm2_5"], 2), "pm10_ugm3": round(comp["pm10"], 2),
        "no2_ugm3": round(comp["no2"], 2), "o3_ugm3": round(comp["o3"], 2),
        "co_ugm3": round(comp["co"], 2), "so2_ugm3": comp.get("so2", ""),
        "nh3_ugm3": comp.get("nh3", ""), "air_index": round(air_index, 2),
        "waqi_aqi": waqi_aqi, "air_source": air_source,
    }
    return parsed, {"openweather": data, "waqi": waqi_raw}


def fetch_weather(lat, lon, ow_key):
    url = "https://api.openweathermap.org/data/2.5/weather"
    data = _get(url, {"lat": lat, "lon": lon, "appid": ow_key, "units": "metric"})
    if not data or "main" not in data:
        return None, data
    m, wind = data["main"], data.get("wind", {})
    parsed = {
        "temperature_c": m.get("temp"), "feels_like_c": m.get("feels_like"),
        "humidity_percent": m.get("humidity"), "pressure_hpa": m.get("pressure"),
        "wind_speed_ms": wind.get("speed"), "wind_deg": wind.get("deg"),
        "clouds_percent": data.get("clouds", {}).get("all"),
        "visibility_m": data.get("visibility"),
        "weather_desc": (data.get("weather", [{}])[0].get("description", "")),
        "weather_source": "openweathermap",
    }
    return parsed, data


def exposure_index(air, traffic):
    p_pop = ALMATY_POPULATION / 2_500_000.0
    p_poll = (0.42*air["pm25_ugm3"]/35 + 0.18*air["pm10_ugm3"]/75
              + 0.20*air["no2_ugm3"]/100 + 0.12*air["co_ugm3"]/1200
              + 0.08*traffic["congestion_percent"]/100)
    return round(70 * p_poll * (0.62 + 0.38*p_pop), 2)


def main():
    tomtom = os.environ.get("TOMTOM_KEY")
    openweather = os.environ.get("OPENWEATHER_KEY")
    waqi = os.environ.get("WAQI_TOKEN", "")
    if not tomtom or not openweather:
        print("ERROR: TOMTOM_KEY and OPENWEATHER_KEY required."); sys.exit(1)

    os.makedirs("data", exist_ok=True); os.makedirs(RAW_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    cycle_id = now.strftime("%Y%m%dT%H%M%SZ")
    ts_iso = now.isoformat()
    new_file = not os.path.exists(OUTPUT_CSV)

    rows, raw_bundle, kept, skipped = [], {}, 0, 0
    for st in STATIONS:
        print(f"[{st['name']}] polling...")
        traffic, traffic_raw = fetch_traffic(st["lat"], st["lon"], tomtom)
        air, air_raw = fetch_air(st["lat"], st["lon"], openweather, waqi)
        raw_bundle[st["id"]] = {"traffic": traffic_raw, "air": air_raw}

        if traffic is None or air is None:
            skipped += 1
            miss = [n for n, v in (("traffic", traffic), ("air", air)) if v is None]
            print(f"    SKIP — missing real {'/'.join(miss)}")
            continue

        weather, weather_raw = fetch_weather(st["lat"], st["lon"], openweather)
        raw_bundle[st["id"]]["weather"] = weather_raw
        if weather is None:
            weather = {k: "" for k in ["temperature_c","feels_like_c","humidity_percent",
                       "pressure_hpa","wind_speed_ms","wind_deg","clouds_percent",
                       "visibility_m","weather_desc"]}
            weather["weather_source"] = "unavailable"

        row = {"timestamp_utc": ts_iso, "cycle_id": cycle_id, "station_id": st["id"],
               "station_name": st["name"], "district": st["district"], "road": st["road"],
               "lat": st["lat"], "lon": st["lon"],
               "population_exposure_index": exposure_index(air, traffic),
               "dist_to_chp_km": _dist_km(st["lat"], st["lon"], CHP_LAT, CHP_LON),
               "heating_season": 1 if now.month in (10,11,12,1,2,3) else 0,
               "downwind_of_chp": _downwind_of_chp(st["lat"], st["lon"], weather.get("wind_deg"))}
        row.update(traffic); row.update(air); row.update(weather)
        rows.append(row); kept += 1
        print(f"    OK — pm25={air['pm25_ugm3']} veh/h={traffic['cars_per_hour']} T={weather.get('temperature_c')}")

    # always store raw bundle (even if some skipped) for audit/future variables
    try:
        with open(os.path.join(RAW_DIR, f"{cycle_id}.json"), "w") as f:
            json.dump({"cycle_id": cycle_id, "timestamp_utc": ts_iso, "stations": raw_bundle},
                      f, ensure_ascii=False)
    except Exception as e:
        print(f"    (raw save warning: {e})")

    if not rows:
        print(f"Cycle {cycle_id}: 0 rows (all {skipped} skipped)."); return

    with open(OUTPUT_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file: writer.writeheader()
        for row in rows: writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    print(f"Cycle {cycle_id}: {kept} rows written, {skipped} skipped.")


if __name__ == "__main__":
    main()
