import math
from typing import Any

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import transform as transform_geometry
from shapely.errors import GEOSException
from shapely.geometry import mapping


AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString"}
SUPPORTED_TYPES = AREA_TYPES | LENGTH_TYPES | {"Point", "MultiPoint"}


def _utm_crs_for_geometry(geometry: Any, source_crs: CRS) -> CRS:
    to_geographic = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
    geographic_geometry = transform_geometry(
        geometry, to_geographic.transform, interleaved=False
    )
    center = geographic_geometry.representative_point()
    longitude, latitude = center.x, center.y

    if not (-180 <= longitude <= 180 and -80 <= latitude <= 84):
        raise ValueError("Feature is outside the supported UTM latitude/longitude range")

    zone = min(int((longitude + 180) // 6) + 1, 60)
    epsg_code = (32600 if latitude >= 0 else 32700) + zone
    return CRS.from_epsg(epsg_code)


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def process_features(
    source_features: list[tuple[Any, dict[str, Any]]],
    *,
    source_crs: CRS | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    resolved_crs = CRS.from_user_input(source_crs) if source_crs else None
    crs_text = resolved_crs.to_string() if resolved_crs else None
    processed: list[dict[str, Any]] = []

    for feature_index, (geometry, raw_properties) in enumerate(source_features):
        geometry_type = geometry.geom_type if geometry is not None else None
        properties = {str(key): _json_value(value) for key, value in raw_properties.items()}
        item: dict[str, Any] = {
            "index": feature_index,
            "geometry_type": geometry_type,
            "geometry": _json_value(mapping(geometry)) if geometry is not None else None,
            "map_geometry": None,
            "map_error": None,
            "properties": properties,
            "source_crs": crs_text,
            "projected_crs": None,
            "measurement": None,
            "unit": None,
            "processing_error": None,
        }

        if geometry is not None and not geometry.is_empty and resolved_crs is not None:
            try:
                to_geographic = Transformer.from_crs(
                    resolved_crs, CRS.from_epsg(4326), always_xy=True
                )
                geographic_geometry = transform_geometry(
                    geometry, to_geographic.transform, interleaved=False
                )
                item["map_geometry"] = _json_value(mapping(geographic_geometry))
            except (
                ValueError,
                TypeError,
                ArithmeticError,
                ProjError,
                GEOSException,
            ) as error:
                item["map_error"] = str(error)

        if geometry is None:
            item["processing_error"] = "Feature has null geometry"
        elif geometry.is_empty:
            item["processing_error"] = "Feature has empty geometry"
        elif geometry_type not in SUPPORTED_TYPES:
            item["processing_error"] = f"Unsupported geometry type: {geometry_type}"
        elif not geometry.is_valid:
            item["processing_error"] = "Feature geometry is invalid"
        elif geometry_type in AREA_TYPES | LENGTH_TYPES:
            if resolved_crs is None:
                item["processing_error"] = "Cannot measure feature because its CRS is missing"
            else:
                try:
                    projected_crs = _utm_crs_for_geometry(geometry, resolved_crs)
                    to_projected = Transformer.from_crs(
                        resolved_crs, projected_crs, always_xy=True
                    )
                    projected = transform_geometry(
                        geometry, to_projected.transform, interleaved=False
                    )
                    item["projected_crs"] = projected_crs.to_string()
                    if geometry_type in AREA_TYPES:
                        item["measurement"] = float(projected.area)
                        item["unit"] = "m²"
                    else:
                        item["measurement"] = float(projected.length)
                        item["unit"] = "m"
                except (
                    ValueError,
                    TypeError,
                    ArithmeticError,
                    ProjError,
                    GEOSException,
                ) as error:
                    item["processing_error"] = f"Could not measure feature: {error}"
        processed.append(item)

    return crs_text, processed
