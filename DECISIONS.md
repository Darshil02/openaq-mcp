# Decisions

## Layer separation

Three layers: transport (FastMCP, later), tools (later), service (OpenAQ client). HTTP lives only in the service layer's `_get` method. Rule: if httpx appears in a tool, separation is broken. The service layer is a plain Python library, testable without MCP.

## Input validation at the edge

`LocationQuery` (Pydantic) validates before any HTTP. Field constraints handle ranges (lat/lon bounds, radius 1 to 25000). A model validator handles cross-field rules: coordinates and radius must travel together, point and bbox cannot mix, at least one scope is required. Bad input never reaches the API.

## One error type

OpenAQ returns three different error body formats: clean JSON on 404, structured detail on 422, plain text on some 500s. `_get` collapses every non-200 and every network failure into a single `OpenAQError` with a readable message, so callers handle one exception, not three formats.

## Hiding the sensor layer

OpenAQ models location to sensor to measurement. The `/latest` endpoint returns a value plus a sensorsId but no unit and no pollutant name; those live on the `/locations/{id}` sensor metadata. `get_readings` calls both endpoints and joins them on sensor ID, so callers see {parameter, value, unit} and never learn sensors exist.

## Units never converted

Each value carries its own unit string copied directly from sensor metadata. No conversion, no unit math. In OpenAQ the same pollutant has different parameter IDs per unit, so converting would be both wrong and lossy.

## Staleness flagging

The `/latest` endpoint returns the last known value regardless of age, so latest is not the same as fresh. Each reading carries age_hours and a stale boolean (older than 48 hours). Stale values are kept and labelled, not dropped, so an old reading cannot masquerade as current and is not silently discarded. The 48 hour threshold is a deliberate choice for hourly air-quality data.

## Empty means no coverage

An empty readings list returns an explicit note: no current data, not necessarily clean air. Silence must never be read as safety.
