import json
from typing import Any

from pymysql import MySQLError

from app.utils.database import decode_json_value, get_connection, initialize_database


def _feature_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": record["feature_index"],
        "geometry_type": record["geometry_type"],
        "geometry": decode_json_value(record["geometry"]),
        "map_geometry": decode_json_value(record["map_geometry"]),
        "map_error": record["map_error"],
        "properties": decode_json_value(record["properties"]),
        "source_crs": record["source_crs"],
        "projected_crs": record["projected_crs"],
        "measurement": record["measurement"],
        "unit": record["unit"],
        "processing_error": record["processing_error"],
    }


def save_processed_file(
    file_id: str,
    filename: str,
    crs: str | None,
    features: list[dict[str, Any]],
) -> None:
    connection = get_connection()
    try:
        initialize_database(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO files (id, filename, feature_count, crs, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (file_id, filename, len(features), crs, "COMPLETED"),
            )
            cursor.executemany(
                """
                INSERT INTO features (
                    file_id, feature_index, geometry_type, geometry, map_geometry,
                    map_error, properties, source_crs, projected_crs, measurement,
                    unit, processing_error
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                [
                    (
                        file_id,
                        feature["index"],
                        feature["geometry_type"],
                        json.dumps(feature["geometry"], allow_nan=False)
                        if feature["geometry"] is not None
                        else None,
                        json.dumps(feature["map_geometry"], allow_nan=False)
                        if feature["map_geometry"] is not None
                        else None,
                        feature["map_error"],
                        json.dumps(feature["properties"], allow_nan=False),
                        feature["source_crs"],
                        feature["projected_crs"],
                        feature["measurement"],
                        feature["unit"],
                        feature["processing_error"],
                    )
                    for feature in features
                ],
            )
        connection.commit()
    except (MySQLError, TypeError, ValueError):
        connection.rollback()
        raise
    finally:
        connection.close()


def get_file(file_id: str) -> dict[str, Any] | None:
    connection = get_connection()
    try:
        initialize_database(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, filename, feature_count, crs, status
                FROM files WHERE id = %s
                """,
                (file_id,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def get_features(file_id: str) -> list[dict[str, Any]] | None:
    connection = get_connection()
    try:
        initialize_database(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM files WHERE id = %s", (file_id,))
            if cursor.fetchone() is None:
                return None
            cursor.execute(
                """
                SELECT feature_index, geometry_type, geometry, map_geometry,
                       map_error, properties, source_crs, projected_crs,
                       measurement, unit, processing_error
                FROM features WHERE file_id = %s ORDER BY feature_index
                """,
                (file_id,),
            )
            return [_feature_from_record(record) for record in cursor.fetchall()]
    finally:
        connection.close()
