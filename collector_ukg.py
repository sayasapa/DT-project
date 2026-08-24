!/usr/bin/env python3
"""
Ust-Kamenogorsk (Oskemen) Air Quality Collector — AQICN/WAQI edition (v3)
Industrial city — sources: Kazzinc (lead-zinc), UMZ (Ulba metallurgical), CHP.
Collects: air quality (AQICN/WAQI — PM2.5, PM10, NO2, SO2, CO, O3, AQI),
weather bundled in the same feed, traffic (TomTom), industrial-source context
(distance/bearing/downwind per source). Runs 24/7 via GitHub Actions.

Why this version: earlier attempts using WAQI's map/bounds/ discovery and
search/ endpoint found 0 stations for this city. It turns out Ust-Kamenogorsk
IS covered — by the AirKaz.org sensor network (the same network used for
Almaty) — but two things were wrong before:
  1. The station UIDs we'd guessed were simply incorrect.
  2. Some WAQI station UIDs require the feed URL prefix "A" instead of "@"
     (a known quirk: /feed/A114571/ works where /feed/@114571/ returns
     "Unknown ID" for certain stations). We now try both prefixes.

Real AirKaz.org station UIDs for Oskemen/Ust-Kamenogorsk (found via aqicn.org
station pages): 114562 (Электротовары), 114571 (ЦДК), 517507 (М.Тынышпаев 126),
517498 (Өтепов 37), 519514 (Широкая 44).

SECRETS (GitHub Actions):
  WAQI_TOKEN  - AQICN/WAQI token (free)     (https://aqicn.org/data-platform/token/)
  TOMTOM_KEY  - TomTom traffic key (optional) (https://developer.tomtom.com/)
"""
import os, csv, math, time, json
from datetime import datetime, timezone
import urllib.request

# ---------------- SECRETS (match your GitHub secret names) ----------------
WAQI_TOKEN  = os.environ.get("WAQI_TOKEN", "")
TOMTOM_KEY  = os.environ.get("TOMTOM_KEY", "")
OUT_CSV     = "data/ukg_air_data.csv"

# City center (Ust-Kamenogorsk / Oskemen)
CITY = {"name": "Ust-Kamenogorsk", "lat": 49.9714, "lon": 82.6059}

# Industrial pollution sources (verified coordinates)
SOURCES = {
    "Kazzinc": {"lat": 49.9800, "lon": 82.6170, "type": "lead_zinc_copper_smelter"},
    "UMZ":     {"lat": 49.9550, "lon": 82.6060, "type": "metallurgical_uranium_beryllium"},
    "CHP":     {"lat": 49.9400, "lon": 82.6300, "type": "thermal_power_plant"},
}

# Real AirKaz.org / AirNet station UIDs confirmed to exist for this city
# (verified via their aqicn.org station pages, not guessed).
KNOWN_STATION_UIDS = [114562, 114571, 517507, 517498, 519514]

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

def fetch_station_feed(uid):
    """Try both the '@' and 'A' UID prefixes — WAQI requires 'A' for some
    stations (observed quirk), '@' returns 'Unknown ID' for those same UIDs."""
    for prefix in ("@", "A"):
        url = f"https://api.waqi.info/feed/{prefix}{uid}/?token={WAQI_TOKEN}"
        data = fetch_json(url)
        if data and data.get("status") == "ok":
            return data["data"]
        else:
            status = data.get("status") if data else "no_response"
            msg = data.get("data") if data else None
            print(f"  feed {prefix}{uid}: non-ok status={status} msg={msg}")
        time.sleep(0.2)
    return None

# ---------------- AQICN/WAQI ----------------
def get_aqicn_stations():
    stations = []
    for uid in KNOWN_STATION_UIDS:
        d = fetch_station_feed(uid)
        if not d:
            continue
        iaqi = d.get("iaqi", {})
        geo = d.get("city", {}).get("geo")
        lat = geo[0] if geo and len(geo) == 2 else CITY["lat"]
        lon = geo[1] if geo and len(geo) == 2 else CITY["lon"]
        stations.append({
            "uid": uid, "lat": lat, "lon": lon,
            "name": d.get("city", {}).get("name", "unknown"),
            "pm25": iaqi.get("pm25", {}).get("v"),
            "pm10": iaqi.get("pm10", {}).get("v"),
            "no2":  iaqi.get("no2", {}).get("v"),
            "so2":  iaqi.get("so2", {}).get("v"),   # metallurgy marker
            "co":   iaqi.get("co", {}).get("v"),
            "o3":   iaqi.get("o3", {}).get("v"),
            "temp_c":    iaqi.get("t", {}).get("v"),
            "humidity":  iaqi.get("h", {}).get("v"),
            "pressure":  iaqi.get("p", {}).get("v"),
            "wind_speed": iaqi.get("w", {}).get("v"),
            "wind_deg":  iaqi.get("wd", {}).get("v"),
            "aqi":  d.get("aqi"),
            "dominentpol": d.get("dominentpol"),
            "aqi_time": d.get("time", {}).get("iso"),
        })
        time.sleep(0.3)
    return stations

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

    heating_season = 1 if datetime.now().month in (10, 11, 12, 1, 2, 3) else 0

    stations = get_aqicn_stations()
    print(f"  Found {len(stations)} AQICN stations")

    rows = []
    for st in stations:
        lat, lon = st["lat"], st["lon"]
        wind_deg = st.get("wind_deg")
        traffic = get_traffic(lat, lon)
        time.sleep(0.3)

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
               "station_uid": st["uid"], "station_name": st["name"],
               "lat": lat, "lon": lon,
               "pm25": st.get("pm25"), "pm10": st.get("pm10"),
               "no2": st.get("no2"), "so2": st.get("so2"),
               "co": st.get("co"), "o3": st.get("o3"),
               "aqi": st.get("aqi"), "dominentpol": st.get("dominentpol"),
               "aqi_time": st.get("aqi_time"),
               "current_speed": traffic.get("current_speed"),
               "free_flow_speed": traffic.get("free_flow_speed"),
               "congestion_percent": traffic.get("congestion_percent"),
               "nearest_source": nearest[0],
               "nearest_source_dist_km": round(geodist_km(nearest[1]["lat"], nearest[1]["lon"], lat, lon), 2),
               **src_feats,
               "temp_c": st.get("temp_c"), "humidity": st.get("humidity"),
               "pressure": st.get("pressure"), "wind_speed": st.get("wind_speed"),
               "wind_deg": wind_deg,
               "heating_season": heating_season}
        rows.append(row)

    if not rows:
        print("  No data collected this cycle")
        return

    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    fieldnames = list(rows[0].keys())
    file_exists = os.path.isfile(OUT_CSV)

    if file_exists:
        with open(OUT_CSV, "r", encoding="utf-8") as f:
            existing_header = f.readline().strip().split(",")
        if existing_header != fieldnames:
            # Schema changed (e.g. switched data provider) — archive the old
            # file instead of silently mixing incompatible rows under one header.
            archive_path = OUT_CSV.replace(".csv", f"_archive_{int(time.time())}.csv")
            os.rename(OUT_CSV, archive_path)
            print(f"  CSV schema changed, archived old file to {archive_path}")
            file_exists = False

    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerows(rows)
    print(f"  Wrote {len(rows)} rows to {OUT_CSV}")

if __name__ == "__main__":
    if not WAQI_TOKEN:
        print("ERROR: set WAQI_TOKEN (get free token at aqicn.org/data-platform/token/)")
    else:
        collect()
