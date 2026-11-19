"""
Pydantic Request and Response Schemas for IMX335 Edge Web API.
Adheres to PROJECT.md § Interface Contracts and AC3 verification specifications.
"""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class ExposureControlRequest(BaseModel):
    """Request payload for updating camera hardware exposure."""
    exposure_s: float = Field(..., ge=0.00015, le=90.0, description="Exposure duration in seconds")
    gain_db: Optional[float] = Field(None, ge=0.0, le=72.0, description="Analog/digital gain in dB")


class GainControlRequest(BaseModel):
    """Request payload for updating sensor analog/digital gain."""
    gain_db: float = Field(..., ge=0.0, le=72.0, description="Sensor gain in dB")


class CameraModeRequest(BaseModel):
    """Request payload for toggling camera auto/manual exposure mode."""
    mode: Optional[str] = Field(None, description="Exposure mode: 'auto' or 'manual'")
    auto: Optional[bool] = Field(None, description="Boolean flag: True for auto, False for manual")


class AeLockRequest(BaseModel):
    """Auto exposure with one knob held: lock 'exposure' or 'gain' (at value, default the current one), or 'none'."""
    lock: str = Field(..., description="'none', 'exposure', 'gain' or 'both' (fixed exposure and gain)")
    value: Optional[float] = Field(None, ge=0.0, le=90.0, description="Locked exposure (s) or gain (dB)")
    exposure_s: Optional[float] = Field(None, ge=0.0, le=90.0, description="Exposure for lock 'both'")
    gain_db: Optional[float] = Field(None, ge=0.0, le=90.0, description="Gain for lock 'both'")


class TargetBrightnessRequest(BaseModel):
    """Lock the auto-exposure target brightness (mean 0-255), or null to use the built-in day/night targets."""
    value: Optional[float] = Field(None, ge=0.0, le=255.0)


class MasterDarkRequest(BaseModel):
    """Request payload for generating a Master Dark calibration stack."""
    num_frames: int = Field(default=9, ge=3, le=50, description="Number of dark frames to stack")
    exposure_s: float = Field(default=2.0, ge=0.001, le=90.0, description="Exposure duration in seconds")
    gain_db: float = Field(default=36.0, ge=0.0, le=72.0, description="Sensor gain in dB")


class ImageTransformRequest(BaseModel):
    """Request payload for image orientation: mirror (horizontal), flip (vertical) and rotation."""
    flip_horizontal: Optional[bool] = Field(None, description="Mirror image left-right")
    flip_vertical: Optional[bool] = Field(None, description="Flip image upside-down")
    rotate_deg: Optional[int] = Field(None, description="Rotation in degrees: 0 or 180")
    rotate_isp_180: Optional[bool] = Field(
        None, description="Extra 180° rotation for day (camera ISP / MJPG) frames only; RAW10 night frames unchanged"
    )


class WhiteBalanceRequest(BaseModel):
    """Request payload for RAW10 white balance."""
    mode: Optional[str] = Field(None, pattern="^(auto|manual)$", description="'auto' or 'manual'")
    r_gain: Optional[float] = Field(None, ge=0.4, le=4.0, description="Manual red gain relative to green")
    b_gain: Optional[float] = Field(None, ge=0.4, le=4.0, description="Manual blue gain relative to green")


class OutputResolutionRequest(BaseModel):
    """Request payload for selecting the output image resolution preset."""
    width: int = Field(..., ge=320, le=12000, description="Output width in pixels")
    height: int = Field(..., ge=240, le=12000, description="Output height in pixels")


class CameraSelectRequest(BaseModel):
    """Request payload for switching the active camera."""
    type: str = Field(..., pattern="^(auto|rts5869|picamera|zwo|qhy|v4l2|indi|simulator)$", description="Camera backend")
    index: int = Field(0, ge=0, le=15, description="Camera number within that backend")


class StarBoostRequest(BaseModel):
    """Request payload for night star boost (software sum-binning of the viewer frame)."""
    enabled: Optional[bool] = Field(None, description="Turn star boost on or off")
    bin: Optional[int] = Field(None, ge=2, le=4, description="NxN sum-binning window")


