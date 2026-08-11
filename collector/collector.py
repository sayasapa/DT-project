#!/usr/bin/env python3
"""
Almaty Traffic & Air Quality — data collector.

Design principle (lesson from the prototype study):
    Only REAL data are written. If any required API response is missing or
    incomplete for a station in a given cycle, that station is SKIPPED and
    logged — no fallback / placeholder values are ever written.

Reads API keys from environment variables (GitHub Secrets):
    TOMTOM_KEY, OPENWEATHER_KEY, WAQI_TOKEN

Appends one row per station-time to data/city_twin_data.csv
"""

import os
import sys
import csv
import time
import math
import datetime as dt
import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------
STATIONS = [
    {"id": "central", "name": "Central", "lat": 43.2387, "lon": 76.9456,
     "road": "Abay Ave / Zheltoksan St"},
    {"id": "north",   "name": "North",   "lat": 43.2855, "lon": 76.9342,
     "road": "Northern Ring Road"},
    {"id": "east",    "name": "East",    "lat": 43.2569, "lon": 77.0114,
     "road": "Raiymbek Ave / eastern entrance"},
    {"id": "south",   "name": "South",   "lat": 43.2053, "lon": 76.9038,
     "road": "Al-Farabi Ave / Navoi St"},
    {"id": "west",    "name": "West",    "lat": 43.2461, "lon": 76.8528,
     "road": "Ryskulov Ave / western entrance"},
]

OUTPUT_FILE = os.path.join("data", "city_twin_data.csv")

# City-level constant used only for the exposure index (not a live value)
ALMATY_POPULATION = 2_359_000

# Emission-model constants (same simple macroscopic model as the study)
MAX_DENSITY = 200          # veh/km, jam density assumption
SEGMENT_LENGTH_KM = 1.0    # nominal segment length
EMISSION_FACTOR = 0.00012  # kg CO2 per (veh * km) — nominal

REQUEST_TIMEOUT = 20       # seconds per API call
RETRIES = 2                # quick retries before giving up on a call

CSV_FIELDS = [
    "timestamp_utc", "station_id", "station_name", "road", "lat", "lon",
    # traffic
    "speed_kmh", "free_flow_speed_kmh", "density_cars_per_km",
    "cars_per_hour", "congestion_percent", "co2_kg_per_hour",
    # air
    "pm25_ugm3", "pm10_ugm3", "no2_ugm3", "o3_ugm3", "co_ugm3", "air_index",
    # weather
    "temperature_c", "humidity_percent", "wind_speed_ms",
    # derived
    "population_exposure_index",
    # provenance
    "traffic_source", "air_source", "weather_source",
]


# ----------------------------------------------------------------------
# API CALLS  (each returns dict on success, or None on failure)
# ----------------------------------------------------------------------
def _get(url, params):
    """GET with small retry loop. Returns parsed JSON or None."""
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # 4xx/5xx: log and give up (don't hammer)
            print(f"    HTTP {r.status_code} for {url}")
            return None
        except requests.RequestException as e:
            print(f"    request error ({attempt+1}/{RETRIES+1}): {e}")
            time.sleep(2)
    return None


def fetch_traffic(lat, lon, key):
    """TomTom Flow Segment Data. Returns dict or None (never fabricates)."""
    url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
    data = _get(url, {"point": f"{lat},{lon}", "key": key})
    if not data or "flowSegmentData" not in data:
        return None
    fsd = data["flowSegmentData"]
    speed = fsd.get("currentSpeed")
    free = fsd.get("freeFlowSpeed")
    if speed is None or free is None or free == 0:
        return None
    # macroscopic model
    density = MAX_DENSITY * max(0.0, 1.0 - speed / free)
    vehicles_per_hour = density * speed
    congestion = max(0.0, min(100.0, (1.0 - speed / free) * 100.0))
    co2 = vehicles_per_hour * SEGMENT_LENGTH_KM * EMISSION_FACTOR * 1000  # kg/h scale
    return {
        "speed_kmh": round(speed, 2),
        "free_flow_speed_kmh": round(free, 2),
        "density_cars_per_km": round(density, 2),
        "cars_per_hour": round(vehicles_per_hour, 1),
        "congestion_percent": round(congestion, 1),
        "co2_kg_per_hour": round(co2, 1),
        "traffic_source": "tomtom",
    }


