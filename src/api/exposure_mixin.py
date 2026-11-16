"""Exposure, gain, auto-exposure mode/lock and persisted camera settings.

Mixin of AllSkyService (src/api/service.py): methods only, all state lives on the service instance."""

import logging
import math
import os
import time
from typing import Dict, Any, Optional

from src.hardware.driver import camera_limits


logger = logging.getLogger("imx335.api")


class ExposureMixin:

    def set_exposure(self, exposure_s: float, gain_db: Optional[float] = None) -> Dict[str, Any]:
        """Update hardware exposure duration and optional gain."""
        with self.lock:
            target_gain = gain_db if gain_db is not None else self.camera.current_gain_db
            reg_info = self.camera.set_hardware_exposure(exposure_s, target_gain)
            self.last_capture_time = 0.0
            if self.focus.active:
                self.focus.last_action = time.time()
            self._persist_camera_settings()
            self.activity.info(
                "camera",
                f"Exposure set to {reg_info.get('actual_exposure_s')}s, gain {reg_info.get('gain_db')} dB"
                f" (requested {exposure_s}s / {target_gain} dB)",
            )
            return {"success": True, "settings": reg_info}

    def set_gain(self, gain_db: float) -> Dict[str, Any]:
        """Update sensor analog/digital gain."""
        with self.lock:
            reg_info = self.camera.set_hardware_exposure(self.camera.current_exposure_s, gain_db)
            self.last_capture_time = 0.0
            self._persist_camera_settings()
            return {"success": True, "settings": reg_info}

    def set_mode(self, mode: str) -> Dict[str, Any]:
        """Toggle between Auto and Manual modes."""
        mode_clean = mode.lower().strip()
        # Plain Auto/Manual drops any exposure or gain lock: Auto computes both knobs again
        self.auto_exposure.lock_exposure_s = None
        self.auto_exposure.lock_gain_db = None
        if mode_clean in ["auto", "automatic"]:
            self.mode = "auto"
            if not bool(os.environ.get("PYTEST_CURRENT_TEST")) and self.last_frame is not None:
                try:
                    self.auto_exposure.exposure_s = float(getattr(self.camera, "current_exposure_s", self.auto_exposure.exposure_s))
                    self.auto_exposure.gain_db = float(getattr(self.camera, "current_gain_db", self.auto_exposure.gain_db))
                    self.auto_exposure.hw_min_exposure_s = getattr(self.camera, "min_exposure_s", None)
                    limits = camera_limits(self.camera)
                    self.auto_exposure.hw_max_exposure_s = limits.get("max_exposure_s")
                    self.auto_exposure.hw_min_gain_db = limits.get("min_gain_db")
                    self.auto_exposure.hw_max_gain_db = limits.get("max_gain_db")
                    new_exp, new_gain, _ = self.auto_exposure.process_frame_feedback(self.last_frame, live_camera=True)
                    self.camera.set_hardware_exposure(new_exp, new_gain)
                    self.last_capture_time = 0.0
                except Exception:
                    pass
        else:
            self.mode = "manual"
        self._persist_camera_settings()
        self.activity.info("camera", f"Exposure mode set to {self.mode.upper()}")
        return {"success": True, "mode": self.mode}

    # Picture-brightness limits for the lock warning (frame mean, 0-255)
    WARN_BRIGHT = 215.0          # washed out: highlights clip
    WARN_DARK_DAY = 25.0         # sun up or twilight: a frame this dark is wrong
    WARN_DARK_NIGHT = 4.0        # night: practically black
    WARN_OFF_TARGET = 0.4        # locked target missed by more than 40% (and 25 levels)

    def exposure_warning(self) -> Optional[Dict[str, str]]:
        """A hint when something is locked (exposure, gain or the target brightness; manual = both) and the picture is
        far too dark or too bright, because auto exposure cannot correct what is held. None when nothing is locked
        or the brightness is fine."""
        ae, br = self.auto_exposure, self.last_brightness
        locks = "both" if self.mode == "manual" else ae.lock_name()
        if br is None or not (locks or ae.target_brightness is not None):
            return None
        held = {"both": "Exposure and gain are locked", "exposure": "Exposure is locked", "gain": "Gain is locked"}.get(
            locks or "") or f"Target brightness is locked at {float(ae.target_brightness):.0f}"
        night = float(getattr(ae, "solar_elevation", -20.0)) < -6.0
        if br > self.WARN_BRIGHT:
            return {"kind": "bright", "message": f"Picture too bright ({br:.0f}/255). {held}: lower it or let Auto work."}
        if br < (self.WARN_DARK_NIGHT if night else self.WARN_DARK_DAY):
            return {"kind": "dark", "message": f"Picture too dark ({br:.0f}/255). {held}: raise it or let Auto work."}
        t = ae.target_brightness
        if t is not None and abs(br - t) > max(25.0, self.WARN_OFF_TARGET * t) and locks:
            return {"kind": "off_target", "message": f"Brightness {br:.0f} is far from the target {t:.0f}. {held}, so Auto cannot reach it."}
        return None

    def get_ae_lock(self) -> Dict[str, Any]:
        """Which knobs are held: manual mode is both locked, auto may hold one of them."""
        if self.mode == "manual":
            return {"lock": "both", "exposure_s": float(self.camera.current_exposure_s),
                    "gain_db": float(self.camera.current_gain_db),
                    "target_brightness": self.auto_exposure.target_brightness, "brightness": self.last_brightness,
                    "warning": self.exposure_warning()}
        ae = self.auto_exposure
        return {"lock": ae.lock_name(), "exposure_s": ae.lock_exposure_s, "gain_db": ae.lock_gain_db,
                "target_brightness": ae.target_brightness, "brightness": self.last_brightness,
                "warning": self.exposure_warning()}

    def set_target_brightness(self, value: Optional[float]) -> Dict[str, Any]:
        """Lock the auto-exposure target for the frame's mean brightness (0-255), or None for the built-in targets."""
        self.auto_exposure.target_brightness = None if value is None else max(5.0, min(250.0, float(value)))
        self._persist_camera_settings()
        t = self.auto_exposure.target_brightness
        self.activity.info("camera", f"Target brightness locked at {t:.0f}/255" if t is not None
                           else "Target brightness back to automatic day/night targets")
        return {"success": True, "mode": self.mode, "ae_lock": self.get_ae_lock()}

    def set_ae_lock(self, lock: str, value: Optional[float] = None,
                    exposure_s: Optional[float] = None, gain_db: Optional[float] = None) -> Dict[str, Any]:
        """Lock exposure, gain, both (= manual) or neither (= full auto); the loop adjusts what is unlocked."""
        if lock not in ("none", "exposure", "gain", "both"):
            raise ValueError("lock must be 'none', 'exposure', 'gain' or 'both'")
        if lock == "both":
            exp = exposure_s if exposure_s is not None else self.camera.current_exposure_s
            gain = gain_db if gain_db is not None else self.camera.current_gain_db
            self.set_mode("manual")
            self.set_exposure(float(exp), float(gain))
            return {"success": True, "mode": self.mode, "ae_lock": self.get_ae_lock()}
        with self.lock:
            ae = self.auto_exposure
            lim = camera_limits(self.camera)
            min_exp = float(lim.get("min_exposure_s") or 1e-6)
            max_exp = float(lim.get("max_exposure_s") or 15.0)
            max_gain = 48.0 if lim.get("max_gain_db") is None else float(lim["max_gain_db"])
            old_exp = max(min_exp, float(getattr(self.camera, "current_exposure_s", ae.exposure_s) or ae.exposure_s))
            old_gain = float(getattr(self.camera, "current_gain_db", ae.gain_db))
            ae.hw_min_exposure_s = min_exp
            ae.hw_max_exposure_s = max_exp
            ae.hw_min_gain_db = lim.get("min_gain_db")
            ae.hw_max_gain_db = lim.get("max_gain_db")
            ae.lock_exposure_s = ae.lock_gain_db = None
            if lock == "exposure":
                v = float(value if value is not None else old_exp)
                ae.lock_exposure_s = max(min_exp, min(max_exp, v))
                ae.exposure_s = ae.lock_exposure_s
                if old_exp > 0 and ae.lock_exposure_s > 0 and abs(ae.lock_exposure_s - old_exp) / max(old_exp, 1e-6) > 0.05:
                    ev_delta_db = 20.0 * math.log10(old_exp / ae.lock_exposure_s)
                    ae.gain_db = max(0.0, min(max_gain, round(old_gain + ev_delta_db, 1)))
                if self.last_frame is not None:
                    ae.process_frame_feedback(self.last_frame)
            elif lock == "gain":
                v = float(value if value is not None else old_gain)
                ae.lock_gain_db = max(0.0, min(max_gain, v))
                ae.gain_db = ae.lock_gain_db
                if abs(ae.lock_gain_db - old_gain) > 0.5:
                    ev_scale = 10.0 ** ((old_gain - ae.lock_gain_db) / 20.0)
                    ae.exposure_s = max(min_exp, min(max_exp, old_exp * ev_scale))
                if self.last_frame is not None:
                    ae.process_frame_feedback(self.last_frame)
            else:
                if self.last_frame is not None:
                    ae.process_frame_feedback(self.last_frame)
            self.mode = "auto"
            self.camera.set_hardware_exposure(
                ae.lock_exposure_s if ae.lock_exposure_s is not None else ae.exposure_s,
                ae.lock_gain_db if ae.lock_gain_db is not None else ae.gain_db,
            )
            self.last_capture_time = 0.0
            self._persist_camera_settings()
        if lock == "none":
            self.activity.info("camera", "Auto exposure: exposure and gain both automatic")
        else:
            held = f"{ae.lock_exposure_s:.4g}s" if lock == "exposure" else f"{ae.lock_gain_db:.1f} dB"
            self.activity.info("camera", f"Auto exposure: {lock} locked at {held}, the other follows the sky")
        return {"success": True, "mode": self.mode, "ae_lock": self.get_ae_lock()}

    def _persist_camera_settings(self) -> None:
        """Remember exposure mode and manual exposure/gain so they survive a server restart."""
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        self.station_config["camera_settings"] = {
            "mode": self.mode,
            "exposure_s": float(getattr(self.camera, "current_exposure_s", 0.2)),
            "gain_db": float(getattr(self.camera, "current_gain_db", 12.0)),
            "lock_exposure_s": self.auto_exposure.lock_exposure_s,
            "lock_gain_db": self.auto_exposure.lock_gain_db,
            "target_brightness": self.auto_exposure.target_brightness,
        }
        self._save_station_config()

    def restore_camera_settings(self) -> Optional[Dict[str, Any]]:
        """Re-apply the last saved manual exposure/gain on startup (auto mode needs nothing restored)."""
        cs = self.station_config.get("camera_settings") or {}
        self.auto_exposure.target_brightness = cs.get("target_brightness")
        if cs.get("mode") != "manual":
            self.auto_exposure.lock_exposure_s = cs.get("lock_exposure_s")
            self.auto_exposure.lock_gain_db = cs.get("lock_gain_db")
            return None
        try:
            self.camera.set_hardware_exposure(float(cs["exposure_s"]), float(cs["gain_db"]))
            self.mode = "manual"
            self.last_capture_time = 0.0
        except Exception:
            return None
        return dict(cs)
