"""Local web search and page reading.

Tagged "web" so the Claude provider can swap these for Anthropic's hosted
web_search / web_fetch tools; the local Ollama model uses these.
"""
from __future__ import annotations

from html.parser import HTMLParser

import requests

from ..security import safe_get
from .registry import Registry

USER_AGENT = "Mozilla/5.0 (compatible; personal-assistant)"


SEARCH_BACKENDS = ("auto", "google", "yahoo", "bing")      # the free engines each fail on some queries


def search_web(query: str, count: int = 6) -> list[dict]:
    """Search the web, falling through several free backends until one returns results."""
    from ddgs import DDGS

    last: Exception | None = None
    for backend in SEARCH_BACKENDS:
        try:
            results = DDGS().text(query, max_results=count, backend=backend)
            if results:
                return results
        except Exception as exc:                 # "No results found", rate limits, timeouts
            last = exc
    raise RuntimeError(f"Web search found nothing for '{query}' ({last}).")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return "\n".join(parser.parts)


WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def weather_report(location: str) -> str:
    """Current weather and a 3-day outlook from Open-Meteo (free, no key)."""
    geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                       params={"name": location, "count": 1}, timeout=15).json()
    if not geo.get("results"):
        return f"Couldn't find a place called '{location}'."
    place = geo["results"][0]
    data = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "forecast_days": 3,
    }, timeout=15).json()
    now, daily = data["current"], data["daily"]
    sky = lambda code: WEATHER_CODES.get(code, f"weather code {code}")  # noqa: E731
    lines = [
        f"{place['name']}, {place.get('country', '')}: {now['temperature_2m']}°C "
        f"(feels like {now['apparent_temperature']}°C), {sky(now['weather_code'])}, "
        f"humidity {now['relative_humidity_2m']}%, wind {now['wind_speed_10m']} km/h."
    ]
    for i, day in enumerate(daily["time"]):
        lines.append(f"{day}: {daily['temperature_2m_min'][i]}-{daily['temperature_2m_max'][i]}°C, "
                     f"{sky(daily['weather_code'][i])}, rain chance {daily['precipitation_probability_max'][i]}%")
    return "\n".join(lines)


def register(reg: Registry) -> None:
    @reg.tool(
        "get_weather",
        "Current weather and a 3-day forecast for a city or town.",
        {"location": {"type": "string", "description": "Place name, e.g. 'Hyderabad'."}},
        required=("location",),
    )
    def get_weather(location: str) -> str:
        return weather_report(location)

    @reg.tool(
        "web_search",
        "Search the web. Returns titles, URLs and short snippets. Follow up with fetch_page "
        "to read a result in full.",
        {"query": {"type": "string"}},
        required=("query",),
        tags=("web",),
    )
    def web_search(query: str) -> str:
        results = search_web(query)
        return "\n\n".join(f"{r['title']}\n{r['href']}\n{r['body']}" for r in results) or "No results."

    @reg.tool(
        "fetch_page",
        "Download a web page and return its readable text.",
        {"url": {"type": "string", "description": "http(s) URL."}},
        required=("url",),
        tags=("web",),
    )
    def fetch_page(url: str) -> str:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Only http and https URLs are allowed.")
        resp = safe_get(url, headers={"User-Agent": USER_AGENT}, timeout=20)   # never reaches this PC or the LAN
        resp.raise_for_status()
        if "html" in resp.headers.get("content-type", ""):
            return html_to_text(resp.text)
        return resp.text


