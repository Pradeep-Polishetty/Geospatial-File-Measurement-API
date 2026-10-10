from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pymysql import MySQLError

from app.routes.files import router as files_router
from app.schemas.file import HealthResponse
from app.utils.database import get_connection, initialize_database


app = FastAPI(
    title="Geospatial File Measurement API",
    description="Upload KML or zipped Shapefiles to calculate per-feature measurements.",
    version="1.0.0",
)
app.include_router(files_router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health", response_model=HealthResponse, tags=["health"])
def health_check() -> dict[str, str]:
    connection = None
    try:
        connection = get_connection()
        initialize_database(connection)
    except (MySQLError, RuntimeError) as error:
        raise HTTPException(
            status_code=503, detail="Database unavailable."
        ) from error
    finally:
        if connection is not None:
            connection.close()
    return {"status": "ok"}
