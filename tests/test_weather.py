"""Tests for weather.py: the forecast facts. No network: a fake OPEN gives the answer."""

import io
import json
import urllib.error

import pytest
from fastapi import HTTPException

from agentic_os import job_store, weather, web

NOW = 1_800_000_000.0  # seconds, like time.time()


def answer(current_time="2026-11-13T12:15", **changes):
    """Return an answer like Open-Meteo's, for six hours of one day."""
    data = {
        "timezone": "Europe/Sofia",
        "current": {"time": current_time, "temperature_2m": 21.0, "apparent_temperature": 17.2,
                    "weather_code": 0, "wind_speed_10m": 20.9},
        "hourly": {
            "time": [f"2026-11-13T{hour:02d}:00" for hour in (10, 11, 12, 13, 20, 23)],
            "temperature_2m": [18.6, 19.8, 20.9, 21.1, 14.6, 12.2],
            "precipitation_probability": [0, 0, 0, 0, 35, 3],
            "weather_code": [1, 0, 0, 0, 2, 61],
        },
    }
    data.update(changes)
    return data


@pytest.fixture
def host(monkeypatch):
    """A fake Open-Meteo. host.reply is what it sends (bytes, or an error to raise); host.urls is what was asked."""
    class Host:
        reply = json.dumps(answer()).encode()
        urls = []

        def open(self, url, timeout):
            self.urls.append(url)
            if isinstance(self.reply, Exception):
                raise self.reply
            return io.BytesIO(self.reply)

    fake = Host()
    fake.urls = []
    monkeypatch.setattr(weather, "OPEN", fake.open)
    return fake


def test_parse_keeps_now_and_the_hours_that_are_left():
    facts = weather.parse(answer(), "Sofia")

    assert facts["place"] == "Sofia" and facts["time"] == "2026-11-13 12:15"
    assert facts["temperature"] == 21.0 and facts["feels_like"] == 17.2 and facts["text"] == "clear sky"
    assert [hour["time"] for hour in facts["hours"]] == ["13:00", "20:00", "23:00"]
    assert facts["hours"][2] == {"time": "23:00", "temperature": 12.2, "rain": 3, "text": "light rain"}
    assert facts["line"] == "Sofia 21\u00b0C, clear sky"
    assert facts["rest"] == "Until the end of the day: 12 to 21\u00b0C, rain chance up to 35% (around 20:00)."


def test_a_dry_evening_and_the_end_of_the_day():
    dry = answer()
    dry["hourly"]["precipitation_probability"] = [0, 0, 0, 0, None, 0]  # the host sends null for "no value"
    assert weather.parse(dry, "Sofia")["rest"] == "Until the end of the day: 12 to 21\u00b0C, no rain expected."

    late = weather.parse(answer(current_time="2026-11-13T23:30"), "Sofia")
    assert late["hours"] == [] and late["rest"].startswith("The day is over")


def test_an_unknown_weather_code_is_not_an_error():
    odd = answer()
    odd["current"]["weather_code"] = 42
    assert weather.parse(odd, "Sofia")["text"] == "code 42"
    odd["current"]["weather_code"] = "<b>sunny</b>"  # text from the host never becomes our text
    assert weather.parse(odd, "Sofia")["text"] == "unknown"


@pytest.mark.parametrize("bad", [
    {},
    {"current": {}, "hourly": {}},
    answer(current={"time": "not a time", "temperature_2m": 1, "apparent_temperature": 1, "wind_speed_10m": 1}),
    answer(current={"time": "2026-11-13T12:15", "temperature_2m": "21", "apparent_temperature": 1,
                    "wind_speed_10m": 1}),
    answer(hourly={"time": ["2026-11-13T13:00"], "temperature_2m": [], "precipitation_probability": [],
                   "weather_code": []}),
    answer(hourly={"time": ["2026-11-13T13:00"], "temperature_2m": [None], "precipitation_probability": [0],
                   "weather_code": [0]}),
    [],
])
def test_an_answer_that_is_not_a_forecast(bad):
    with pytest.raises(weather.WeatherError):
        weather.parse(bad, "Sofia")


def test_the_request_goes_to_the_one_host_with_the_place(host):
    weather.weather(NOW)

    assert len(host.urls) == 1
    assert host.urls[0].startswith("https://api.open-meteo.com/v1/forecast?")
    assert "latitude=42.6977" in host.urls[0] and "longitude=23.3219" in host.urls[0]


def test_a_fresh_answer_is_not_asked_again(host):
    first = weather.weather(NOW)
    again = weather.weather(NOW + weather.CACHE_SECONDS - 1)
    assert again == first and len(host.urls) == 1

    weather.weather(NOW + weather.CACHE_SECONDS)
    assert len(host.urls) == 2


def test_no_network_gives_the_old_answer_marked_stale(host):
    fresh = weather.weather(NOW)
    host.reply = urllib.error.URLError("no network")

    old = weather.weather(NOW + weather.CACHE_SECONDS + 60)

    assert old == {**fresh, "stale": True}
    assert "stale" not in json.loads(weather.CACHE_FILE.read_text())["facts"]  # the mark is never saved


def test_no_network_and_no_old_answer_is_an_error(host):
    host.reply = urllib.error.URLError("no network")
    with pytest.raises(weather.WeatherError, match="Cannot reach api.open-meteo.com"):
        weather.weather(NOW)

    host.reply = b"<html>not json</html>"
    with pytest.raises(weather.WeatherError, match="not JSON"):
        weather.weather(NOW)

    host.reply = b" " * (weather.MAX_BYTES + 1)
    with pytest.raises(weather.WeatherError, match="too big"):
        weather.weather(NOW)


def test_a_new_place_in_the_settings_asks_again(host):
    weather.weather(NOW)
    job_store.set_setting("weather_place", {"name": "Plovdiv", "latitude": 42.15, "longitude": 24.75})

    facts = weather.weather(NOW + 1)

    assert facts["place"] == "Plovdiv" and len(host.urls) == 2 and "latitude=42.15" in host.urls[1]


@pytest.mark.parametrize("wrong", [
    "Plovdiv",
    {"name": "Plovdiv"},
    {"name": "", "latitude": 42.15, "longitude": 24.75},
    {"name": "Plovdiv", "latitude": 420, "longitude": 24.75},
    {"name": "Plovdiv", "latitude": True, "longitude": 24.75},
    {"name": "Plovdiv", "latitude": "42.15&x=1", "longitude": 24.75},
])
def test_a_wrong_place_in_the_settings_is_the_default(wrong):
    job_store.set_setting("weather_place", wrong)
    assert weather.place() == weather.DEFAULT_PLACE


def test_a_redirect_is_not_followed():
    handler = weather.NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/") is None


def test_the_report_lists_the_hours(host):
    text = weather.report(weather.weather(NOW))

    assert text.splitlines()[0] == "Sofia at 2026-11-13 12:15: 21\u00b0C (feels like 17\u00b0C), clear sky, wind 21 km/h."
    assert "  20:00   15\u00b0C  rain  35%  partly cloudy" in text


def test_the_web_api(host):
    assert web.api_weather()["line"] == "Sofia 21\u00b0C, clear sky"


def test_the_web_api_without_network_is_a_503(host):
    host.reply = urllib.error.URLError("no network")
    with pytest.raises(HTTPException) as error:
        web.api_weather()
    assert error.value.status_code == 503
