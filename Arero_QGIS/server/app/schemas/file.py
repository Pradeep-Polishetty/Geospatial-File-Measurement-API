from typing import Any

from pydantic import BaseModel


class FeatureResponse(BaseModel):
    index: int
    geometry_type: str | None
    geometry: dict[str, Any] | None
    map_geometry: dict[str, Any] | None
    map_error: str | None
    properties: dict[str, Any]
    source_crs: str | None
    projected_crs: str | None
    measurement: float | None
    unit: str | None
    processing_error: str | None


class UploadResponse(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: str
    features: list[FeatureResponse]


class FileResponse(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: str


class MeasurementsResponse(BaseModel):
    file_id: str
    features: list[FeatureResponse]


class HealthResponse(BaseModel):
    status: str
