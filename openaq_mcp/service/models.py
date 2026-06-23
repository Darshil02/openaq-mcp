from typing import Optional
from pydantic import BaseModel, Field, model_validator


class LocationQuery(BaseModel):
    # Field constraints: range checks, done declaratively.
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    radius: Optional[int] = Field(default=None, gt=0, le=25000)
    bbox: Optional[tuple[float, float, float, float]] = None
    country: Optional[str] = Field(default=None, min_length=2, max_length=2)

    # Model validator: cross-field rules, runs after the fields above pass.
    @model_validator(mode="after")
    def check_scope(self):
        has_point = self.latitude is not None and self.longitude is not None
        has_radius = self.radius is not None
        has_bbox = self.bbox is not None
        has_country = self.country is not None

        # Rule 1: coordinates and radius must travel together (API 422s otherwise).
        if has_point != has_radius:
            raise ValueError("coordinates and radius must be provided together")

        # Rule 2: can't mix a point search with a bbox search.
        if (has_point or has_radius) and has_bbox:
            raise ValueError("use coordinates+radius OR bbox, not both")

        # Rule 3: at least one search scope is required.
        if not (has_point or has_bbox or has_country):
            raise ValueError("provide coordinates+radius, bbox, or a country code")

        return self