class PerformanceSettingsRequest(BaseModel):
    """Request payload for CPU/quality trade-offs; omitted fields stay unchanged."""
    min_capture_interval_s: Optional[float] = Field(None, ge=0.4, le=60, description="Minimum seconds between captures")
    analysis_max_width: Optional[int] = Field(None, ge=320, le=2592, description="Star/cloud analysis width")
    sky_analysis_enabled: Optional[bool] = Field(None, description="Turn star count / cloud cover sampling on or off")
    analysis_interval_s: Optional[float] = Field(
        None, ge=10, le=3600, description="Seconds between star/cloud samples"
    )
    stream_max_width: Optional[int] = Field(None, ge=320, le=2592, description="Live stream JPEG width")
    stream_jpeg_quality: Optional[int] = Field(None, ge=40, le=95, description="Live stream JPEG quality")
    background_capture_interval_s: Optional[float] = Field(
        None, ge=0, le=3600, description="Capture with no viewer: 0 = back to back, N = every N seconds"
    )


class EventConfigRequest(BaseModel):
    """Turn detection of each sky event type on or off; omitted types stay unchanged."""
    meteor: Optional[bool] = None
    plane: Optional[bool] = None
    satellite: Optional[bool] = None
    clouds: Optional[bool] = None
    flash: Optional[bool] = None


class DayObjectsConfigRequest(BaseModel):
    """Daytime object detection (birds, insects, aircraft). Omitted fields stay as they are; min_contrast_dn null goes
    back to the sensitivity preset."""
    enabled: Optional[bool] = None
    classes: Optional[List[Literal["plane", "bird", "insect", "object"]]] = None
    sensitivity: Optional[Literal["low", "normal", "high"]] = None
    min_blob_area: Optional[int] = Field(default=None, ge=1, le=100, description="px at 640 px work width")
    max_blob_area: Optional[int] = Field(default=None, ge=5, le=5000, description="px at 640 px work width")
    min_contrast_dn: Optional[float] = Field(default=None, ge=3.0, le=100.0)
    retention_days: Optional[int] = Field(default=None, ge=1, le=90)
    max_mb: Optional[int] = Field(default=None, ge=5, le=500)


class AdsbConfigRequest(BaseModel):
    """ADS-B receiver (SBS-1 / BaseStation feed, usually port 30003) + Flightradar24 Enterprise global feed settings."""
    host: Optional[str] = Field(default=None, max_length=253)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    enabled: Optional[bool] = None
    public_fallback: Optional[bool] = Field(default=None, description="Use a public ADS-B network (adsb.fi, then adsb.lol) while the receiver is not delivering; sends the station position rounded to 0.5 degree")
    contribute: Optional[bool] = Field(default=None, description="Share what this station's own receiver hears with the Zenivo network (signed with the station key); never forwards public-network or Flightradar24 data")
    fr24_enabled: Optional[bool] = None
    fr24_email: Optional[str] = Field(default=None, max_length=254, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$",
                                      description="Flightradar24 account e-mail (empty clears it); anything that is not an e-mail address is refused")
    fr24_password: Optional[str] = Field(default=None, max_length=254)
    fr24_token: Optional[str] = Field(default=None, max_length=1024)
    radiosondes_enabled: Optional[bool] = Field(default=None, description="Weather balloons (SondeHub) on the Live page")


class CloudConfigRequest(BaseModel):
    """Request payload for cloud cover analysis: sky region and/or forgetting the learned clear-sky reference."""
    sky_roi: Optional[List[float]] = Field(
        None, min_length=4, max_length=4, description="Sky region as fractions [left, top, right, bottom]"
    )
    reset_reference: bool = Field(False, description="Forget the learned clear-sky reference (after re-aiming the camera)")


class SkyMaskRequest(BaseModel):
    """Plate-solve sky mask: polygons in frame fractions [[x, y], ...] (0..1, x right, y down)."""
    sky: List[List[List[float]]] = Field(
        default_factory=list, max_length=16, description="Open-sky polygons (none = the whole frame)"
    )
    obstruction: List[List[List[float]]] = Field(
        default_factory=list, max_length=16, description="Buildings, trees and lit facades to ignore"
    )


