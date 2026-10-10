import stat
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pyproj import CRS
from pyproj.exceptions import CRSError
from shapefile import Reader, ShapefileException
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    shape as shapely_shape,
)

from app.services.measurement_service import process_features


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2000
SHAPEFILE_REQUIRED_EXTENSIONS = {".shp", ".shx", ".dbf"}
KML_GEOMETRY_ELEMENTS = {"Point", "LineString", "Polygon"}


class FileProcessingError(ValueError):
    pass


def _safe_extract_shapefile(archive_path: Path, destination: Path) -> Path:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise FileProcessingError("The uploaded ZIP file is corrupt or unreadable") from error

    with archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise FileProcessingError("ZIP archive contains too many files")

        declared_size = sum(member.file_size for member in members if not member.is_dir())
        if declared_size > MAX_EXTRACTED_BYTES:
            raise FileProcessingError("ZIP archive expands beyond the allowed size")

        extracted_size = 0
        extracted_paths: list[Path] = []
        root = destination.resolve()
        seen_paths: set[str] = set()

        for member in members:
            normalized_name = member.filename.replace("\\", "/")
            member_path = PurePosixPath(normalized_name)
            if (
                not normalized_name
                or "\x00" in normalized_name
                or member_path.is_absolute()
                or PureWindowsPath(member.filename).drive
                or any(part in {"", ".", ".."} for part in member_path.parts)
            ):
                raise FileProcessingError("ZIP archive contains an unsafe file path")

            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise FileProcessingError("ZIP archive contains a symbolic link")

            output_path = (destination / Path(*member_path.parts)).resolve()
            if output_path != root and root not in output_path.parents:
                raise FileProcessingError("ZIP archive contains an unsafe file path")

            relative_key = member_path.as_posix().casefold()
            if relative_key in seen_paths:
                raise FileProcessingError("ZIP archive contains duplicate file paths")
            seen_paths.add(relative_key)
            if member.is_dir():
                output_path.mkdir(parents=True, exist_ok=True)
                continue

            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source, output_path.open("wb") as target:
                    while chunk := source.read(64 * 1024):
                        extracted_size += len(chunk)
                        if extracted_size > MAX_EXTRACTED_BYTES:
                            raise FileProcessingError(
                                "ZIP archive expands beyond the allowed size"
                            )
                        target.write(chunk)
            except (
                OSError,
                zipfile.BadZipFile,
                RuntimeError,
                NotImplementedError,
                EOFError,
            ) as error:
                raise FileProcessingError("Could not safely extract ZIP archive") from error
            extracted_paths.append(output_path)

    shapefiles = [path for path in extracted_paths if path.suffix.lower() == ".shp"]
    if not shapefiles:
        raise FileProcessingError("ZIP archive does not contain a Shapefile (.shp)")
    if len(shapefiles) > 1:
        raise FileProcessingError("ZIP archive contains multiple Shapefiles; expected one")

    shapefile = shapefiles[0]
    component_extensions = {
        path.suffix.lower()
        for path in extracted_paths
        if path.parent == shapefile.parent
        and path.stem.casefold() == shapefile.stem.casefold()
    }
    missing = SHAPEFILE_REQUIRED_EXTENSIONS - component_extensions
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise FileProcessingError(f"Shapefile is missing required component(s): {missing_names}")

    return shapefile


def _element_name(element: ET.Element) -> str:
    return element.tag.rpartition("}")[2]


def _element_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _element_name(child) == name:
            return child.text
    return None


def _kml_coordinates(element: ET.Element) -> list[tuple[float, ...]]:
    text = _element_text(element, "coordinates")
    if not text:
        return []
    coordinates = []
    for coordinate in text.split():
        values = tuple(float(value) for value in coordinate.split(","))
        if len(values) < 2:
            raise ValueError("KML coordinates must contain longitude and latitude")
        coordinates.append(values)
    return coordinates


