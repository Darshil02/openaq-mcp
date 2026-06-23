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