class SatellitesConfigRequest(BaseModel):
    """Satellite overlay settings (Settings > Integrations): which CelesTrak groups, how often, and from where."""
    enabled: Optional[bool] = None
    groups: Optional[List[str]] = Field(default=None, max_length=8, description="CelesTrak groups to track")
    refresh_h: Optional[int] = Field(default=None, ge=1, le=168, description="Hours between orbit downloads")
    tle_url: Optional[str] = Field(default=None, max_length=300, description="https address with {group}; empty = the chosen source")
    source: Optional[str] = Field(default=None, pattern="^(celestrak|satnogs)$", description="Orbit source: CelesTrak groups or the SatNOGS DB catalogue")


class ManualStarSolveRequest(BaseModel):
    """Stars clicked on the live frame (fractions of the displayed image) to find the camera pointing."""
    points: List[List[float]] = Field(..., min_length=4, max_length=80, description="[x, y] fractions, x right, y down")
    apply: bool = Field(False, description="Store the result; false only identifies the stars")


class FocusConfigRequest(BaseModel):
    """Focus assistant region of interest and measurement method."""
    cx: Optional[float] = Field(None, ge=0.0, le=1.0, description="ROI centre x as a fraction of the frame width")
    cy: Optional[float] = Field(None, ge=0.0, le=1.0, description="ROI centre y as a fraction of the frame height")
    roi_mode: Optional[str] = Field(None, pattern="^(auto|manual)$", description="auto: automatic focus target; manual: user-locked ROI")
    roi_size: Optional[int] = Field(None, ge=64, le=1024, description="ROI side in native pixels")
    method: Optional[str] = Field(None, pattern="^(auto|stars|lights|contrast)$", description="Focus measure")
    smooth: Optional[int] = Field(None, ge=1, le=30, description="Frames in the robust running average of the score")
    reset: bool = Field(False, description="Forget the score history and best score")
    autofocus: Optional[bool] = Field(None, description="start only: sweep autofocus right away; default: lens mode is AF")


class LensRequest(BaseModel):
    """Manual focus of a motorised lens, in dioptres (1 / distance in m; 0 = infinity)."""
    position: Optional[float] = Field(None, ge=0.0, le=32.0, description="Absolute lens position (D)")
    delta: Optional[float] = Field(None, ge=-5.0, le=5.0, description="Relative move (D)")
    tracking: Optional[bool] = Field(None, description="Night focus tracking (temperature drift) on/off")
    mode: Optional[str] = Field(None, pattern="^(auto|manual)$", description="auto: temperature compensation + nightly star checks; manual: hold")
    reset_learning: bool = Field(False, description="Forget the learned focus-vs-temperature samples")
    refocus_every_h: Optional[float] = Field(None, ge=0, le=48, description="AF: full refocus sweep period in hours (0 = off)")


class LensAutofocusRequest(BaseModel):
    mode: str = Field("sweep", pattern="^(sweep|camera)$", description="sweep: this app's score; camera: libcamera AF")


class StationLocationRequest(BaseModel):
    """Request payload for setting station coordinates and/or manually adjusting optical & orientation parameters."""
    latitude_deg: Optional[float] = Field(default=None, ge=-90.0, le=90.0, description="Station latitude in degrees (-90 to +90)")
    longitude_deg: Optional[float] = Field(default=None, ge=-180.0, le=180.0, description="Station longitude in degrees (-180 to +180)")
    latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0, description="Alias for latitude_deg")
    longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0, description="Alias for longitude_deg")
    elevation_m: float = Field(default=20.0, ge=-500.0, le=9000.0, description="Station elevation in meters")
    location_name: Optional[str] = Field(default=None, description="Station name or city")
    station_name: Optional[str] = Field(default=None, description="Alias for location_name")
    geo_source: Optional[str] = Field(default="manual", description="Coordinate source: browser_gps, ip_geolocation, or manual")
    fov_hint: Optional[str] = Field(default="auto", description="Optional FOV hint in degrees or 'auto'")
    auto_plate_solve: bool = Field(default=False, description="Only run plate-solving when explicitly requested")
    # Optional manual optical & orientation overrides ("fix, settings" on Web UI)
    lens_model: Optional[str] = None
    focal_length_mm: Optional[float] = None
    pixel_scale_arcsec_px: Optional[float] = None
    fov_horizontal_deg: Optional[float] = None
    fov_vertical_deg: Optional[float] = None
    fov_diagonal_deg: Optional[float] = None
    optical_center_x_px: Optional[float] = None
    optical_center_y_px: Optional[float] = None
    horizon_radius_90_px: Optional[float] = None
    azimuth_deg: Optional[float] = None
    altitude_deg: Optional[float] = None
    zenith_tilt_deg: Optional[float] = None
    north_position_angle_deg: Optional[float] = None
    remote_solver_url: Optional[str] = Field(default=None, pattern=r"^(https://virtuallab3d\.com)?$", description="Remote plate solver, chosen from a list: https://virtuallab3d.com (default) or empty (never upload)")


