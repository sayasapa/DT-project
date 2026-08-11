# Almaty Traffic & Air Quality — 24/7 Data Collector

A lightweight background collector that runs on **GitHub Actions** (free), polls
live traffic and air-quality APIs every 15 minutes, and appends **only real data**
to `data/city_twin_data.csv`.

**Key principle:** if any required API response is missing or incomplete for a
station in a cycle, that station is **skipped and logged** — no fallback or
placeholder values are ever written. This avoids the synthetic-data problem that
can corrupt later analysis.

---

## What you need (3 free API keys)

| Service | Sign up | Env variable |
|---|---|---|
| TomTom Traffic | https://developer.tomtom.com | `TOMTOM_KEY` |
| OpenWeather (Air Pollution + Weather) | https://openweathermap.org/api | `OPENWEATHER_KEY` |
| WAQI (optional context) | https://aqicn.org/data-platform/token | `WAQI_TOKEN` |

WAQI is optional — the collector still works without it (records are just
labelled `openweathermap` instead of `openweathermap+waqi`).

---

## Setup (one time, ~10 minutes)

### 1. Create a GitHub repository
- Go to https://github.com/new
- Name it e.g. `almaty-city-twin`, set it **Private** (recommended), create.

### 2. Upload these files
Upload the whole folder contents, keeping the structure:
```
collector.py
requirements.txt
README.md
.github/workflows/collect.yml
data/.gitkeep
```
(Use "Add file → Upload files", or `git push` if you use git locally.)

### 3. Add your API keys as Secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add three secrets (names must match exactly):
- `TOMTOM_KEY` = your TomTom key
- `OPENWEATHER_KEY` = your OpenWeather key
- `WAQI_TOKEN` = your WAQI token (or skip if not using WAQI)

**Never put keys in the code.** They live only in Secrets.

### 4. Enable Actions
- Open the **Actions** tab. If prompted, click **"I understand… enable workflows"**.
- Select **"Collect city-twin data"** → **Run workflow** to test it once immediately.
- Check the run log: you should see `OK — pm25=… veh/h=…` lines and
  `Cycle complete: N rows written`.
- After the test, it will keep running automatically every ~15 minutes.

That's it. Data accumulate in `data/city_twin_data.csv` in the repo.

---

## Notes & tuning

- **Interval.** Set in `.github/workflows/collect.yml` (`cron: "*/15 * * * *"`).
  15 min × 5 stations = ~480 calls/API/day — comfortably within free limits.
  GitHub's scheduler is best-effort and may delay runs under load; this is normal.
- **Data location.** `data/city_twin_data.csv`. Download it any time from the repo,
  or clone the repo, for offline analysis.
- **Coverage.** Each row records `traffic_source` / `air_source`. If a station is
  frequently skipped, its API coverage at that point is poor (as happened for some
  corridors in the study) — visible directly from which rows exist.
- **Columns** match the format used in the study, so your existing analysis code
  and notebooks work on this file directly.

---

## Later: connect a dashboard (Layer 4)

The CSV in this repo is the single source of truth. A visualization layer can be
added later without touching the collector — e.g.:
- a static site that reads the raw CSV URL
  (`https://raw.githubusercontent.com/<user>/<repo>/main/data/city_twin_data.csv`),
- or a Streamlit / Dash / React app that loads the same file.

Because acquisition (this collector) and visualization are decoupled, the dashboard
can never inject fallback values into the scientific dataset.

---

## Architecture (digital-twin layers)

1. **Acquisition (this repo, 24/7):** live API polling → real records only.
2. **State store:** the accumulating CSV (or a database later).
3. **Analytics / ML:** offline notebooks over the accumulated state.
4. **Visualization (optional):** dashboard on top of the same data.

Real-time visualization is one interface to the twin, not a requirement — the
twin's core is the continuous acquisition and the evolving digital state.
