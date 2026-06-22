"""
Počasí z Open-Meteo API — zdarma, bez API klíče.
Pardubice: lat=50.04, lon=15.78
Cachuje výsledek na 30 minut.
"""
import time
import logging
import requests
from datetime import datetime

_log = logging.getLogger("weather")

# WMO weather codes -> česky
_WMO = {
    0:  "jasno",
    1:  "převážně jasno", 2: "částečně zataženo", 3: "zataženo",
    45: "mlha", 48: "námrazová mlha",
    51: "slabé mrholení", 53: "mrholení", 55: "silné mrholení",
    61: "slabý déšť", 63: "déšť", 65: "silný déšť",
    71: "slabé sněžení", 73: "sněžení", 75: "silné sněžení",
    77: "sněhové vločky",
    80: "slabé přeháňky", 81: "přeháňky", 82: "silné přeháňky",
    85: "sněhové přeháňky", 86: "silné sněhové přeháňky",
    95: "bouřka", 96: "bouřka s krupobitím", 99: "silná bouřka s krupobitím",
}


class WeatherCHMU:

    def __init__(self, lat: float = 50.04, lon: float = 15.78):
        self._lat = lat
        self._lon = lon
        self._url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,weathercode,windspeed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&timezone=Europe/Prague&forecast_days=1"
        )
        self._cache     = None
        self._cache_ts  = 0.0
        self._cache_ttl = 1800.0  # 30 minut


    def _fetch(self) -> dict | None:
        try:
            r = requests.get(self._url, timeout=10)
            if r.status_code != 200:
                _log.warning("Open-Meteo HTTP %d", r.status_code)  # WEATHER_FETCH_LOG_V1
                return None
            data    = r.json()
            cur     = data.get('current', {})
            daily   = data.get('daily', {})
            code    = cur.get('weathercode', 0)
            from datetime import datetime as _dt
            result  = {
                'description':   _WMO.get(code, f'kod {code}'),
                'weathercode':   code,
                'temp_current':  cur.get('temperature_2m'),
                'temp_min':      daily.get('temperature_2m_min', [None])[0],
                'temp_max':      daily.get('temperature_2m_max', [None])[0],
                'windspeed':     cur.get('windspeed_10m'),
                'fetched_at':    _dt.now().strftime('%H:%M'),
            }
            return result
        except Exception as e:
            _log.error("Weather fetch error: %s", e)  # WEATHER_FETCH_LOG_V1
            return None

    def get_weather(self) -> dict:
        now = time.time()
        if self._cache and now - self._cache_ts < self._cache_ttl:
            return self._cache
        result = self._fetch()
        if result:
            self._cache    = result
            self._cache_ts = now
        return result or {}

    def get_context_string(self) -> str:
        w = self.get_weather()
        if not w:
            return ""
        desc  = w.get("description", "neznámo")
        temp  = w.get("temp_current")
        tmin  = w.get("temp_min")
        tmax  = w.get("temp_max")
        wind  = w.get("windspeed")
        parts = [f"Počasí: {desc}"]
        if temp is not None:
            parts.append(f"{temp:.1f}°C")
        if tmin is not None and tmax is not None:
            parts.append(f"(min {tmin:.0f}, max {tmax:.0f}°C)")
        if wind:
            parts.append(f"vítr {wind:.0f} km/h")
        return " ".join(parts) + "."

    def get_tomorrow_string(self) -> str:
        """Vrať předpověď na zítřek pro LLM."""
        from datetime import datetime, timedelta
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.")
        try:
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={self._lat}&longitude={self._lon}"
                f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
                f"&timezone=Europe/Prague&forecast_days=2"
            )
            r = requests.get(url, timeout=10)
            data  = r.json()
            daily = data.get("daily", {})
            codes = daily.get("weathercode", [0, 0])
            tmins = daily.get("temperature_2m_min", [None, None])
            tmaxs = daily.get("temperature_2m_max", [None, None])
            code  = codes[1] if len(codes) > 1 else 0
            tmin  = tmins[1] if len(tmins) > 1 else None
            tmax  = tmaxs[1] if len(tmaxs) > 1 else None
            desc  = _WMO.get(code, "proměnlivě")
            parts = [f"Zítra ({tomorrow}): {desc}"]
            if tmin is not None and tmax is not None:
                parts.append(f"{tmin:.0f}–{tmax:.0f}°C")
            return " ".join(parts) + "."
        except Exception as e:
            _log.error("Tomorrow forecast error: %s", e)
            return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    w = WeatherCHMU()
    print(w.get_weather())
    print(w.get_context_string())
