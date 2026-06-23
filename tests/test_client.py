import httpx
import pytest
import respx

from openaq_mcp.service.client import OpenAQClient, OpenAQError
from openaq_mcp.service.models import LocationQuery

BASE = "https://api.openaq.org/v3"


@pytest.fixture
def client():
    # Pass a key directly so the test never depends on the env var.
    c = OpenAQClient(api_key="test-key")
    yield c
    c.close()


@respx.mock
def test_find_locations_shapes_output(client):
    # Fake the /locations response with one station carrying two sensors.
    respx.get(f"{BASE}/locations").mock(return_value=httpx.Response(200, json={
        "meta": {"found": 1},
        "results": [{
            "id": 217,
            "name": "Test Station",
            "locality": "Testville",
            "country": {"code": "US"},
            "coordinates": {"latitude": 40.0, "longitude": -86.0},
            "datetimeLast": {"utc": "2026-06-22T16:00:00Z"},
            "sensors": [
                {"id": 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
                {"id": 2, "parameter": {"name": "o3", "units": "ppm"}},
            ],
        }],
    }))

    q = LocationQuery(latitude=40.0, longitude=-86.0, radius=10000)
    out = client.find_locations(q)

    assert len(out) == 1
    station = out[0]
    assert station["id"] == 217
    assert station["pollutants"] == ["pm25", "o3"]   # names only, no sensor IDs
    assert "sensors" not in station                   # sensor layer is hidden


@respx.mock
def test_get_readings_joins_units(client):
    # Sensor metadata comes from /locations/{id}
    respx.get(f"{BASE}/locations/217").mock(return_value=httpx.Response(200, json={
        "results": [{
            "id": 217,
            "sensors": [
                {"id": 360, "parameter": {"name": "pm25", "units": "µg/m³"}},
            ],
        }],
    }))
    # Values come from /locations/{id}/latest, keyed by sensorsId, no unit.
    respx.get(f"{BASE}/locations/217/latest").mock(return_value=httpx.Response(200, json={
        "results": [{
            "sensorsId": 360,
            "value": 4.7,
            "datetime": {"utc": "2026-06-22T16:00:00Z"},
        }],
    }))

    out = client.get_readings(217)

    assert len(out["readings"]) == 1
    r = out["readings"][0]
    assert r["parameter"] == "pm25"
    assert r["value"] == 4.7
    assert r["unit"] == "µg/m³"     # the unit was joined from sensor metadata
    assert out["note"] is None


@respx.mock
def test_get_readings_empty_has_note(client):
    respx.get(f"{BASE}/locations/999").mock(return_value=httpx.Response(200, json={
        "results": [{"id": 999, "sensors": []}],
    }))
    respx.get(f"{BASE}/locations/999/latest").mock(return_value=httpx.Response(200, json={
        "results": [],
    }))

    out = client.get_readings(999)

    assert out["readings"] == []
    assert out["note"] is not None   # empty must explain itself, not imply clean air


@respx.mock
def test_404_raises_openaq_error(client):
    respx.get(f"{BASE}/locations/99999999").mock(
        return_value=httpx.Response(404, json={"detail": "Location not found"})
    )

    with pytest.raises(OpenAQError):
        client.get_readings(99999999)


def test_bad_input_rejected_by_model():
    # Validation happens before any HTTP, so no mock needed.
    with pytest.raises(Exception):
        LocationQuery(latitude=40.0, longitude=-86.0)   # radius missing