class HistogramStats(BaseModel):
    mean: float
    stddev: float
    median: int
    clipping_low_pct: float
    clipping_high_pct: float


class HistogramResponse(BaseModel):
    r: List[int]
    g: List[int]
    b: List[int]
    y: List[int]
    calculation_time_ms: float
    stats: HistogramStats


class GalleryItem(BaseModel):
    type: str
    url: str
    title: Optional[str] = None
    timestamp: Optional[str] = None


class GalleryResponse(BaseModel):
    date: str
    items: List[GalleryItem]


class MeteorEventItem(BaseModel):
    event_id: str
    type: str = "meteor"
    timestamp: str
    length_px: float
    aspect_ratio: float
    peak_dn: int
    bbox: List[int]
    thumbnail_url: Optional[str] = None


class MeteorEventsResponse(BaseModel):
    events: List[MeteorEventItem]
    total_count: int


class StoragePolicyRequest(BaseModel):
    """Retention policy update; omitted fields keep their current value."""
    frames_keep_days: Optional[int] = Field(None, ge=1, le=365)
    products_keep_days: Optional[int] = Field(None, ge=1, le=3650)
    max_data_gb: Optional[float] = Field(None, ge=0.5, le=10000)
    min_free_gb: Optional[float] = Field(None, ge=0.1, le=1000)
    archive_interval_s: Optional[float] = Field(None, ge=5, le=3600)
    timelapse_refresh_s: Optional[float] = Field(None, ge=60, le=86400)


class ObsOverlayRequest(BaseModel):
    """OBS and live stream celestial overlay, transform, title, and replay settings."""
    sky_overlay: Optional[bool] = Field(None, description="Show stars & constellations stick figures")
    sky_labels: Optional[bool] = Field(None, description="Show star & constellation labels")
    dso_overlay: Optional[bool] = Field(None, description="Show Messier deep-sky object markers & labels")
    grid_overlay: Optional[bool] = Field(None, description="Show alt/az grid rings & N/E/S/W compass")
    sun_moon: Optional[bool] = Field(None, description="Show Sun & Moon tracking markers")
    badge: Optional[bool] = Field(None, description="Show astrometry info badge (LST, FOV, PA)")
    adsb_overlay: Optional[bool] = Field(None, description="Show live ADS-B aircraft positions on sky")
    events_overlay: Optional[bool] = Field(None, description="Show recent meteor/aircraft streak lines on sky")
    fit: Optional[str] = Field(None, pattern="^(cover|contain)$", description="Framing fit mode")
    res: Optional[str] = Field(None, pattern="^(stream|full)$", description="Picture size: stream (same as /live) or full output resolution")
    bg: Optional[str] = Field(None, pattern="^(black|transparent)$", description="Background fill mode")
    night_vision: Optional[bool] = Field(None, description="Night vision / red lighting mode")
    opacity: Optional[float] = Field(None, ge=0.0, le=1.0, description="Overlay opacity level (0.0 to 1.0)")
    zoom: Optional[float] = Field(None, ge=0.5, le=10.0, description="Digital zoom factor (1.0 = 100%)")
    pan_x: Optional[float] = Field(None, ge=-100.0, le=100.0, description="Horizontal pan percentage (-100 to 100)")
    pan_y: Optional[float] = Field(None, ge=-100.0, le=100.0, description="Vertical pan percentage (-100 to 100)")
    rotate: Optional[float] = Field(None, ge=-180.0, le=180.0, description="Tilt / rotation angle in degrees")
    flip_h: Optional[bool] = Field(None, description="Mirror horizontally on OBS")
    flip_v: Optional[bool] = Field(None, description="Flip vertically on OBS")
    brightness: Optional[float] = Field(None, ge=0.2, le=3.0, description="Video brightness multiplier")
    contrast: Optional[float] = Field(None, ge=0.2, le=3.0, description="Video contrast multiplier")
    title_text: Optional[str] = Field(None, max_length=200, description="Main title overlay text")
    title_sub: Optional[str] = Field(None, max_length=200, description="Subtitle overlay text")
    title_pos: Optional[str] = Field(None, pattern="^(top-left|top-center|bottom-left|bottom-center)$", description="Title position")
    view_mode: Optional[str] = Field(None, pattern="^(live|event|timelapse|keogram|startrails)$", description="Active display mode")
    replay_url: Optional[str] = Field(None, max_length=500, description="Replay media URL")
    replay_crop_url: Optional[str] = Field(None, max_length=500, description="Replay close-up crop image URL")
    replay_label: Optional[str] = Field(None, max_length=300, description="Replay event description badge")
    replay_bbox: Optional[List[float]] = Field(None, max_length=4, description="Optional normalized line/bbox for highlight")



