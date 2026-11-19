"""
ADS-B, traffic, day objects, radiosondes, satellites and map endpoints.
"""

from typing import Optional
from fastapi import HTTPException, Path, Query, Request, status, Depends

from src.api.schemas import (
    AdsbConfigRequest,
    DayObjectsConfigRequest,
    SatellitesConfigRequest,
    WeatherSettingsRequest,
)
from src.api.service import AllSkyService
from src.api.routes.common import (router, get_service, public_view, _check_range, _check_ts, _sat_cache, _traffic_cache, _dayobj_cache, _acinfo_cache, GUEST_MAX_SAT_IDS, GUEST_MAX_TRAIL_S)


@router.get("/api/adsb")
def get_adsb_config(request: Request, service: AllSkyService = Depends(get_service)):
    """ADS-B flight matching: receiver address and live feed status (receiver address and FR24 account e-mail: admin only)."""
    from src.auth.middleware import is_admin
    cfg = service.get_adsb_config()
    if not is_admin(request):
        cfg = dict(cfg, host="", network_host="", fr24_email="", fr24=dict(cfg.get("fr24") or {}, email=""))
    return cfg


@router.get("/api/adsb/aircraft")
def get_adsb_aircraft(limit: int = Query(default=30, ge=1, le=6000),
                      trail: float = Query(default=0.0, ge=0.0, le=900.0),
                      bounds: Optional[str] = Query(default=None, max_length=120),
                      fr24: Optional[bool] = Query(default=None),
                      compact: bool = Query(default=False),
                      at: Optional[float] = Query(default=None, description="recent frame midpoint, epoch seconds"),
                      request: Request = None,
                      service: AllSkyService = Depends(get_service)):
    """Aircraft heard right now (local ADS-B + Flightradar24 global feed): flight, altitude, speed, heading,
    distance, direction, in camera view. trail=<seconds> adds lat/lon and each aircraft's recent path (3D map);
    bounds=north,south,west,east or 'global' queries Flightradar24 for that region or the whole globe.
    For guests a box is snapped to a 1-degree grid and must lie within 500 km of the station (else 400)."""
    from src.auth.middleware import is_admin
    if at is not None:
        _check_ts(at=at)
    if bounds and not is_admin(request):
        from src.analytics.fr24 import normalize_guest_bounds
        try:
            bounds = normalize_guest_bounds(bounds, float(service.station_config.get("latitude_deg", 0.0)),
                                            float(service.station_config.get("longitude_deg", 0.0)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return public_view(request, service, service.get_aircraft_now(
        limit=limit, trail_s=trail, bounds=bounds, include_fr24=fr24, compact=compact, at=at))


@router.get("/api/traffic")
def get_traffic(t0: float = Query(description="start, epoch seconds"),
                t1: float = Query(description="end, epoch seconds"),
                limit: int = Query(default=2000, ge=1, le=20000),
                request: Request = None,
                service: AllSkyService = Depends(get_service)):
    """Aircraft and satellites that were inside the camera view between t0 and t1 (replay / name an event's flight).
    Rows: a = [icao, callsign, reg, type, lat, lon, alt_m, speed_kmh, heading, az, el, x, y], s = [norad, name, az, el, x, y, range_km, sunlit]."""
    _check_range(request, t0, t1)
    samples = _traffic_cache.get_or_compute((int(t0 // 10), int(t1 // 10), limit),
                                            lambda: service.traffic_log().query(t0, t1, limit))
    return public_view(request, service, {"samples": samples})


@router.get("/api/traffic/status")
def get_traffic_status(service: AllSkyService = Depends(get_service)):
    """Traffic log settings (retention days, size cap, interval) and how much is stored."""
    return service.traffic_log().status()


@router.get("/api/day-objects")
def get_day_objects(t0: float = Query(description="start, epoch seconds"),
                    t1: float = Query(description="end, epoch seconds"),
                    limit: int = Query(default=2000, ge=1, le=20000),
                    request: Request = None,
                    service: AllSkyService = Depends(get_service)):
    """Birds, insects and aircraft followed by day: class, size, speed (frame widths/s), straightness and the
    path as [seconds since start, x, y] frame fractions."""
    _check_range(request, t0, t1)
    tracks = _dayobj_cache.get_or_compute((int(t0 // 10), int(t1 // 10), limit),
                                          lambda: service.day_object_log().query(t0, t1, limit))
    return {"tracks": tracks}


@router.get("/api/day-objects/status")
def get_day_objects_status(service: AllSkyService = Depends(get_service)):
    """Stored day-object tracks and the live tracker state (rain detected from drops on the dome, etc.)."""
    tracker = getattr(service, "day_objects", None)
    return {"log": service.day_object_log().status(), "tracker": tracker.status() if tracker is not None else None,
            **service.get_day_objects_config()}


@router.get("/api/day-objects/config")
def get_day_objects_config(service: AllSkyService = Depends(get_service)):
    """Daytime object detection settings (classes recorded, sensitivity, blob size, contrast, retention) and the presets."""
    return service.get_day_objects_config()


@router.post("/api/day-objects/config")
def set_day_objects_config(req: DayObjectsConfigRequest, service: AllSkyService = Depends(get_service)):
    """Change the daytime object detection settings (persisted, applied live). Admin only like every write."""
    fields = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None or k == "min_contrast_dn"}
    return service.set_day_objects_config(**fields)


@router.get("/api/radiosondes")
def get_radiosondes(limit: int = Query(default=40, ge=1, le=200),
                    request: Request = None,
                    service: AllSkyService = Depends(get_service)):
    """Weather balloons (SondeHub radiosondes) near the station: altitude, climb, distance, direction, in camera view."""
    return public_view(request, service, service.get_radiosondes_now(limit=limit))


@router.get("/api/adsb/info/{icao}")
def get_adsb_aircraft_info(icao: str = Path(pattern=r"^[0-9A-Fa-f]{6}$"),
                           callsign: str = Query(default="", pattern=r"^[A-Za-z0-9]{0,8}$"),
                           service: AllSkyService = Depends(get_service)):
    """Type, registration, airline and route of one aircraft (tap card on the live picture). Unknown parts are
    looked up in the background, so an empty answer is worth one retry after a few seconds."""
    key = (icao.upper(), callsign.upper())
    return _acinfo_cache.get_or_compute(key, lambda: service.get_aircraft_info(key[0], key[1]))


@router.get("/api/adsb/flight/{fr24_id}")
def get_adsb_flight_detail(fr24_id: str, service: AllSkyService = Depends(get_service)):
    """Full Flightradar24 flight detail and complete historical trail from departure airport."""
    try:
        return service.get_fr24_flight_detail(fr24_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/api/satellites")
def get_satellites(track: float = Query(default=0.0, ge=0.0, le=120.0),
                   below: bool = Query(default=False),
                   limit: int = Query(default=60, ge=1, le=400),
                   ids: str = Query(default=""),
                   trail: float = Query(default=0.0, ge=0.0, le=1800.0),
                   request: Request = None,
                   service: AllSkyService = Depends(get_service)):
    """Large satellites above the horizon now: az/alt, range, sunlit, lat/lon/height and position in the camera
    frame. track=<minutes> adds each of the highest ones' path (5 min back, <minutes> ahead); below=true also
    lists those under the horizon (3D map); ids=<norad,...> also tracks those."""
    from src.auth.middleware import is_admin
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    if not is_admin(request):
        id_list = id_list[:GUEST_MAX_SAT_IDS]
        trail = min(trail, GUEST_MAX_TRAIL_S)
    track_ids = frozenset(id_list)
    raw = _sat_cache.get_or_compute(
        (round(track, 1), below, limit, tuple(sorted(track_ids)), round(trail, 0)),
        lambda: service.get_satellites_now(track_min=track, below=below, limit=limit, trail_s=trail,
                                           track_ids=set(track_ids)))
    return public_view(request, service, raw)


@router.get("/api/map/config")
def get_map_config(service: AllSkyService = Depends(get_service)):
    """Basemap settings for the 3D flights map (Google Maps key, if configured)."""
    return service.get_map_config()


@router.get("/api/map/airports")
def get_map_airports(service: AllSkyService = Depends(get_service)):
    """Airports and runways around the station (OurAirports, public domain) for the 3D flights map."""
    return service.get_airports()


@router.get("/api/map/airports/frame")
def get_map_airports_frame(service: AllSkyService = Depends(get_service)):
    """Airports inside the camera picture (frame fractions + ICAO/IATA) for the Airports layer on the live view."""
    return _acinfo_cache.get_or_compute(("airports-frame",), service.get_airports_frame)


@router.post("/api/adsb")
def set_adsb_config(req: AdsbConfigRequest, service: AllSkyService = Depends(get_service)):
    """Set the ADS-B receiver (SBS-1 feed host/port), Flightradar24 Enterprise credentials, or turn flight matching on/off (persisted)."""
    try:
        return service.set_adsb_config(**req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/api/satellites/config")
def get_satellites_config(service: AllSkyService = Depends(get_service)):
    """Satellite overlay settings and the state of the orbit download (admin)."""
    return service.get_satellites_config()


@router.post("/api/satellites/config")
def set_satellites_config(req: SatellitesConfigRequest, service: AllSkyService = Depends(get_service)):
    """Switch the satellite overlay, choose the CelesTrak groups, the refresh interval and an alternative TLE source."""
    try:
        return service.set_satellites_config(enabled=req.enabled, groups=req.groups, refresh_h=req.refresh_h,
                                             tle_url=req.tle_url, source=req.source)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/api/satellites/refresh")
def refresh_satellites(service: AllSkyService = Depends(get_service)):
    """Download the orbits now instead of waiting for the next scheduled refresh."""
    return service.refresh_satellites()


@router.get("/api/data-sources")
def get_data_sources(service: AllSkyService = Depends(get_service)):
    """Every outside data source (ADS-B, FR24, SondeHub, CelesTrak, airports): on or off, working or not (admin)."""
    return service.get_data_sources()


# Weather and forecast around the station (src/weather.py): reading is public, switching it on is admin
@router.get("/api/weather")
def get_weather(service: AllSkyService = Depends(get_service)):
    """Current conditions and the next 48 h for the station position (empty until the admin enables it)."""
    return service.weather.status()


@router.post("/api/weather/settings")
def set_weather_settings(req: WeatherSettingsRequest, service: AllSkyService = Depends(get_service)):
    """Turn the weather source on or off and pick the refresh interval (admin)."""
    try:
        service.weather.set_settings(req.enabled, req.refresh_min)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    service.activity.info("station", f"Weather source {'on' if service.weather.settings()['enabled'] else 'off'}")
    return service.weather.status()


@router.post("/api/weather/refresh")
def refresh_weather(service: AllSkyService = Depends(get_service)):
    """Fetch the weather now instead of waiting for the next refresh (admin)."""
    return {"started": service.weather.refresh_async(), **service.weather.status()}
