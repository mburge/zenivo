"""
Calibration library endpoints (dark frames).
"""

from fastapi import HTTPException, Query, Depends
from fastapi.responses import Response

from src.api.schemas import (
    MasterDarkRequest,
)
from src.api.service import AllSkyService
from src.api.routes.common import (router, get_service, _dark_preview_cache)


# ----------------------------------------------------------------------
# Calibration Library Endpoints (AC3.4)
# ----------------------------------------------------------------------

@router.get("/api/calibration/status")
def get_calibration_status(
    service: AllSkyService = Depends(get_service),
):
    """
    Retrieve real-time Master Dark calibration progress, status message,
    latest calibration result, and saved calibration library files.
    """
    return service.get_calibration_status()


@router.get("/api/calibration/dark-preview")
def get_dark_preview(
    file: str = Query(...),
    stats: bool = Query(False),
    service: AllSkyService = Depends(get_service),
):
    """Contrast-stretched PNG preview (or stats JSON with stats=1) of a saved native-RAW master dark."""
    res = _dark_preview_cache.get_or_compute(file, lambda: service.get_dark_preview(file))
    if res is None:
        raise HTTPException(status_code=404, detail="dark not found")
    png, st = res
    if stats:
        return st
    return Response(content=png, media_type="image/png")


@router.post("/api/calibration/auto-library")
def trigger_auto_dark_library(
    only_missing: bool = Query(default=False),
    service: AllSkyService = Depends(get_service),
):
    """
    Automatically capture and median-stack all 6 standard Master Dark profiles
    (Twilight to High-Sensitivity Night) without requiring manual parameter selection.
    """
    return service.trigger_auto_dark_library(only_missing=only_missing)


@router.post("/api/calibration/master-dark")
@router.post("/api/calibration/dark")
def trigger_master_dark(
    req: MasterDarkRequest,
    service: AllSkyService = Depends(get_service),
):
    """
    Capture N frames with matching exposure and gain, median stack into Master Dark,
    detect hot pixels (DN >= 100), and store to calibration library.
    """
    return service.trigger_master_dark(req.num_frames, req.exposure_s, req.gain_db)
