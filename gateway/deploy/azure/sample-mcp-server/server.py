"""A genuine Streamable HTTP MCP server -- not a mock -- for exercising the
gateway's Azure deployment end-to-end before pointing anything real at it.
Two toy domains (weather, flights) so a demo has more than one tool to call.

Deliberately not the SDK's own conformance fixture (conformance/mcp-probe/
server.py): that one is a pytest-adjacent investigation artifact; this one is
meant to be built into a container and deployed as this gateway's actual
upstream (see ../deploy-sample-mcp-server.sh), with canned but slightly
richer responses that read naturally in a live Rovo conversation.

FastMCP's default streamable_http_path is "/mcp", matching MCPParser.matches()
(path.startswith("/mcp")) with zero path rewriting needed on the gateway side.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="parapet-sample-mcp-server", host="0.0.0.0", port=8090)  # noqa: S104

_WEATHER = {
    "san francisco": {"condition": "foggy", "high_f": 62, "low_f": 51},
    "new york": {"condition": "clear", "high_f": 78, "low_f": 64},
    "london": {"condition": "light rain", "high_f": 58, "low_f": 49},
    "tokyo": {"condition": "sunny", "high_f": 81, "low_f": 68},
}

_FLIGHTS = {
    ("san francisco", "new york"): [
        {"flight": "PA101", "depart": "08:15", "arrive": "16:40", "status": "on time"},
        {"flight": "PA204", "depart": "13:30", "arrive": "21:55", "status": "delayed 20m"},
    ],
    ("new york", "london"): [
        {"flight": "PA330", "depart": "21:00", "arrive": "09:10+1", "status": "on time"},
    ],
}


@mcp.tool()
def get_weather(city: str) -> str:
    """Current conditions for a city (toy data -- san francisco, new york,
    london, tokyo are the only ones populated)."""
    entry = _WEATHER.get(city.strip().lower())
    if entry is None:
        return f"No data for {city!r}. Known cities: {', '.join(sorted(_WEATHER))}"
    return f"{city}: {entry['condition']}, high {entry['high_f']}F / low {entry['low_f']}F"


@mcp.tool()
def get_forecast(city: str, days: int = 3) -> str:
    """A deterministic N-day forecast (toy data, not a real forecast API)."""
    entry = _WEATHER.get(city.strip().lower())
    if entry is None:
        return f"No data for {city!r}. Known cities: {', '.join(sorted(_WEATHER))}"
    days = max(1, min(days, 7))
    base_high = entry["high_f"]
    lines = [f"day {i + 1}: {entry['condition']}, high {base_high - i}F" for i in range(days)]
    return f"{days}-day forecast for {city}:\n" + "\n".join(lines)


@mcp.tool()
def search_flights(origin: str, destination: str) -> str:
    """Toy flight search between two cities (only SF->NYC and NYC->London
    are populated -- everything else returns no results, deliberately, so a
    real conversation can demonstrate both the happy path and an empty one)."""
    key = (origin.strip().lower(), destination.strip().lower())
    flights = _FLIGHTS.get(key)
    if not flights:
        return f"No flights found from {origin} to {destination}."
    lines = [
        f"{f['flight']}: depart {f['depart']}, arrive {f['arrive']} ({f['status']})"
        for f in flights
    ]
    return f"Flights from {origin} to {destination}:\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
