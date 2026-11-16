"""Weather: the forecast for one place, from Open-Meteo. Python only, no AI, free.

Exact work is Python: a forecast is numbers, so no agent searches the web for it.
Python asks one fixed host (HOST) and keeps only numbers and times from the answer.
Every text (the place name, "clear sky") is ours: nothing a stranger wrote reaches
the page or an agent.

What leaves the machine: one HTTPS request with the latitude and the longitude of
the place. No key, no account. The answer is kept for CACHE_SECONDS in
data/weather.json, so the page asks the host at most four times an hour.

The place is the setting "weather_place" in data/settings.json, for example
    "weather_place": {"name": "Plovdiv", "latitude": 42.15, "longitude": 24.75}
Without it the place is DEFAULT_PLACE.

Usage (from the project folder):
    python -m agentic_os.weather          now, and the hours until the end of the day
    python -m agentic_os.weather --json   the same facts as JSON (what /api/weather returns)
"""

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from agentic_os import job_store, memory
from agentic_os.job_store import DATA_DIR

CACHE_FILE = DATA_DIR / "weather.json"
HOST = "api.open-meteo.com"
TIMEOUT = 10  # seconds
MAX_BYTES = 200_000  # one day of hours is about 2 KB; a much bigger answer is not a forecast
CACHE_SECONDS = 15 * 60  # Open-Meteo's "current" values change every 15 minutes
DEFAULT_PLACE = {"name": "Sofia", "latitude": 42.6977, "longitude": 23.3219}
DEGREES = "\u00b0C"

# WMO weather codes (the "weather_code" numbers of Open-Meteo) in simple English.
CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "cloudy",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


class WeatherError(Exception):
    """The forecast cannot be read: no network, a bad answer, a bad place. The message says why."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: we talk to HOST only, not to a host that HOST names."""

    def redirect_request(self, *args, **kwargs):
        return None


OPEN = urllib.request.build_opener(NoRedirect).open  # tests replace it with a fake (no network in tests)


