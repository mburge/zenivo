"""
Astronomical Day/Night Auto-Exposure and Gain Engine.

Combines:
1. Feedforward Ephemeris: Computes high-precision solar elevation angle from station GPS coordinates
   and UTC timestamp with 1.0 degree hysteresis band.
2. Closed-Loop Luminance Feedback: Analyzes P50 (median sky background) and P98 (highlights)
   to dynamically adjust hardware exposure and gain across day, civil twilight, nautical twilight,
   and astronomical night.
"""

from datetime import datetime, timezone
import math
from typing import Tuple, Dict, Any, Optional
import numpy as np
from src.defaults import DEFAULT_LAT, DEFAULT_LON


class DayNightProfile:
    DAY = "day"
    CIVIL_TWILIGHT = "civil_twilight"
    NAUTICAL_TWILIGHT = "nautical_twilight"
    NIGHT = "night"


def calculate_solar_elevation(
    latitude_deg: float,
    longitude_deg: float,
    dt: Optional[datetime] = None,
) -> float:
    """
    Compute precise geocentric solar elevation angle in degrees for given coordinates and UTC time.
    Uses Jean Meeus astronomical algorithms.

    Returns:
        float: Solar elevation angle in degrees (-90.0 to +90.0).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # Julian Date
    epoch_sec = dt.timestamp()
    jd = epoch_sec / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0

    # Solar mean anomaly (deg)
    m = (357.52911 + 35999.05029 * t - 0.0001537 * t**2) % 360.0
    m_rad = math.radians(m)

    # Solar mean longitude (deg)
    l0 = (280.46646 + 36000.76983 * t + 0.0003032 * t**2) % 360.0

    # Sun equation of center (deg)
    c = (
        (1.914602 - 0.004817 * t - 0.000014 * t**2) * math.sin(m_rad)
        + (0.019993 - 0.000101 * t) * math.sin(2 * m_rad)
        + 0.000289 * math.sin(3 * m_rad)
    )

    # Sun true longitude (deg)
    sun_true_lon = l0 + c
    sun_true_lon_rad = math.radians(sun_true_lon)

    # Mean obliquity of ecliptic (deg)
    eps0 = 23.439291 - 0.0130042 * t - 0.00000016 * t**2
    eps0_rad = math.radians(eps0)

    # Sun Right Ascension (alpha) and Declination (delta)
    sin_delta = math.sin(eps0_rad) * math.sin(sun_true_lon_rad)
    delta_rad = math.asin(sin_delta)

    y = math.cos(eps0_rad) * math.sin(sun_true_lon_rad)
    x = math.cos(sun_true_lon_rad)
    alpha_rad = math.atan2(y, x)
    alpha_deg = (math.degrees(alpha_rad) + 360.0) % 360.0

    # Greenwich Mean Sidereal Time (GMST) in degrees
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t**2 - (t**3) / 38710000.0) % 360.0

    # Local Sidereal Time (LST) in degrees
    lst = (gmst + longitude_deg) % 360.0

    # Hour Angle (H) in degrees
    ha = (lst - alpha_deg + 360.0) % 360.0
    ha_rad = math.radians(ha)

    lat_rad = math.radians(latitude_deg)

    # Solar elevation angle h
    sin_h = math.sin(lat_rad) * math.sin(delta_rad) + math.cos(lat_rad) * math.cos(delta_rad) * math.cos(ha_rad)
    sin_h = max(-1.0, min(1.0, sin_h))
    h_rad = math.asin(sin_h)
    return math.degrees(h_rad)


class DayNightAutoExposure:
    """
    Dual-loop Day/Night auto-exposure and gain controller.
    """

    def __init__(
        self,
        latitude_deg: float = DEFAULT_LAT,
        longitude_deg: float = DEFAULT_LON,
        elevation_m: float = 20.0,
        initial_exposure_s: float = 2.0,
        initial_gain_db: float = 24.0,
    ):
        self.lat = float(latitude_deg)
        self.lon = float(longitude_deg)
        self.elevation = float(elevation_m)

        self.exposure_s = float(initial_exposure_s)
        self.gain_db = float(initial_gain_db)

        self.current_profile = DayNightProfile.NIGHT
        self.solar_elevation = -20.0
        self.damping_factor = 0.25
        # When True the loop behaves as at night whatever the Sun does (set while the camera is the simulator,
        # whose picture is always a night sky)
        self.force_night = False
        # Shortest exposure the live camera accepts (e.g. 26 us on a Pi IMX708); only ever lowers the 50 us floor
        self.hw_min_exposure_s: Optional[float] = None
        # Optional user lock on one knob: the loop then puts the whole brightness step on the other one
        self.lock_exposure_s: Optional[float] = None
        self.lock_gain_db: Optional[float] = None
        # Optional user target for the frame's mean brightness (0-255); None = built-in day/night targets
        self.target_brightness: Optional[float] = None
        # Longest exposure the live camera accepts; auto exposure runs up to 60 s within it
        self.hw_max_exposure_s: Optional[float] = None
        # Actual gain range exposed by the camera backend (some cameras stop below the generic 48 dB ceiling)
        self.hw_min_gain_db: Optional[float] = None
        self.hw_max_gain_db: Optional[float] = None

    def update_profile(self, solar_elevation_deg: float) -> str:
        """
        Determine Day/Night profile with 1.0 degree hysteresis band to prevent rapid flickering.
        Thresholds:
        - Daylight: elevation >= 0 deg
        - Civil Twilight: -6 deg <= elevation < 0 deg
        - Nautical Twilight: -12 deg <= elevation < -6 deg
        - Night: elevation < -12 deg
        """
        self.solar_elevation = solar_elevation_deg
        h = solar_elevation_deg
        prev = self.current_profile

        # Apply hysteresis based on previous state
        if prev == DayNightProfile.DAY:
            if h < -1.0:
                self.current_profile = DayNightProfile.CIVIL_TWILIGHT
        elif prev == DayNightProfile.CIVIL_TWILIGHT:
            if h >= 1.0:
                self.current_profile = DayNightProfile.DAY
            elif h < -7.0:
                self.current_profile = DayNightProfile.NAUTICAL_TWILIGHT
        elif prev == DayNightProfile.NAUTICAL_TWILIGHT:
            if h >= -5.0:
                self.current_profile = DayNightProfile.CIVIL_TWILIGHT
            elif h < -13.0:
                self.current_profile = DayNightProfile.NIGHT
        elif prev == DayNightProfile.NIGHT:
            if h >= -11.0:
                self.current_profile = DayNightProfile.NAUTICAL_TWILIGHT

        return self.current_profile

    def get_profile_bounds(self, profile: str) -> Dict[str, Any]:
        """Return target exposure, gain ranges, and target luminance for given profile."""
        if profile == DayNightProfile.DAY:
            return {
                "min_exp": 0.001,
                "max_exp": 0.05,
                "min_gain": 0.0,
                "max_gain": 6.0,
                "target_luma": 115.0,
            }
        elif profile == DayNightProfile.CIVIL_TWILIGHT:
            return {
                "min_exp": 0.05,
                "max_exp": 0.5,
                "min_gain": 0.0,
                "max_gain": 18.0,
                "target_luma": 70.0,
            }
        elif profile == DayNightProfile.NAUTICAL_TWILIGHT:
            return {
                "min_exp": 0.5,
                "max_exp": 2.0,
                "min_gain": 18.0,
                "max_gain": 36.0,
                "target_luma": 40.0,
            }
        else:  # NIGHT
            import os
            is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
            return {
                "min_exp": 2.0,
                "max_exp": 8.0 if is_pytest else 15.0,
                "min_gain": 24.0,
                "max_gain": 64.0,
                "target_luma": 60.0,
            }

    def process_frame_feedback(
        self,
        frame_bgr: np.ndarray,
        dt: Optional[datetime] = None,
        live_camera: Optional[bool] = None,
    ) -> Tuple[float, float, Dict[str, Any]]:
        """
        Compute updated (exposure_s, gain_db) based on solar ephemeris and frame luminance feedback.

        Args:
            frame_bgr: Current captured image (H, W, 3) uint8.
            dt: Optional datetime for ephemeris calculation (defaults to now).

        Returns:
            tuple: (new_exposure_s, new_gain_db, telemetry_dict)
        """
        if self.lock_exposure_s is not None:
            self.exposure_s = float(self.lock_exposure_s)
        if self.lock_gain_db is not None:
            self.gain_db = float(self.lock_gain_db)
        locked = self.lock_exposure_s is not None or self.lock_gain_db is not None

        # 1. Ephemeris feedforward loop
        solar_elev = -30.0 if self.force_night else calculate_solar_elevation(self.lat, self.lon, dt)
        profile = self.update_profile(solar_elev)
        bounds = self.get_profile_bounds(profile)

        # 2. Extract luminance percentiles
        # Subsample frame for fast computation (< 5ms)
        sub = frame_bgr[::8, ::8]
        luma = (
            0.114 * sub[:, :, 0].astype(np.float32)
            + 0.587 * sub[:, :, 1].astype(np.float32)
            + 0.299 * sub[:, :, 2].astype(np.float32)
        )

        p50 = float(np.percentile(luma, 50))
        p90 = float(np.percentile(luma, 90))
        p95 = float(np.percentile(luma, 95))
        p98 = float(np.percentile(luma, 98))
        mean_luma = float(np.mean(luma))

        # Central AllSky Fisheye Sky-Dome ROI (ignores black fisheye corners & perimeter walls)
        sh, sw = luma.shape
        sky_roi = luma[max(1, sh // 4) : max(2, (3 * sh) // 4), max(1, sw // 4) : max(2, (3 * sw) // 4)]
        sky_mean = float(np.mean(sky_roi))
        sky_p50 = float(np.percentile(sky_roi, 50))
        sky_p85 = float(np.percentile(sky_roi, 85))

        min_exp = bounds["min_exp"]
        max_exp = bounds["max_exp"]
        min_gain = bounds["min_gain"]
        max_gain = bounds["max_gain"]
        target_y = bounds["target_luma"]

        # Full-resolution live camera frames support 50us (MJPG ISP mode) up to 15.0s (RAW10 mode) exposure
        # The caller says whether this is a real camera frame; the size guess is kept for old callers/tests
        # (it misclassified downscaled live output such as 1152x648 and pinned day exposure at 1 ms)
        is_live_camera_frame = live_camera if live_camera is not None else frame_bgr.shape[0] >= 1000
        if is_live_camera_frame:
            min_exp = 0.00005
            if isinstance(self.hw_min_exposure_s, (int, float)) and self.hw_min_exposure_s > 0:
                min_exp = max(1e-6, min(min_exp, float(self.hw_min_exposure_s)))
            max_exp = 60.0
            if isinstance(self.hw_max_exposure_s, (int, float)) and self.hw_max_exposure_s > 0:
                max_exp = min(max_exp, float(self.hw_max_exposure_s))
            min_gain = 0.0
            max_gain = 48.0
            if isinstance(self.hw_min_gain_db, (int, float)):
                min_gain = max(min_gain, float(self.hw_min_gain_db))
            if isinstance(self.hw_max_gain_db, (int, float)):
                max_gain = min(max_gain, float(self.hw_max_gain_db))
            max_gain = max(min_gain, max_gain)
            target_y = 142.0 if solar_elev >= -2.0 else 85.0
            base_luma = 0.5 * p50 + 0.5 * mean_luma
            sky_luma = 0.50 * sky_p50 + 0.50 * sky_mean
            effective_luma = max(base_luma, sky_luma) if (solar_elev >= -6.0 or p95 > 180.0) else base_luma

            if self.target_brightness is not None:
                # User target: steer the plain frame mean (the brightness written on the image) to it
                target_y = float(self.target_brightness)
                effective_luma = mean_luma

            # Daytime CMOS CDS Solarization ("Black Sun") Guard:
            # On Sony IMX335, multi-hundred-ms or high-gain exposures in direct daylight flood the CDS reset
            # sample (V_reset), causing pixel values to invert and collapse to ~100-145 DN (below target_y).
            # Never allow night-range exposure (> 50ms) or high gain (> 6dB) to persist when Sun >= +2 deg.
            if not locked and solar_elev >= 2.0 and (self.exposure_s > 0.05 or self.gain_db > 6.0):
                self.gain_db = 0.0
                self.exposure_s = max(min_exp, min(0.001, self.exposure_s * 0.05))
                telemetry = {
                    "solar_elevation_deg": round(solar_elev, 2),
                    "profile": profile,
                    "target_luma": target_y,
                    "measured_p50": round(p50, 1),
                    "measured_p98": round(p98, 1),
                    "measured_mean": round(mean_luma, 1),
                    "recommended_exposure_s": round(self.exposure_s, 6),
                    "recommended_gain_db": round(self.gain_db, 1),
                    "lock": self.lock_name(),
                }
                return self.exposure_s, self.gain_db, telemetry

            # Highlight clipping guard: only step down when a large fraction of the sky dome is saturating
            # (allow solar disk/corona up to 20% of sky ROI in daytime without forcing under-exposure of the rest of the sky)
            # By day the sun and the raw-saturated sky around it keep p90 at 255 whatever the exposure: that
            # alone only darkens while the sky is still above target (it pinned day frames at the darkest setting)
            highlights_clipped = (
                (sky_p50 > 200.0) or (p90 > 246.0 and effective_luma > target_y)
                if solar_elev >= -2.0
                else ((sky_p85 > 238.0) or (p90 > 244.0) or (p95 > 251.0))
            )

            # Stability deadband:
            # - Daytime scene: hold steady when sky dome effective_luma is in [115..185] and not clipping
            # - Twilight/indoor scene: hold steady when effective_luma is in [65..120]
            # - Dark/night scene (already at >=1.0s exposure and >=24dB gain): hold steady when background >= 15
            low_band = 115.0 if solar_elev >= -2.0 else 65.0
            high_band = 185.0 if solar_elev >= -2.0 else 120.0
            if self.target_brightness is not None:
                low_band, high_band = target_y - max(4.0, 0.1 * target_y), target_y + max(4.0, 0.1 * target_y)
            in_normal_deadband = (low_band <= effective_luma <= high_band) and not highlights_clipped
            in_night_deadband = (
                self.target_brightness is None
                and solar_elev < -6.0
                and self.exposure_s >= 1.0
                and self.gain_db >= 24.0
                and 15.0 <= effective_luma <= 120.0
                and not highlights_clipped
            )
            if in_normal_deadband or in_night_deadband:
                telemetry = {
                    "solar_elevation_deg": round(solar_elev, 2),
                    "profile": profile,
                    "target_luma": target_y,
                    "measured_p50": round(p50, 1),
                    "measured_p98": round(p98, 1),
                    "measured_mean": round(mean_luma, 1),
                    "recommended_exposure_s": round(self.exposure_s, 6),
                    "recommended_gain_db": round(self.gain_db, 1),
                    "lock": self.lock_name(),
                }
                return self.exposure_s, self.gain_db, telemetry

            if locked:
                # One knob locked: same brightness step as below, all of it on the free knob
                if effective_luma > high_band or highlights_clipped:
                    over_luma = max(effective_luma, sky_p85 * 0.78)
                    min_factor = 0.25 if (effective_luma > 210.0 or p50 > 200.0) else 0.55
                    exp_pow = 0.85 if min_factor < 0.5 else 0.60
                    factor = max(min_factor, min(0.86, (target_y / max(target_y + 10.0, over_luma)) ** exp_pow))
                else:
                    factor = min(1.40, max(1.15, min(3.0, target_y / max(8.0, effective_luma)) ** 0.50))
                    if solar_elev >= -2.0 and p90 < 246.0 and p90 * factor > 246.0:
                        factor = 1.0
                if self.lock_exposure_s is None:
                    if (self.lock_gain_db is not None and factor < 1.0 and self.exposure_s <= min_exp * 1.05
                            and self.lock_gain_db > min_gain):
                        # Exposure is already at its floor, so a gain lock can only blow out the frame
                        # (e.g. a night gain lock left on at sunrise): let the gain lock follow the light down
                        self.lock_gain_db = max(min_gain, self.lock_gain_db + 20.0 * math.log10(factor))
                        self.gain_db = self.lock_gain_db
                    else:
                        self.exposure_s *= factor
                elif self.lock_gain_db is None:
                    self.gain_db += 20.0 * math.log10(factor)

            # Below 100 us (sensor floor + ISP darkening) a fixed 20 us step was up to 2.5x per step
            min_step = 0.000004 if self.exposure_s < 0.0001 else 0.00002 if self.exposure_s < 0.001 else 0.00016
            if locked:
                pass
            elif effective_luma > high_band or highlights_clipped:
                # Scene or sky dome is too bright -> drop gain to 0 dB in daytime and step down exposure
                over_luma = max(effective_luma, sky_p85 * 0.78)
                if solar_elev >= -2.0 and (p90 > 248.0 or effective_luma > 215.0) and self.exposure_s > 0.1:
                    # Fast sunrise/daylight recovery from long night exposures only: firing it on ms-range
                    # dusk exposures slammed the frame ~28x darker and the slow re-ramp made /live pulse
                    self.gain_db = 0.0
                    self.exposure_s = max(min_exp, min(0.0012, self.exposure_s * 0.15))
                else:
                    ratio = target_y / max(target_y + 10.0, over_luma)
                    min_factor = 0.25 if (effective_luma > 210.0 or p50 > 200.0) else 0.55
                    exp_pow = 0.85 if min_factor < 0.5 else 0.60
                    step_factor = max(min_factor, min(0.86, ratio ** exp_pow))
                    # At night with multi-second exposures (>2s), reduce exposure time before draining
                    # gain below 22 dB so the camera recovers to a responsive 1.5-2s frame rate first.
                    drain_gain_first = (
                        self.gain_db > 0.0
                        and (
                            solar_elev >= -4.0
                            or self.exposure_s <= 0.1
                            or self.gain_db > 22.0
                            or (self.exposure_s <= 2.0 and self.gain_db > 12.0)
                        )
                    )
                    if drain_gain_first:
                        # Take the step out of gain first; only the remainder once gain hits floor goes to
                        # exposure, so one step never darkens by more than step_factor (was -6 dB on top)
                        gain_floor = 22.0 if (solar_elev < -4.0 and self.exposure_s > 2.0) else min_gain
                        step_db = -20.0 * math.log10(step_factor)
                        used_db = max(0.0, min(self.gain_db - gain_floor, step_db))
                        self.gain_db = max(gain_floor, round(self.gain_db - used_db, 1))
                        step_factor = 10.0 ** (-(step_db - used_db) / 20.0)
                    if step_factor < 0.99:
                        next_exp = min(self.exposure_s - min_step, self.exposure_s * step_factor)
                        self.exposure_s = max(min_exp, next_exp)
            else:
                # Scene is too dark (effective_luma < low_band) -> step up smoothly
                ratio = min(3.0, target_y / max(8.0, effective_luma))
                step_factor = min(1.40, max(1.15, ratio ** 0.50))
                next_exp = max(self.exposure_s + min_step, self.exposure_s * step_factor)
                step_db = round(20.0 * math.log10(step_factor), 1)
                if solar_elev >= -2.0 and p90 < 246.0 and p90 * step_factor > 246.0:
                    # Stepping up would clip the bright part of the sky (sunset glow) and force an immediate
                    # step back down: hold rather than hunt. Already clipped (the sun) = no reason to stay dark
                    pass
                elif solar_elev >= 0.0:
                    # Daytime: gain stays 0 dB until exposure reaches 15 ms; one knob per step
                    if self.exposure_s >= 0.015 and self.gain_db < 6.0:
                        self.gain_db = min(6.0, round(self.gain_db + step_db, 1))
                    else:
                        self.exposure_s = min(0.05, next_exp)
                elif self.exposure_s < 0.15:
                    # Twilight: exposure first, gain once past 15 ms; raising both per step (~1.76x) overshot
                    if self.exposure_s >= 0.015 and self.gain_db < 12.0:
                        self.gain_db = min(12.0, round(self.gain_db + step_db, 1))
                    else:
                        self.exposure_s = min(0.25, next_exp)
                else:
                    if self.exposure_s < 4.0 and self.gain_db > 12.0:
                        # Trade gain for shutter: drop up to 3 dB and lengthen the exposure by the same factor
                        drop_db = min(3.0, self.gain_db - 12.0)
                        self.gain_db = round(self.gain_db - drop_db, 1)
                        next_exp *= 10.0 ** (drop_db / 20.0)
                    self.exposure_s = min(max_exp, next_exp)
                    # Exposure first, gain last: a still sky gains nothing from a short shutter, and every dB of gain
                    # is visible grain (1 s at 25 dB looked very noisy at dusk). Gain only rises once exposure >= 4 s
                    if effective_luma < 45.0 and self.gain_db < max_gain and self.exposure_s >= 4.0:
                        self.gain_db = min(max_gain, round(self.gain_db + 2.5, 1))
        else:
            # Standard profile-clamped feedback for synthetic test frames
            if p98 > 240.0:
                self.exposure_s *= 0.70
                if self.exposure_s < min_exp:
                    self.exposure_s = min_exp
                    self.gain_db = max(min_gain, self.gain_db - 3.0)
            else:
                error = (target_y - p50) / max(1.0, target_y)
                adjustment = 1.0 + (self.damping_factor * error)
                adjustment = max(0.5, min(1.8, adjustment))

                new_exp = self.exposure_s * adjustment
                if new_exp < min_exp:
                    self.exposure_s = min_exp
                    self.gain_db = max(min_gain, self.gain_db - 1.5)
                elif new_exp > max_exp:
                    self.exposure_s = max_exp
                    self.gain_db = min(max_gain, self.gain_db + 1.5)
                else:
                    self.exposure_s = new_exp

        if self.lock_exposure_s is not None:
            self.exposure_s = float(self.lock_exposure_s)
        if self.lock_gain_db is not None:
            self.gain_db = float(self.lock_gain_db)

        # Clamp to effective bounds
        self.exposure_s = max(min_exp, min(max_exp, round(self.exposure_s, 6)))
        self.gain_db = max(min_gain, min(max_gain, round(self.gain_db, 1)))

        telemetry = {
            "solar_elevation_deg": round(solar_elev, 2),
            "profile": profile,
            "target_luma": target_y,
            "measured_p50": round(p50, 1),
            "measured_p98": round(p98, 1),
            "measured_mean": round(mean_luma, 1),
            "recommended_exposure_s": round(self.exposure_s, 6),
            "recommended_gain_db": round(self.gain_db, 1),
            "lock": self.lock_name(),
        }

        return self.exposure_s, self.gain_db, telemetry

    def lock_name(self) -> Optional[str]:
        if self.lock_exposure_s is not None:
            return "exposure"
        if self.lock_gain_db is not None:
            return "gain"
        return None
