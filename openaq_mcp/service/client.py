import os
import httpx

from openaq_mcp.service.models import LocationQuery


class OpenAQError(Exception):
    """Raised when the OpenAQ API call fails. The tool layer catches this."""
    pass


class OpenAQClient:
    BASE_URL = "https://api.openaq.org/v3"

    def __init__(self, api_key: str | None = None):
        # Pull the key once, here. Nothing else in the codebase reads the env.
        key = api_key or os.getenv("OPENAQ_API_KEY")
        if not key:
            raise OpenAQError("OPENAQ_API_KEY is not set")
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={"X-API-Key": key},
            timeout=10.0,
        )

    def _get(self, path: str, params: dict) -> dict:
        """The single choke point for every request. All error handling lives here."""
        # Drop None values so we never send empty query params.
        params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.get(path, params=params)
        except httpx.RequestError as e:
            # Network-level failure: DNS, timeout, connection refused.
            raise OpenAQError(f"could not reach OpenAQ: {e}") from e

        if resp.status_code == 200:
            return resp.json()

        # Non-200. Build a clean message regardless of the body's format.
        # (404 returns JSON, 422 a Python-style body, bad coords a plain-text 500.)
        raise OpenAQError(f"OpenAQ returned {resp.status_code}: {resp.text[:200]}")

    def find_locations(self, query: LocationQuery) -> list[dict]:
        """Find monitoring stations within a search scope. Returns clean station dicts."""
        params: dict = {"limit": 100}

        if query.latitude is not None:
            params["coordinates"] = f"{query.latitude},{query.longitude}"
            params["radius"] = query.radius
        if query.bbox is not None:
            params["bbox"] = ",".join(str(c) for c in query.bbox)
        if query.country is not None:
            params["iso"] = query.country

        data = self._get("/locations", params)
        results = data.get("results", [])

        # Shape the output: keep what a caller needs, drop the rest.
        return [self._shape_location(loc) for loc in results]

    def _shape_location(self, loc: dict) -> dict:
        """Trim a raw OpenAQ location to the fields we expose."""
        return {
            "id": loc["id"],
            "name": loc.get("name"),
            "locality": loc.get("locality"),
            "country": loc.get("country", {}).get("code"),
            "coordinates": loc.get("coordinates"),
            "last_reported": loc.get("datetimeLast", {}).get("utc"),
            # Pollutants this station measures, pulled from sensors but not exposing sensor IDs.
            "pollutants": [
                s["parameter"]["name"] for s in loc.get("sensors", [])
            ],
        }

    def _get_sensor_map(self, location_id: int) -> dict[int, dict]:
        """Return {sensor_id: {"parameter": name, "unit": units}} for one location.

        This is the lookup table that turns a bare value into a labelled reading.
        Hiding the sensor layer happens here: callers never see this map.
        """
        data = self._get(f"/locations/{location_id}", {})
        results = data.get("results", [])
        if not results:
            return {}
        sensors = results[0].get("sensors", [])
        return {
            s["id"]: {
                "parameter": s["parameter"]["name"],
                "unit": s["parameter"]["units"],
            }
            for s in sensors
        }

    def _resolve_sensor(self, location_id: int, parameter: str) -> dict:
        """Find the sensor at a location that measures a given pollutant.

        Returns {"sensor_id": int, "parameter": str, "unit": str}.

        Raises OpenAQError with an instructive message if the pollutant isn't
        measured here, listing what IS available so the caller can retry.
        """
        sensor_map = self._get_sensor_map(location_id)
        if not sensor_map:
            raise OpenAQError(
                f"location {location_id} has no sensors; "
                f"call find_locations to confirm the id"
            )

        wanted = parameter.strip().lower()
        for sensor_id, meta in sensor_map.items():
            if meta["parameter"].lower() == wanted:
                return {
                    "sensor_id": sensor_id,
                    "parameter": meta["parameter"],
                    "unit": meta["unit"],
                }

        # Miss: turn it into an instruction, not a dead end.
        available = sorted(m["parameter"] for m in sensor_map.values())
        raise OpenAQError(
            f"'{parameter}' is not measured at location {location_id}. "
            f"Available pollutants here: {', '.join(available)}"
        )

    # Maps the model-facing aggregation choice to the endpoint path segment.
    # raw -> /measurements, daily -> /measurements/daily, hourly -> /measurements/hourly.
    _AGG_PATHS = {
        "raw": "measurements",
        "hourly": "measurements/hourly",
        "daily": "measurements/daily",
    }

    def get_measurements(
        self,
        location_id: int,
        parameter: str,
        date_from: str,
        date_to: str,
        aggregation: str = "daily",
        limit: int = 100,
    ) -> dict:
        """Historical series for one pollutant at one station.

        Resolves the pollutant to its sensor internally, then fetches the
        aggregated series. Units travel with every value. Results are capped at
        `limit`; if more exist, the response says so rather than paging forever.
        """
        if aggregation not in self._AGG_PATHS:
            raise OpenAQError(
                f"aggregation must be one of {sorted(self._AGG_PATHS)}; got '{aggregation}'"
            )

        # Hop 1: pollutant name -> sensor (raises instructively on a miss).
        sensor = self._resolve_sensor(location_id, parameter)
        sensor_id = sensor["sensor_id"]
        unit = sensor["unit"]

        # Hop 2: fetch the series from the sensor endpoint.
        path = f"/sensors/{sensor_id}/{self._AGG_PATHS[aggregation]}"
        data = self._get(path, {
            "datetime_from": date_from,
            "datetime_to": date_to,
            "limit": limit,
        })

        results = data.get("results", [])
        found = data.get("meta", {}).get("found")

        series = [self._shape_measurement(m, unit) for m in results]

        # Truncation as instruction, not silence (audit #7).
        truncated = isinstance(found, int) and found > len(series)
        note = None
        if not series:
            note = (
                f"No {parameter} measurements at location {location_id} between "
                f"{date_from} and {date_to}. The station may not have reported then, "
                f"or the range may predate its first data."
            )
        elif truncated:
            note = (
                f"Showing first {len(series)} of {found} points. "
                f"Narrow the date range for finer detail."
            )

        return {
            "location_id": location_id,
            "parameter": sensor["parameter"],
            "unit": unit,
            "aggregation": aggregation,
            "series": series,
            "note": note,
        }

    def _shape_measurement(self, m: dict, unit: str) -> dict:
        """Trim one measurement bucket. Daily/hourly carry summary + coverage;
        raw carries a single value. Shape both into one consistent form."""
        period = m.get("period") or {}
        bucket = (period.get("datetimeFrom") or {}).get("utc")

        summary = m.get("summary") or {}
        coverage = m.get("coverage") or {}

        return {
            "date": bucket,
            "value": m.get("value"),          # present on raw; may be None on aggregated
            "avg": summary.get("avg"),        # headline for daily/hourly
            "min": summary.get("min"),
            "max": summary.get("max"),
            "unit": unit,                     # unit travels with the value
            "coverage_pct": coverage.get("percentComplete"),
        }

    def get_readings(self, location_id: int) -> dict:
        """Latest value per pollutant at a station, each joined to its unit.

        Returns a dict with the station id and a list of readings. An empty
        readings list means no recent data, NOT clean air. Each reading carries
        its own age so a stale value is never mistaken for a current one.
        """
        from datetime import datetime, timezone

        sensor_map = self._get_sensor_map(location_id)
        data = self._get(f"/locations/{location_id}/latest", {})
        latest = data.get("results", [])

        now = datetime.now(timezone.utc)
        readings = []
        for item in latest:
            sid = item["sensorsId"]
            meta = sensor_map.get(sid)
            if meta is None:
                continue

            ts = item.get("datetime", {}).get("utc")
            age_hours = None
            if ts:
                measured = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_hours = round((now - measured).total_seconds() / 3600, 1)

            readings.append({
                "parameter": meta["parameter"],
                "value": item["value"],
                "unit": meta["unit"],
                "datetime": ts,
                "age_hours": age_hours,
                "stale": age_hours is not None and age_hours > 48,
            })

        return {
            "location_id": location_id,
            "readings": readings,
            "note": None if readings else (
                "No recent measurements available. This means no current data, "
                "not necessarily clean air."
            ),
        }

    def close(self):
        self._client.close()