def is_number(value):
    """True for a real, finite number (JSON true/false is not a number here)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def place():
    """Return the place from the settings, or DEFAULT_PLACE when the setting is missing or wrong."""
    saved = job_store.settings().get("weather_place")
    if (isinstance(saved, dict) and isinstance(saved.get("name"), str) and saved["name"].strip()
            and is_number(saved.get("latitude")) and -90 <= saved["latitude"] <= 90
            and is_number(saved.get("longitude")) and -180 <= saved["longitude"] <= 180):
        return {"name": saved["name"].strip()[:40], "latitude": saved["latitude"], "longitude": saved["longitude"]}
    return dict(DEFAULT_PLACE)


def url(where):
    """Return the request for one day of forecast, in the local time of the place."""
    query = urllib.parse.urlencode({
        "latitude": where["latitude"],
        "longitude": where["longitude"],
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "timezone": "auto",
        "forecast_days": 1,
    })
    return f"https://{HOST}/v1/forecast?{query}"


def fetch(where):
    """Ask Open-Meteo and return its answer as a dict. The only function that uses the network."""
    try:
        with OPEN(url(where), timeout=TIMEOUT) as reply:
            raw = reply.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as error:  # no network, a timeout, an HTTP error, a redirect
        raise WeatherError(f"Cannot reach {HOST}: {error}") from error
    if len(raw) > MAX_BYTES:
        raise WeatherError(f"The answer of {HOST} is too big.")
    try:
        return json.loads(raw)
    except ValueError as error:
        raise WeatherError(f"The answer of {HOST} is not JSON: {error}") from error


def describe(code):
    """Return our text for a weather code. An unknown code gets a text too, never an error."""
    return CODES.get(code, f"code {code}") if is_number(code) else "unknown"


def parse(data, name):
    """Turn Open-Meteo's answer into our facts. Pure: no network, no clock.

    "Now" is the time in the answer (the local time of the place), so the hours
    "until the end of the day" never depend on this machine's clock or time zone.
    """
    try:
        current, hourly = data["current"], data["hourly"]
        now = datetime.fromisoformat(current["time"])
        numbers = [current["temperature_2m"], current["apparent_temperature"], current["wind_speed_10m"]]
        times, temperatures = hourly["time"], hourly["temperature_2m"]
        rains, codes = hourly["precipitation_probability"], hourly["weather_code"]
        if not all(is_number(number) for number in numbers):
            raise ValueError("a current value is not a number")
        if not (len(times) == len(temperatures) == len(rains) == len(codes)):
            raise ValueError("the hourly lists have different lengths")
        hours = []
        for moment, temperature, rain, code in zip(times, temperatures, rains, codes):
            moment = datetime.fromisoformat(moment)
            if moment <= now or moment.date() != now.date():
                continue
            if not is_number(temperature):
                raise ValueError("an hourly temperature is not a number")
            hours.append({"time": moment.strftime("%H:%M"), "temperature": temperature,
                          "rain": rain if is_number(rain) else 0,  # the host sends null when it has no value
                          "text": describe(code)})
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherError(f"The answer of {HOST} is not a forecast: {error!r}") from error
    facts = {
        "place": name,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "temperature": numbers[0],
        "feels_like": numbers[1],
        "wind": numbers[2],
        "text": describe(current.get("weather_code")),
        "hours": hours,
    }
    facts["line"] = f"{name} {round(numbers[0])}{DEGREES}, {facts['text']}"
    facts["rest"] = rest_of_day(hours)
    return facts


def rest_of_day(hours):
    """Return one sentence about the hours that are left today."""
    if not hours:
        return "The day is over: no more hours in today's forecast."
    low = round(min(hour["temperature"] for hour in hours))
    high = round(max(hour["temperature"] for hour in hours))
    wettest = max(hours, key=lambda hour: hour["rain"])  # the first one of the wettest hours
    rain = (f"rain chance up to {round(wettest['rain'])}% (around {wettest['time']})"
            if wettest["rain"] > 0 else "no rain expected")
    return f"Until the end of the day: {low} to {high}{DEGREES}, {rain}."


def load_cache():
    """Return the saved answer {"at", "place", "facts"}, or None when there is no good one."""
    try:
        saved = json.loads(CACHE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(saved, dict) and is_number(saved.get("at")) and isinstance(saved.get("facts"), dict):
        return saved
    return None


def weather(now=None):
    """Return the facts for the place in the settings. Asks the host only when the saved answer is old.

    now: seconds (time.time()), for tests.
    When the host cannot be reached, an old answer for the same place is returned
    with "stale": True; with no such answer, WeatherError.
    """
    now = time.time() if now is None else now
    where = place()
    saved = load_cache()
    if saved and saved.get("place") != where:
        saved = None  # the place changed in the settings
    if saved and 0 <= now - saved["at"] < CACHE_SECONDS:
        return saved["facts"]
    try:
        facts = parse(fetch(where), where["name"])
    except WeatherError:
        if saved:
            return {**saved["facts"], "stale": True}
        raise
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    memory.write_atomic(CACHE_FILE, json.dumps({"at": now, "place": where, "facts": facts}, indent=2))
    return facts


def report(facts):
    """Return the facts as text, for the terminal (and later for an agent's task)."""
    lines = [
        f"{facts['place']} at {facts['time']}: {round(facts['temperature'])}{DEGREES} "
        f"(feels like {round(facts['feels_like'])}{DEGREES}), {facts['text']}, "
        f"wind {round(facts['wind'])} km/h.",
        facts["rest"],
    ]
    for hour in facts["hours"]:
        lines.append(f"  {hour['time']}  {round(hour['temperature']):>3}{DEGREES}  "
                     f"rain {round(hour['rain']):>3}%  {hour['text']}")
    if facts.get("stale"):
        lines.append(f"(An old answer: {HOST} cannot be reached now.)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="The weather now and until the end of the day (free, no AI).")
    parser.add_argument("--json", action="store_true", help="print the facts as JSON")
    args = parser.parse_args()
    try:
        facts = weather()
    except WeatherError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(facts, indent=2) if args.json else report(facts))


if __name__ == "__main__":
    main()
