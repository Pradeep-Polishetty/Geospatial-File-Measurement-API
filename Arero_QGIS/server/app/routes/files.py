import csv
import io
import json
import uuid
from pathlib import PureWindowsPath
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from pymysql import MySQLError

from app.schemas.file import (
    FeatureResponse,
    FileResponse,
    MeasurementsResponse,
    UploadResponse,
)
from app.services.file_service import (
    MAX_UPLOAD_BYTES,
    FileProcessingError,
    process_uploaded_file,
)
from app.services.storage_service import get_features, get_file, save_processed_file


router = APIRouter(prefix="/api/files", tags=["files"])


def _load_export_records(file_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        record = get_file(file_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
        features = get_features(file_id)
    except (MySQLError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_database_error_detail(error, "export file data"),
        ) from error
    if features is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return record, features


def _export_feature_id(file_id: str, feature: dict[str, Any]) -> str:
    return f"{file_id}:{feature['index']}"


def _database_error_detail(error: MySQLError | RuntimeError, action: str) -> str:
    if isinstance(error, MySQLError) and error.args and error.args[0] == 1049:
        return (
            "The configured database does not exist. Create the application database "
            "in TiDB Cloud or set DB_DATABASE to an existing database."
        )
    if isinstance(error, MySQLError) and error.args and error.args[0] == 1142:
        return (
            "The database user cannot create tables in this schema. "
            "Select an application database and grant it CREATE privileges."
        )
    return f"Could not {action} in the database. Check the database configuration and try again."


@router.post("/", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(file: UploadFile = File(...)) -> dict:
    filename = PureWindowsPath(file.filename or "").name
    extension = PureWindowsPath(filename).suffix.lower()
    if extension not in {".kml", ".zip"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file extension. Upload a .kml or .zip file",
        )

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file exceeds the 50 MB size limit",
        )
    try:
        result = process_uploaded_file(filename, content)
    except FileProcessingError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error

    features = [FeatureResponse.model_validate(feature) for feature in result["features"]]
    file_id = str(uuid.uuid4())
    try:
        save_processed_file(file_id, filename, result["crs"], [item.model_dump() for item in features])
    except (MySQLError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_database_error_detail(error, "save the processed file"),
        ) from error

    return {
        "id": file_id,
        "filename": filename,
        "feature_count": len(features),
        "crs": result["crs"],
        "status": "COMPLETED",
        "features": features,
    }


@router.get("/{file_id}/export.csv", response_class=Response)
def export_csv(file_id: str) -> Response:
    file_record, features = _load_export_records(file_id)
    output = io.StringIO(newline="")
    fieldnames = (
        "feature_id",
        "feature_index",
        "geometry_type",
        "coordinates",
        "crs",
        "source_crs",
        "projected_crs",
        "properties",
        "measurement",
        "measurement_unit",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for feature in features:
        geometry = feature["geometry"]
        coordinates = None
        if isinstance(geometry, dict):
            coordinates = geometry.get("coordinates", geometry.get("geometries"))
        writer.writerow(
            {
                "feature_id": _export_feature_id(file_id, feature),
                "feature_index": feature["index"],
                "geometry_type": feature["geometry_type"] or "",
                "coordinates": json.dumps(coordinates, ensure_ascii=False)
                if coordinates is not None
                else "",
                "crs": feature["source_crs"] or file_record["crs"] or "",
                "source_crs": feature["source_crs"] or "",
                "projected_crs": feature["projected_crs"] or "",
                "properties": json.dumps(feature["properties"], ensure_ascii=False),
                "measurement": feature["measurement"]
                if feature["measurement"] is not None
                else "",
                "measurement_unit": feature["unit"] or "",
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="features.csv"'},
    )


@router.get("/{file_id}/export.geojson", response_class=Response)
def export_geojson(file_id: str) -> Response:
    file_record, features = _load_export_records(file_id)
    geojson_features = []
    for feature in features:
        source_crs = feature["source_crs"] or file_record["crs"]
        properties = {
            "feature_id": _export_feature_id(file_id, feature),
            "feature_index": feature["index"],
            "geometry_type": feature["geometry_type"],
            "crs": "EPSG:4326",
            "source_crs": source_crs,
            "projected_crs": feature["projected_crs"],
            "properties": feature["properties"],
            "measurement": feature["measurement"],
            "measurement_unit": feature["unit"],
        }
        if feature["map_geometry"] is None and feature["geometry"] is not None:
            properties["source_geometry"] = feature["geometry"]
        geojson_features.append(
            {
                "type": "Feature",
                "id": _export_feature_id(file_id, feature),
                "geometry": feature["map_geometry"],
                "properties": properties,
            }
        )
    content = json.dumps(
        {"type": "FeatureCollection", "features": geojson_features},
        ensure_ascii=False,
        allow_nan=False,
    )
    return Response(
        content=content,
        media_type="application/geo+json",
        headers={"Content-Disposition": 'attachment; filename="features.geojson"'},
    )


@router.get("/{file_id}/", response_model=FileResponse)
def read_file(file_id: str) -> dict:
    try:
        record = get_file(file_id)
    except (MySQLError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_database_error_detail(error, "read file information"),
        ) from error
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return record


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def read_measurements(file_id: str) -> dict:
    try:
        features = get_features(file_id)
    except (MySQLError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_database_error_detail(error, "read measurements"),
        ) from error
    if features is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return {"file_id": file_id, "features": features}
