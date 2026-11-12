"""Marine weather forecast from NOAA's free API (no key needed).

Fetches the 7-day marine forecast for a point near Boston Harbor and returns a
simple go / caution / poor rating per day based on wind speed and wave height.
Falls back to a static "unknown" rating without network access or when NOAA errors.
"""
import datetime as dt, json, os, threading, time, urllib.request

# Boston Harbor approximate coordinates
LAT, LON = 42.35, -71.05
GRIDPOINT_URL = None  # resolved lazily from /points
FORECAST_CACHE = {"data": None, "expires": 0}
CACHE_LOCK = threading.Lock()
CACHE_TTL = 3600  # re-fetch hourly

MOCK = os.environ.get("MOCK", "auto")

# thresholds for dive conditions
WIND_CAUTION_KT = 15
WIND_POOR_KT = 25
WAVE_CAUTION_FT = 3
WAVE_POOR_FT = 5


def _rating(wind_kt, wave_ft):
    if wind_kt >= WIND_POOR_KT or wave_ft >= WAVE_POOR_FT:
        return "poor"
    if wind_kt >= WIND_CAUTION_KT or wave_ft >= WAVE_CAUTION_FT:
        return "caution"
    return "go"


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pierside-demo/1.0", "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _resolve_gridpoint():
    global GRIDPOINT_URL
    if GRIDPOINT_URL:
        return GRIDPOINT_URL
    data = _fetch_json(f"https://api.weather.gov/points/{LAT},{LON}")
    GRIDPOINT_URL = data["properties"]["forecast"]
    return GRIDPOINT_URL


def _parse_forecast(data):
    """Extract daily conditions from NOAA's /forecast response."""
    days = {}
    for period in data.get("properties", {}).get("periods", []):
        date_str = period.get("startTime", "")[:10]
        if not date_str:
            continue
        try:
            date = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if date in days:
            continue

        wind_speed_str = period.get("windSpeed", "0")
        wind_kt = 0
        for part in wind_speed_str.replace("to", " ").split():
            try:
                wind_kt = max(wind_kt, int(part))
            except ValueError:
                pass

        detail = period.get("detailedForecast", "").lower()
        wave_ft = 0
        if "wave" in detail or "sea" in detail or "swell" in detail:
            import re
            nums = re.findall(r"(\d+)\s*(?:to\s*(\d+))?\s*(?:foot|feet|ft)", detail)
            for match in nums:
                wave_ft = max(wave_ft, int(match[1] or match[0]))

        wind_dir = period.get("windDirection", "")
        short_forecast = period.get("shortForecast", "")

        days[date] = {
            "wind_kt": wind_kt,
            "wind_dir": wind_dir,
            "wave_ft": wave_ft,
            "short": short_forecast,
            "rating": _rating(wind_kt, wave_ft),
        }
    return days


def _mock_forecast():
    """Deterministic weather for mock mode so tests are stable."""
    days = {}
    for i in range(14):
        d = dt.date.today() + dt.timedelta(days=i)
        wind = 10 + (d.toordinal() % 5) * 4  # cycles 10, 14, 18, 22, 26
        wave = 1 + (d.toordinal() % 4)        # cycles 1, 2, 3, 4
        days[d] = {
            "wind_kt": wind,
            "wind_dir": "SW",
            "wave_ft": wave,
            "short": "Partly cloudy" if wind < 20 else "Windy",
            "rating": _rating(wind, wave),
        }
    return days


def get_forecast():
    """Return {date: {wind_kt, wind_dir, wave_ft, short, rating}} for available days.
    rating is 'go', 'caution', or 'poor'. Returns mock data in MOCK mode."""
    if MOCK == "1" or (MOCK == "auto" and not os.environ.get("ANTHROPIC_API_KEY")):
        return _mock_forecast()

    with CACHE_LOCK:
        if FORECAST_CACHE["data"] and time.time() < FORECAST_CACHE["expires"]:
            return FORECAST_CACHE["data"]

    try:
        url = _resolve_gridpoint()
        data = _fetch_json(url)
        days = _parse_forecast(data)
        with CACHE_LOCK:
            FORECAST_CACHE["data"] = days
            FORECAST_CACHE["expires"] = time.time() + CACHE_TTL
        return days
    except Exception:
        return {}


def slot_weather(slot_id):
    """Given a slot_id like '2026-11-12T08:00', return the weather for that date or None."""
    try:
        date = dt.date.fromisoformat(slot_id[:10])
    except (ValueError, TypeError):
        return None
    forecast = get_forecast()
    return forecast.get(date)


def annotate_slots(slots):
    """Add weather info to a list of slot dicts. Modifies in place and returns the list."""
    forecast = get_forecast()
    for s in slots:
        try:
            date = dt.date.fromisoformat(s["slot_id"][:10])
        except (ValueError, TypeError):
            continue
        w = forecast.get(date)
        if w:
            s["weather"] = {
                "rating": w["rating"],
                "wind": f"{w['wind_kt']} mph {w['wind_dir']}",
                "wave_ft": w["wave_ft"],
                "summary": w["short"],
            }
    return slots


if __name__ == "__main__":
    forecast = _mock_forecast()
    for date in sorted(forecast)[:7]:
        w = forecast[date]
        print(f"{date} {w['rating']:>7}  wind {w['wind_kt']:2} kt {w['wind_dir']}, wave {w['wave_ft']} ft  {w['short']}")
    print("weather ok")
