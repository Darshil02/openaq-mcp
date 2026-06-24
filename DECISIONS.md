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

## Sensor metadata refetch

`/locations/{id}` is fetched on every get_readings and every get_measurements (via the sensor resolver). It changes rarely, so it is the first caching target in Phase 2.

## Staleness threshold is a deliberate cliff

48 hours is a fixed boundary, not a tuned value. age_hours is exposed alongside the stale boolean so consumers can apply their own judgment rather than trusting the cliff.

## Negative values preserved, not clamped

Daily minimums occasionally go slightly negative (near-zero sensor noise). Clamping to zero would falsify the source, so values pass through as returned, consistent with the never-convert-units principle.

## No float rounding in the service layer

OpenAQ's own `value` field provides a clean rounded figure; avg, min, and max carry full precision. The service layer stays a faithful pipe and lets the consumer choose which to display.

## Aggregation is a URL choice, not a query parameter

raw, hourly, and daily map to three different endpoint paths (/measurements, /measurements/hourly, /measurements/daily), validated at the edge against a fixed set.

## Errors written as instructions

A pollutant miss returns the available pollutants for that station; a bad aggregation returns the valid options; an empty window states the likely cause. The aim is model self-correction within one turn, not just failure reporting.
