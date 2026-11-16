"""Frame capture, hot-pixel repair, snapshot encoding and the background capture worker.

Mixin of AllSkyService (src/api/service.py): methods only, all state lives on the service instance."""

import json
import logging
import os
import time
from typing import Dict, Any, Optional, Tuple
import numpy as np
import cv2

from src.hardware.driver import camera_limits

from src.pipeline.overlay import frame_brightness

logger = logging.getLogger("imx335.api")


class CaptureMixin:

    def _log_exposure_warning(self) -> None:
        """One activity-log line when the lock warning appears or changes kind (not on every frame)."""
        w = self.exposure_warning()
        kind = w["kind"] if w else None
        if kind != getattr(self, "_last_exposure_warning", None):
            self._last_exposure_warning = kind
            if w:
                self.activity.warning("camera", w["message"])

    AE_STATE_MAX_AGE_S = 2 * 3600.0   # a restart within this time resumes the last auto exposure / gain
    AE_STATE_SAVE_EVERY_S = 20.0

    def _ae_state_path(self) -> str:
        return os.path.join(self.output_dir, "ae_state.json")

    def _save_ae_state(self, exposure_s: float, gain_db: float, is_pytest: bool = False) -> None:
        """Remember the auto exposure / gain (at most every 20 s) so a restart does not begin from the camera default."""
        now = time.time()
        if is_pytest or now - getattr(self, "_ae_state_saved", 0.0) < self.AE_STATE_SAVE_EVERY_S:
            return
        self._ae_state_saved = now
        try:
            tmp = self._ae_state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"t": round(now, 1), "exposure_s": float(exposure_s), "gain_db": float(gain_db)}, fh)
            os.replace(tmp, self._ae_state_path())
        except OSError:
            pass

    def _restore_ae_state(self) -> bool:
        """After a short stop, start from the last auto exposure / gain: no sudden brightness jump on restart."""
        if os.environ.get("PYTEST_CURRENT_TEST") or self.mode != "auto":
            return False
        try:
            with open(self._ae_state_path(), encoding="utf-8") as fh:
                st = json.load(fh)
            age = time.time() - float(st["t"])
            exp, gain = float(st["exposure_s"]), float(st["gain_db"])
            lim = camera_limits(self.camera)
            if not (0.0 <= age <= self.AE_STATE_MAX_AGE_S) or not (
                    float(lim.get("min_exposure_s") or 0.0) <= exp <= float(lim.get("max_exposure_s") or 1e9)
                    and float(lim.get("min_gain_db") or 0.0) <= gain <= float(lim.get("max_gain_db") or 1e9)):
                return False
            self.camera.set_hardware_exposure(exp, gain)
            self.auto_exposure.exposure_s, self.auto_exposure.gain_db = exp, gain
            self.activity.info("capture", f"Resumed the last auto exposure {exp:.4g}s, gain {gain:.1f} dB ({int(age // 60)} min old)")
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def capture_frame(self, force_refresh: bool = False) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        Capture a frame from camera, caching across concurrent API/MJPEG/WebSocket reads
        so the V4L2 bus is not reopened multiple times per exposure cycle.
        """
        # Do not block live stream/telemetry readers while dark frame stack is actively capturing
        if not force_refresh and self.calibration_status.get("state") == "running" and self.last_frame is not None:
            return self.last_frame, self.last_metadata

        now = time.time()
        is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        if is_pytest or self.focus.active:
            cache_ttl = 0.05
        else:
            cache_ttl = max(self.performance["min_capture_interval_s"], getattr(self.camera, "current_exposure_s", 0.5) * 0.9)
        if not force_refresh and self.last_frame is not None and (now - self.last_capture_time) < cache_ttl:
            return self.last_frame, self.last_metadata

        with self.lock:
            now = time.time()
            if not force_refresh and self.last_frame is not None and (now - self.last_capture_time) < cache_ttl:
                return self.last_frame, self.last_metadata

            # Night (sun below -6°): capture RAW10 so the ISP's purple shadow cast never appears
            if not is_pytest and hasattr(self.camera, "set_night_mode") and now - self._night_check_ts >= 60.0:
                self._night_check_ts = now
                try:
                    elev = self.effective_solar_elevation()
                    self.camera.set_night_mode(elev < -6.0)
                except Exception:
                    pass

            self._sync_decode_reduce()
            try:
                frame, meta = self.camera.capture_frame()
                if self._camera_failing:
                    self._camera_failing = False
                    self.activity.success("capture", "Camera capture recovered")
            except Exception as first_exc:
                # Re-open hardware fd once before falling back to simulator
                ctx = (f"exposure {getattr(self.camera, 'current_exposure_s', '?')} s, "
                       f"gain {getattr(self.camera, 'current_gain_db', '?')} dB, "
                       f"{time.time() - self.last_capture_time:.0f} s since last good frame, "
                       f"calibration={self.calibration_status.get('state')}, focus={self.focus.active}")
                self.activity.warning("capture", f"Capture failed ({type(first_exc).__name__}: {first_exc}; {ctx}); re-opening camera")
                try:
                    self.camera.close()
                    self.camera.open()
                    frame, meta = self.camera.capture_frame()
                    self.activity.success("capture", "Camera re-opened, capture OK")
                except Exception as exc:
                    self._camera_failing = True
                    self.activity.error("capture", f"Camera re-open failed ({type(exc).__name__}: {exc}; {ctx})")
                    if (
                        self.last_frame is not None
                        and not getattr(self.camera, "simulator", False)
                        and not self.last_metadata.get("simulator", False)
                    ):
                        return self.last_frame, self.last_metadata
                    from src.hardware.simulator import SimulatedIMX335Controller
                    try:
                        self.camera.close()
                    except Exception:
                        pass
                    self.camera = SimulatedIMX335Controller(
                        width=getattr(self.camera, "width", 2592),
                        height=getattr(self.camera, "height", 1944),
                    )
                    self.activity.error("camera", "Switched to SIMULATOR because the real camera is not responding; restart the service after fixing the camera")
                    self.camera.open()
                    frame, meta = self.camera.capture_frame()

            bayer10 = meta.pop("bayer10", None)

            # Automatically apply closest Master Dark from library on long exposures (>= 0.4s)
            if not is_pytest and self.calibration_status.get("state") != "running":
                cur_exp = float(meta.get("actual_exposure_s", getattr(self.camera, "current_exposure_s", 0.2)))
                cur_gain = float(meta.get("gain_db", getattr(self.camera, "current_gain_db", 12.0)))
                if cur_exp >= 0.4:
                    frame, matched_dark = self._apply_closest_master_dark(frame, cur_exp, cur_gain, bayer10=bayer10)
                    if matched_dark:
                        meta["applied_master_dark"] = matched_dark
                    if not self.focus.active:
                        frame = self._repair_hot_pixels(frame, bayer10, now)

            # Mirror / flip / rotate after dark subtraction (darks stay in native sensor orientation)
            isp_frame = self._is_isp_frame(bayer10)
            meta["capture_path"] = "isp" if isp_frame else "raw10" if bayer10 is not None else "native"
            frame = self._apply_image_transform(frame, isp_frame)
            if not self.focus.active:
                frame = self._apply_output_resolution(frame)
            meta["image_transform"] = dict(self.station_config.get("image_transform", {}))
            meta["output_resolution"] = {"width": int(frame.shape[1]), "height": int(frame.shape[0])}

            self.last_frame = frame
            self.last_metadata = meta
            self.last_capture_time = time.time()
            self._note_real_capture(self.last_capture_time)

            # Automatic Sunrise Overexposure & CMOS Solarization Guard:
            # If left in manual night exposure after daybreak, switch to auto (note: at >=0.25s in full daylight
            # solar_elev >= 2.0, Sony IMX335 CDS reset overflow solarizes/inverts the frame so p90 drops to ~130-160)
            # An exposure lock in auto mode (the "other knob follows the sky" lock) has the same problem: by day the
            # free gain is already at 0 dB, so nothing can darken the frame and /live stays white.
            exposure_locked_auto = self.mode == "auto" and self.auto_exposure.lock_exposure_s is not None
            if not is_pytest and (self.mode == "manual" or exposure_locked_auto) and self.calibration_status.get("state") != "running":
                try:
                    cur_exp_val = float(getattr(self.camera, "current_exposure_s", 0.1))
                    if cur_exp_val >= 0.08 and frame is not None and frame.shape[0] >= 200:
                        solar_elev = self.effective_solar_elevation()
                        p90_sample = float(np.percentile(frame[::16, ::16], 90))
                        if (solar_elev >= -2.0 and p90_sample >= 245.0) or (solar_elev >= 2.0 and cur_exp_val >= 0.25):
                            if exposure_locked_auto:
                                self.activity.info(
                                    "camera",
                                    f"Daylight: released the {self.auto_exposure.lock_exposure_s:.4g} s exposure lock "
                                    f"(sun {solar_elev:.1f}°, frame p90 {p90_sample:.0f}) so auto exposure can darken the frame")
                            self.mode = "auto"
                            self.auto_exposure.lock_exposure_s = None
                            self.auto_exposure.lock_gain_db = None
                except Exception:
                    pass

            # If auto-exposure is active (and not calibrating), update exposure and gain dynamically
            if self.mode == "auto" and self.calibration_status.get("state") != "running" and not self.focus.active:
                try:
                    self.auto_exposure.exposure_s = float(getattr(self.camera, "current_exposure_s", self.auto_exposure.exposure_s))
                    self.auto_exposure.gain_db = float(getattr(self.camera, "current_gain_db", self.auto_exposure.gain_db))
                    self.auto_exposure.hw_min_exposure_s = getattr(self.camera, "min_exposure_s", None)
                    limits = camera_limits(self.camera)
                    self.auto_exposure.hw_max_exposure_s = limits.get("max_exposure_s")
                    self.auto_exposure.hw_min_gain_db = limits.get("min_gain_db")
                    self.auto_exposure.hw_max_gain_db = limits.get("max_gain_db")
                    new_exp, new_gain, ae_telem = self.auto_exposure.process_frame_feedback(
                        frame, live_camera=not is_pytest
                    )
                    self.camera.set_hardware_exposure(new_exp, new_gain)
                    self._save_ae_state(new_exp, new_gain, is_pytest)
                    self.last_metadata["auto_exposure"] = ae_telem
                    profile = ae_telem.get("profile")
                    if profile and profile != self._last_profile and not is_pytest:
                        if self._last_profile is not None:
                            self.activity.info(
                                "capture",
                                f"Sky profile {self._last_profile} -> {profile} (sun {ae_telem.get('solar_elevation_deg')}°), "
                                f"exposure {new_exp:.4g}s, gain {new_gain:.1f} dB",
                            )
                        self._last_profile = profile
                except Exception:
                    pass

            display = self._star_boost_frame(frame, meta.get("capture_path") == "raw10") if not self.focus.active else frame
            # Time / exposure / gain / brightness reach viewers via telemetry (HTML overlay), not the pixels
            self.last_brightness = round(frame_brightness(frame), 1)
            if not is_pytest:
                self._log_exposure_warning()
            self.display_frame, self._display_frame_ts = display, self.last_capture_time

            # Update cached snapshots after RAW10 subtraction + demosaic. A full-res WebP encode costs
            # ~250 ms of single-core CPU, so it is encoded lazily in get_snapshot_webp() and the
            # data/live/latest.webp copy (not read by the app itself) is refreshed at most every 120 s.
            self.last_webp = None
            if not is_pytest and now - self._last_live_webp_write_ts >= 120.0:
                self._last_live_webp_write_ts = now
                try:
                    with open(os.path.join(self.live_dir, "latest.webp"), "wb") as fp:
                        fp.write(self._encode_webp())
                except Exception:
                    pass

            if self.focus.active:
                # A native 5 MP encode per frame would halve the focus frame rate; snapshots encode lazily
                self.last_jpeg = None
            else:
                ret, jpeg = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ret:
                    self.last_jpeg = jpeg.tobytes()

            if not is_pytest and self.calibration_status.get("state") != "running" and not self.focus.active:
                try:
                    self._maybe_archive_frame_and_update_products(frame, meta)
                except Exception:
                    pass
                try:
                    self._track_focus(frame, meta, now)
                except Exception:
                    pass
                try:
                    # The star-boosted viewer frame: aircraft dots are several times brighter there than in
                    # the raw frame (short exposures), and it is what people see
                    ev_types = self._detect_sky_events(display, meta, now)
                    if ev_types:
                        # The event frame would otherwise fall between two archive ticks: keep it
                        self._maybe_archive_frame_and_update_products(frame, meta, force=True, events=ev_types)
                except Exception:
                    pass

            return frame, meta

    def _repair_hot_pixels(self, frame: np.ndarray, bayer10: Optional[np.ndarray], now: float) -> np.ndarray:
        """Learn hot pixels from a sampled long exposure (every 90 s) and paint them out of every one."""
        tracker = self.hot_pixels
        try:
            if tracker.due(now):
                before = len(tracker.confirmed)
                # The user's sky polygons are drawn on the displayed (mirrored/flipped) image; flips undo themselves
                isp = self._is_isp_frame(bayer10)
                from src.calibration.bright_star_solver import combined_sky_mask
                # User polygons are stable; the automatic mask shrinks under passing clouds
                user = self.station_config.get("sky_mask")
                sky = combined_sky_mask(self._apply_image_transform(frame, isp), user,
                                        use_auto=not (user or {}).get("sky"))
                if sky is not None:
                    sky = self._apply_image_transform(sky.astype(np.uint8), isp).astype(bool)
                n = tracker.sample(frame, now, sky)
                if n != before and (before == 0 or abs(n - before) >= 25):
                    self.activity.info("capture", f"Hot pixels: {n} found on the open sky by staying still while "
                                                  "the stars drift; they are repaired in every long exposure")
            return tracker.repair(frame)
        except Exception as exc:
            self.activity.warning("capture", f"Hot-pixel repair skipped: {exc}")
            return frame

    def _note_real_capture(self, t: float) -> None:
        """Remember how often a new frame really arrives (what the viewers get): exponential average of the interval."""
        prev = getattr(self, "_last_real_capture", None)
        self._last_real_capture = t
        if prev is not None and 0.0 < t - prev < 600.0:
            ema = getattr(self, "_frame_interval_s", None)
            self._frame_interval_s = (t - prev) if ema is None else 0.8 * ema + 0.2 * (t - prev)

    def get_snapshot_webp(self) -> bytes:
        """Return calibrated Light frame in WebP format (captured as RAW10, subtracted in RAW10, then encoded to WebP)."""
        self.capture_frame()
        return self._encode_webp()

    def _encode_webp(self) -> bytes:
        """Encode the display frame to WebP at most once per captured frame (lock-free, like get_status)."""
        frame, frame_ts = self.get_display_frame()
        cached, cached_ts = self.last_webp, self._webp_frame_ts
        if cached is not None and cached_ts == frame_ts:
            return cached
        ret, webp = cv2.imencode(".webp", frame, [cv2.IMWRITE_WEBP_QUALITY, 92])
        if not ret:
            raise RuntimeError("WebP compression failed")
        data = webp.tobytes()
        self.last_webp, self._webp_frame_ts = data, frame_ts
        return data

    def get_snapshot_jpeg(self) -> bytes:
        """Return valid JPEG binary payload, refreshing via capture_frame() when TTL expires."""
        self.capture_frame()
        if self.last_jpeg is not None:
            return self.last_jpeg
        frame, _ = self.get_display_frame()
        ret, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ret:
            raise RuntimeError("JPEG compression failed")
        self.last_jpeg = jpeg.tobytes()
        return self.last_jpeg

    # ------------------------------------------------------------------
    # Disk retention
    # ------------------------------------------------------------------
    def _background_capture_worker(self) -> None:
        """Keep capturing with no viewer connected, so auto exposure, archiving and meteor/aircraft
        detection never stop. Viewers share the same cached frames, so this adds no extra exposures."""
        while not self._retention_stop.is_set():
            interval = float(self.performance.get("background_capture_interval_s", 0.0))
            started = time.time()
            if not self.focus.active and self.calibration_status.get("state") != "running":
                try:
                    self.capture_frame()
                except Exception:
                    pass
            if interval > 0:
                wait = interval - (time.time() - started)
            else:
                # Back to back: wake when the frame cache expires (one exposure, or min_capture_interval_s)
                ttl = max(self.performance["min_capture_interval_s"],
                          float(getattr(self.camera, "current_exposure_s", 0.5)) * 0.9)
                wait = ttl - (time.time() - self.last_capture_time)
            self._retention_stop.wait(max(0.2, wait))