def fetch_air(lat, lon, ow_key, waqi_token):
    """OpenWeather Air Pollution (primary) + WAQI (context). Returns dict or None."""
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    data = _get(url, {"lat": lat, "lon": lon, "appid": ow_key})
    if not data or not data.get("list"):
        return None
    comp = data["list"][0].get("components", {})
    pm25 = comp.get("pm2_5")
    pm10 = comp.get("pm10")
    no2 = comp.get("no2")
    o3 = comp.get("o3")
    co = comp.get("co")
    if pm25 is None or pm10 is None or no2 is None or o3 is None or co is None:
        return None
    air_index = (0.34 * pm25 + 0.18 * pm10 + 0.22 * no2
                 + 0.08 * o3 + 18 * (co / 1000.0))
    air_source = "openweathermap"

    # WAQI is optional context; its absence does NOT invalidate the record
    if waqi_token:
        w = _get(f"https://api.waqi.info/feed/geo:{lat};{lon}/",
                 {"token": waqi_token})
        if w and w.get("status") == "ok":
            air_source = "openweathermap+waqi"

    return {
        "pm25_ugm3": round(pm25, 2),
        "pm10_ugm3": round(pm10, 2),
        "no2_ugm3": round(no2, 2),
        "o3_ugm3": round(o3, 2),
        "co_ugm3": round(co, 2),
        "air_index": round(air_index, 2),
        "air_source": air_source,
    }


def fetch_weather(lat, lon, ow_key):
    """OpenWeather current weather. Returns dict or None (optional layer)."""
    url = "https://api.openweathermap.org/data/2.5/weather"
    data = _get(url, {"lat": lat, "lon": lon, "appid": ow_key, "units": "metric"})
    if not data or "main" not in data:
        return None
    return {
        "temperature_c": data["main"].get("temp"),
        "humidity_percent": data["main"].get("humidity"),
        "wind_speed_ms": data.get("wind", {}).get("speed"),
        "weather_source": "openweathermap",
    }


def exposure_index(air, traffic):
    """Population Exposure Index (proxy). Requires real air + traffic."""
    pm25 = air["pm25_ugm3"]; pm10 = air["pm10_ugm3"]
    no2 = air["no2_ugm3"]; co = air["co_ugm3"]
    congestion = traffic["congestion_percent"]
    p_pop = ALMATY_POPULATION / 2_500_000.0
    p_poll = (0.42 * pm25 / 35 + 0.18 * pm10 / 75 + 0.20 * no2 / 100
              + 0.12 * co / 1200 + 0.08 * congestion / 100)
    return round(70 * p_poll * (0.62 + 0.38 * p_pop), 2)


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    tomtom = os.environ.get("TOMTOM_KEY")
    openweather = os.environ.get("OPENWEATHER_KEY")
    waqi = os.environ.get("WAQI_TOKEN", "")  # optional

    if not tomtom or not openweather:
        print("ERROR: TOMTOM_KEY and OPENWEATHER_KEY must be set.")
        sys.exit(1)

    os.makedirs("data", exist_ok=True)
    new_file = not os.path.exists(OUTPUT_FILE)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    rows = []
    kept, skipped = 0, 0

    for st in STATIONS:
        print(f"[{st['name']}] polling...")
        traffic = fetch_traffic(st["lat"], st["lon"], tomtom)
        air = fetch_air(st["lat"], st["lon"], openweather, waqi)

        # CORE RULE: both traffic and air must be real, else SKIP (no fallback)
        if traffic is None or air is None:
            skipped += 1
            reason = []
            if traffic is None: reason.append("traffic")
            if air is None: reason.append("air")
            print(f"    SKIP — missing real {'/'.join(reason)} data")
            continue

        weather = fetch_weather(st["lat"], st["lon"], openweather) or {
            "temperature_c": "", "humidity_percent": "",
            "wind_speed_ms": "", "weather_source": "unavailable",
        }

        row = {
            "timestamp_utc": now,
            "station_id": st["id"], "station_name": st["name"],
            "road": st["road"], "lat": st["lat"], "lon": st["lon"],
            "population_exposure_index": exposure_index(air, traffic),
        }
        row.update(traffic); row.update(air); row.update(weather)
        rows.append(row)
        kept += 1
        print(f"    OK — pm25={air['pm25_ugm3']} veh/h={traffic['cars_per_hour']}")

    if not rows:
        print(f"Cycle complete: 0 rows written (all {skipped} skipped). Nothing to commit.")
        return

    with open(OUTPUT_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDS})

    print(f"Cycle complete: {kept} rows written, {skipped} skipped.")


if __name__ == "__main__":
    main()