# ---------------------------------------------------------------- quick knowledge (no keys needed)
def news_headlines(topic: str = "", region: str = "IN", limit: int = 8) -> str:
    """Latest headlines from Google News' public RSS feed, for a topic or the top stories."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    lang = f"hl=en-{region}&gl={region}&ceid={region}:en"
    url = (f"https://news.google.com/rss/search?q={quote(topic)}+when:3d&{lang}" if topic.strip()
           else f"https://news.google.com/rss?{lang}")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    items = ET.fromstring(resp.content).findall("./channel/item")[:limit]
    lines = []
    for item in items:
        title = item.findtext("title", "").strip()
        when = item.findtext("pubDate", "")[5:22]
        lines.append(f"- {title} ({when})")       # links are long and rarely needed aloud
    return "\n".join(lines) or f"No recent news found for '{topic}'."


def wikipedia_summary(topic: str) -> str:
    from urllib.parse import quote

    search = requests.get("https://en.wikipedia.org/w/api.php", headers={"User-Agent": USER_AGENT}, timeout=15,
                          params={"action": "opensearch", "search": topic, "limit": 1, "format": "json"}).json()
    if not search[1]:
        return f"Wikipedia has no article matching '{topic}'."
    page = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(search[1][0])}",
                        headers={"User-Agent": USER_AGENT}, timeout=15).json()
    return f"{page.get('title')}: {page.get('extract', '').strip()}\n{page.get('content_urls', {}).get('desktop', {}).get('page', '')}"


def currency(amount: float, from_code: str, to_code: str) -> str:
    data = requests.get(f"https://open.er-api.com/v6/latest/{from_code.upper()}", timeout=15).json()
    if data.get("result") != "success":
        raise ValueError(f"Unknown currency '{from_code}'.")
    rate = data["rates"].get(to_code.upper())
    if rate is None:
        raise ValueError(f"Unknown currency '{to_code}'.")
    return (f"{amount:,.2f} {from_code.upper()} = {amount * rate:,.2f} {to_code.upper()} "
            f"(rate {rate:.4f}, updated {data.get('time_last_update_utc', '')[:16]})")


def register_knowledge(reg: Registry) -> None:
    import os
    from urllib.parse import quote_plus

    @reg.tool(
        "get_news",
        "Latest headlines (last three days) on a topic, or the top stories if no topic. Use for "
        "'what's new in…', briefings and keeping up with the user's fields.",
        {"topic": {"type": "string", "description": "e.g. 'backend development', 'India cricket'. Empty = top stories."}},
    )
    def get_news(topic: str = "") -> str:
        return news_headlines(topic)

    @reg.tool(
        "wikipedia",
        "A short factual summary of a person, place, concept or event from Wikipedia.",
        {"topic": {"type": "string"}},
        required=("topic",),
    )
    def wikipedia(topic: str) -> str:
        return wikipedia_summary(topic)

    @reg.tool(
        "convert_currency",
        "Convert money between currencies at today's rate, e.g. 100 USD to INR.",
        {"amount": {"type": "number"}, "from_currency": {"type": "string", "description": "Code, e.g. USD."},
         "to_currency": {"type": "string", "description": "Code, e.g. INR."}},
        required=("amount", "from_currency", "to_currency"),
    )
    def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
        return currency(amount, from_currency, to_currency)

    @reg.tool(
        "play_youtube",
        "Play something on YouTube: opens the top result for the search in the user's browser "
        "(music, a tutorial, a talk).",
        {"query": {"type": "string"}},
        required=("query",),
    )
    def play_youtube(query: str) -> str:
        import re

        html = requests.get("https://www.youtube.com/results", params={"search_query": query},
                            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=15).text
        ids = re.findall(r'"videoId":"([\w-]{11})"', html)
        url = f"https://www.youtube.com/watch?v={ids[0]}" if ids else \
            f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        os.startfile(url)  # noqa: S606
        return f"Playing on YouTube: {query}." if ids else f"Opened YouTube results for: {query}."

    @reg.tool(
        "open_map",
        "Open Google Maps for a place, or for directions between two places.",
        {"place": {"type": "string", "description": "The place, or the destination for directions."},
         "from_place": {"type": "string", "description": "Starting point, for directions."}},
        required=("place",),
    )
    def open_map(place: str, from_place: str = "") -> str:
        if from_place:
            url = (f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(from_place)}"
                   f"&destination={quote_plus(place)}")
        else:
            url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(place)}"
        os.startfile(url)  # noqa: S606
        return f"Opened Maps for {place}" + (f" from {from_place}." if from_place else ".")