class CommunitySettingsRequest(BaseModel):
    """Community sharing settings; omitted fields keep their value."""
    enabled: Optional[bool] = None
    name: Optional[str] = Field(None, max_length=60, description="Station name shown on the community map (default: the station location name)")
    public_url: Optional[str] = Field(None, max_length=200, description="https URL where this station can be reached from the internet, or empty")
    public_precision_deg: Optional[float] = Field(None, description="Rounding grid for the station's public location (guests and the community card): 0 (exact), 0.01, 0.1 or 1.0 degree")
    precision_deg: Optional[float] = Field(None, description="Deprecated name of public_precision_deg")
    share_thumbnail: Optional[bool] = None
    share_preview: Optional[bool] = Field(None, description="Let map visitors enlarge the preview (up to 1280 px); needs share_thumbnail and a public URL")
    allow_open: Optional[bool] = Field(None, description="Let the community map link to this station's own page (public URL)")
    relay_previews: Optional[bool] = Field(None, description="Store and serve large previews pushed by stations without a public URL (default true; re-encoded JPEG, 1280 px, 100 stations / 25 MB, 24 h)")
    seeds: Optional[List[str]] = Field(None, max_length=10, description="Stations to ask first, one URL each (http allowed for a LAN)")

    @field_validator("name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v is not None else v

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, v: Optional[str]) -> Optional[str]:
        from src.community.card import CardError, check_public_url, to_origin
        v = (v or "").strip() if v is not None else v
        if v:
            try:
                v = to_origin(v) or v  # a typed path or trailing slash is stripped, not an error
                check_public_url(v)
            except CardError as exc:
                raise ValueError(str(exc))
        return v

    @field_validator("precision_deg")
    @classmethod
    def _precision(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v not in (0.01, 0.1, 1.0):
            raise ValueError("precision_deg must be 0.01, 0.1 or 1.0")
        return v

    @field_validator("seeds")
    @classmethod
    def _seeds(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        from urllib.parse import urlsplit
        out: List[str] = []
        for raw in v:
            url = raw.strip()
            if not url:
                continue
            try:
                parts = urlsplit(url)
                parts.port
            except ValueError:
                raise ValueError(f"invalid seed URL: {url[:60]}")
            if (len(url) > 200 or parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None or any(c.isspace() for c in url)):
                raise ValueError(f"invalid seed URL: {url[:60]}")
            if url not in out:
                out.append(url)
        return out


class TimezoneRequest(BaseModel):
    timezone: str = Field("", max_length=64, description='"" (the operating system zone), "coords" or an IANA name')


class UpdateSettingsRequest(BaseModel):
    auto_check: bool = Field(True, description="Check for a new release once a day")


class WeatherSettingsRequest(BaseModel):
    enabled: Optional[bool] = Field(None, description="Fetch weather and forecast for the station position (Open-Meteo)")
    refresh_min: Optional[int] = Field(None, description="Refresh interval in minutes: 15, 30, 60 or 180")