def _kml_geometry(element: ET.Element) -> Any:
    kind = _element_name(element)
    if kind == "Point":
        coordinates = _kml_coordinates(element)
        return Point(coordinates[0]) if coordinates else None
    if kind == "LineString":
        coordinates = _kml_coordinates(element)
        return LineString(coordinates) if len(coordinates) >= 2 else None
    if kind == "Polygon":
        shell = None
        holes = []
        for boundary in element:
            boundary_name = _element_name(boundary)
            if boundary_name not in {"outerBoundaryIs", "innerBoundaryIs"}:
                continue
            ring = next(
                (child for child in boundary if _element_name(child) == "LinearRing"),
                None,
            )
            if ring is None:
                continue
            coordinates = _kml_coordinates(ring)
            if len(coordinates) < 4:
                continue
            if boundary_name == "outerBoundaryIs":
                shell = coordinates
            else:
                holes.append(coordinates)
        return Polygon(shell, holes) if shell else None
    if kind == "MultiGeometry":
        geometries = [
            geometry
            for child in element
            if (geometry := _kml_geometry(child)) is not None
        ]
        if not geometries:
            return None
        types = {geometry.geom_type for geometry in geometries}
        if len(types) > 1:
            return GeometryCollection(geometries)
        geometry_type = geometries[0].geom_type
        if geometry_type == "Point":
            return MultiPoint(geometries)
        if geometry_type == "LineString":
            return MultiLineString(geometries)
        if geometry_type == "Polygon":
            return MultiPolygon(geometries)
        return geometries[0]
    return None


def _kml_properties(placemark: ET.Element) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for child in placemark:
        name = _element_name(child)
        if name in {"name", "description"}:
            properties[name.title() if name == "name" else name] = child.text
        elif name == "ExtendedData":
            for data in child.iter():
                data_name = _element_name(data)
                key = data.attrib.get("name") if data_name in {"Data", "SimpleData"} else None
                if key:
                    value = _element_text(data, "value")
                    properties[key] = value if value is not None else data.text
    return properties


def _read_kml(path: Path) -> list[tuple[Any, dict[str, Any]]]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        raise FileProcessingError(f"Could not parse KML XML: {error}") from error
    if _element_name(root) != "kml":
        raise FileProcessingError("KML document has an invalid root element")

    features: list[tuple[Any, dict[str, Any]]] = []
    for placemark in root.iter():
        if _element_name(placemark) != "Placemark":
            continue
        for child in placemark:
            if _element_name(child) in KML_GEOMETRY_ELEMENTS | {"MultiGeometry"}:
                try:
                    geometry = _kml_geometry(child)
                except (ValueError, TypeError) as error:
                    raise FileProcessingError(
                        f"Could not parse KML geometry: {error}"
                    ) from error
                if geometry is not None:
                    features.append((geometry, _kml_properties(placemark)))
    return features


def _read_shapefile(path: Path) -> tuple[list[tuple[Any, dict[str, Any]]], CRS | None]:
    try:
        reader = Reader(str(path))
        features = [
            (
                shapely_shape(shape_record.shape.__geo_interface__)
                if shape_record.shape.shapeType != 0
                else None,
                shape_record.record.as_dict(),
            )
            for shape_record in reader.iterShapeRecords()
        ]
        reader.close()
    except (ShapefileException, OSError, UnicodeError, ValueError) as error:
        raise FileProcessingError(f"Could not read Shapefile data: {error}") from error

    projection_path = next(
        (
            candidate
            for candidate in path.parent.iterdir()
            if candidate.stem.casefold() == path.stem.casefold()
            and candidate.suffix.casefold() == ".prj"
        ),
        None,
    )
    source_crs = None
    if projection_path is not None:
        try:
            source_crs = CRS.from_wkt(projection_path.read_text(encoding="utf-8"))
        except (CRSError, OSError, UnicodeError) as error:
            raise FileProcessingError(f"Could not read Shapefile CRS: {error}") from error
    return features, source_crs


def process_uploaded_file(filename: str, content: bytes) -> dict[str, Any]:
    extension = Path(PureWindowsPath(filename).name).suffix.lower()
    if extension not in {".kml", ".zip"}:
        raise FileProcessingError("Unsupported file extension. Upload a .kml or .zip file")
    if not content:
        raise FileProcessingError("Uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise FileProcessingError("Uploaded file exceeds the 50 MB size limit")

    with tempfile.TemporaryDirectory(prefix="geospatial-upload-") as temporary_directory:
        work_directory = Path(temporary_directory)
        if extension == ".kml":
            source_path = work_directory / "upload.kml"
            source_path.write_bytes(content)
        else:
            archive_path = work_directory / "upload.zip"
            archive_path.write_bytes(content)
            source_path = _safe_extract_shapefile(archive_path, work_directory / "extracted")

        if extension == ".kml":
            source_features = _read_kml(source_path)
            source_crs = CRS.from_epsg(4326)
        else:
            source_features, source_crs = _read_shapefile(source_path)
        if not source_features:
            raise FileProcessingError("Uploaded geospatial file contains no features")
        crs, features = process_features(source_features, source_crs=source_crs)
        return {"crs": crs, "features": features}
