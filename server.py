"""MCP server entry point: exposes the OpenAQ service layer as MCP tools.

Transport is stdio for local use (Claude Desktop, MCP Inspector). The tools
defined here are a thin layer: they validate input, call the service layer,
and shape output. No HTTP lives in this file or in any tool. That rule is the
whole point of the layer separation.
"""

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load OPENAQ_API_KEY from .env before the service layer reads it.
load_dotenv()

# The server instance. The name is what clients display.
mcp = FastMCP("openaq")

from openaq_mcp.service.client import OpenAQClient, OpenAQError
from openaq_mcp.service.models import LocationQuery

# One client for the process lifetime. Reuses the httpx connection pool across
# tool calls instead of opening a new client every time. Created after
# load_dotenv() so the API key is already in the environment.
client = OpenAQClient()


@mcp.tool()
def find_locations(
    latitude: float | None = None,
    longitude: float | None = None,
    radius: int | None = None,
    country: str | None = None,
) -> list[dict]:
    """Find air-quality monitoring stations within a geographic scope.

    This is the required first step before reading air quality: it returns the
    station IDs that get_readings needs. You must provide ONE search scope:

    - latitude + longitude + radius (radius in meters, 1 to 25000), to search
      around a point. All three are required together.
    - country, a two-letter ISO code (US, IN, GB), to list stations in a country.

    Returns a list of stations, each with: id, name, locality, country,
    coordinates, last_reported (UTC timestamp the station last sent data), and
    pollutants (the list of things it measures). An EMPTY list means there are no
    monitoring stations in that scope, which is NOT the same as clean air; it
    means no coverage there.
    """
    try:
        query = LocationQuery(
            latitude=latitude,
            longitude=longitude,
            radius=radius,
            country=country,
        )
    except ValueError as e:
        # Input failed validation at the edge. Surface a clean message the model
        # can act on, instead of leaking a raw Pydantic traceback.
        raise ValueError(f"Invalid search input: {e}")

    try:
        return client.find_locations(query)
    except OpenAQError as e:
        # Service-layer failure (network, API error). One clean error type.
        raise ValueError(f"OpenAQ request failed: {e}")

@mcp.tool()
def get_readings(location_id: int) -> dict:
    """Get the latest pollutant readings for one monitoring station.

    Call find_locations first to get a location_id. Returns a dict with:

    - location_id: the station queried.
    - readings: a list, one entry per pollutant, each with parameter (e.g.
      "pm25"), value, unit (always paired with the value; never assume a unit),
      datetime (UTC of the measurement), age_hours (how old the reading is), and
      stale (true if older than 48 hours).
    - note: present only when readings is empty. An empty readings list means no
      recent data is available, which is NOT a reading of zero pollution.

    IMPORTANT for interpreting results: a reading marked stale=true is the
    station's LAST known value, not a current one. Do not present a stale value
    as the current air quality; say how old it is. A reading of 0.0 that is stale
    is an old number, not clean air now.
    """
    try:
        return client.get_readings(location_id)
    except OpenAQError as e:
        raise ValueError(f"OpenAQ request failed: {e}")

@mcp.tool()
def get_measurements(
    location_id: int,
    parameter: str,
    date_from: str,
    date_to: str,
    aggregation: str = "daily",
) -> dict:
    """Historical air-quality series for one pollutant at one station.

    Call find_locations first to get a location_id and to see which pollutants
    the station measures. Then call this for a time series of one pollutant.

    Arguments:
    - location_id: from find_locations.
    - parameter: a pollutant name as shown in find_locations' "pollutants" list
      (e.g. "pm25"). If you pass one the station doesn't measure, the error tells
      you which are available; retry with one of those.
    - date_from, date_to: ISO dates, "YYYY-MM-DD" (e.g. "2026-06-01").
    - aggregation: "daily" (default), "hourly", or "raw". Daily is best for
      trends; raw is voluminous and only for short ranges.

    Returns location_id, parameter, unit, aggregation, and series. Each point has
    date, avg (the headline for daily/hourly), min, max, unit, and coverage_pct
    (how complete that bucket is; a low number means the average rests on few
    samples and is less reliable). For raw aggregation read "value", not "avg".

    note is set when the series is empty (with the likely reason) or when results
    were capped (narrow the date range for more). An empty series means no data
    for that window, NOT zero pollution.
    """
    # Validate date shape at the edge so a malformed date becomes an instruction,
    # not a raw API 422.
    from datetime import datetime
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"{label} must be YYYY-MM-DD (e.g. 2026-06-01); got '{value}'"
            )

    try:
        return client.get_measurements(
            location_id=location_id,
            parameter=parameter,
            date_from=date_from,
            date_to=date_to,
            aggregation=aggregation,
        )
    except OpenAQError as e:
        raise ValueError(f"OpenAQ request failed: {e}")

if __name__ == "__main__":
    # stdio transport: the client launches this process and talks over
    # stdin/stdout. No network, no ports. Right choice for local Phase 1.
    mcp.run(transport="stdio")
