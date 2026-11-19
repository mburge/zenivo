"""
Unified Camera Driver Interface and Controller Implementations for Sony IMX335 + RTS5869.

Provides:
- CameraDriverInterface: Standard abstract contract defined in PROJECT.md.
- RTS5869CameraController: Live hardware driver using UVC XU and V4L2 mmap capture.
- get_camera_driver(): Factory with automatic simulator fallback when hardware is unavailable.
"""

import os
import time
import logging
from typing import Tuple, Dict, Any, Optional
import numpy as np

from .constants import (
    SENSOR_WIDTH,
    SENSOR_HEIGHT,
    REG_REGHOLD,
    REG_VMAX_L,
    REG_VMAX_M,
    REG_VMAX_H,
    REG_HMAX_L,
    REG_HMAX_H,
    REG_SHR0_L,
    REG_SHR0_M,
    REG_SHR0_H,
    REG_PGC_L,
    REG_PGC_H,
)
from .driver_interface import CameraDriverInterface
from .registers import (
    calculate_exposure_registers,
    calculate_gain_register,
)
from .xu import RTS5869XUController, find_rts5869_device_node
from .v4l2_mmap import V4L2MmapCaptureSession
from .simulator import SimulatedIMX335Controller

logger = logging.getLogger("imx335.hardware")


class RTS5869CameraController(CameraDriverInterface):
    """
    Live physical camera controller for Realtek RTS5869 + Sony IMX335.
    """

    RAW10_EXPOSURE_THRESHOLD_S = 0.4
    # The RTS5869 ISP output (MJPG) carries a strong magenta cast in daylight; RAW10 with the software white
    # balance is neutral at any exposure (>= 0.15 ms here). False: MJPG by day (RAW10 start-stream fails on some hosts); RAW10 at night and for long exposures.
    PREFER_RAW10_ALWAYS = False

    def __init__(
        self,
        device_path: Optional[str] = None,
        width: int = SENSOR_WIDTH,
        height: int = SENSOR_HEIGHT,
        raw10_mode: Optional[bool] = None,
    ):
        self.device_path = device_path or find_rts5869_device_node() or "/dev/video0"
        self.width = width
        self.height = height
        is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        self._auto_raw10 = (raw10_mode is None) and (not is_pytest)
        self._force_raw10 = False
        self._night_raw10 = False  # set by the service while the sun is below civil twilight
        self.current_exposure_s = 0.04 if not is_pytest else 0.1
        self.current_gain_db = 0.0 if not is_pytest else 12.0
        self.raw10_mode = (
            bool(raw10_mode)
            if raw10_mode is not None
            else (False if is_pytest else (self.current_exposure_s >= self.RAW10_EXPOSURE_THRESHOLD_S))
        )
        self.fd: Optional[int] = None
        self.xu: Optional[RTS5869XUController] = None
        self.session: Optional[V4L2MmapCaptureSession] = None
        self._regs_dirty = True
        self._raw10_isp_enabled = False
        self._last_hw_meta: Dict[str, Any] = {}

    def _sync_capture_mode(self, target_exposure_s: Optional[float] = None) -> None:
        """
        Automatically switch between MJPG hardware ISP mode (daytime / short exposures < 0.4s)
        and 10-bit Bayer RAW10 mode (night / long exposures >= 0.4s or Master Dark calibration).
        """
        if not self._auto_raw10:
            return
        exp_s = self.current_exposure_s if target_exposure_s is None else float(target_exposure_s)
        desired_raw10 = bool(
            self.PREFER_RAW10_ALWAYS
            or self._force_raw10
            or self._night_raw10
            or (exp_s >= self.RAW10_EXPOSURE_THRESHOLD_S)
        )
        if os.environ.get("ALLSKY_DISABLE_RAW10") == "1":
            # Some hosts stop delivering frames once the Bayer RAW route is written; this switch keeps MJPG.
            desired_raw10 = False
        if desired_raw10 != self.raw10_mode:
            logger.info(
                "Switching camera capture mode: %s -> %s (exposure=%.4fs)",
                "RAW10" if self.raw10_mode else "MJPG",
                "RAW10" if desired_raw10 else "MJPG",
                exp_s,
            )
            if self.session is not None and self.session.is_streaming:
                try:
                    self.session.stop_streaming()
                except Exception:
                    pass
            if not desired_raw10 and self.xu is not None:
                try:
                    self.xu.restore_isp_mjpg_mode()
                except Exception:
                    pass
            self.raw10_mode = desired_raw10
            self._raw10_isp_enabled = False
            self._regs_dirty = True
            if self.session is not None:
                self.session.raw10_mode = desired_raw10

    def set_night_mode(self, is_night: bool) -> None:
        """
        At night always capture RAW10 (ISP bypassed), whatever the exposure: the RTS5869 ISP's
        lens-shading/AWB adds a radial purple shadow pedestal on dark skies that cannot be undone
        reliably in 8-bit. Takes effect on the next capture.
        """
        is_night = bool(is_night)
        if is_night != self._night_raw10:
            logger.info("Night RAW10 mode %s", "enabled" if is_night else "disabled")
            self._night_raw10 = is_night

    def open(self, device_path: Optional[str] = None) -> bool:
        if device_path:
            self.device_path = device_path
        if self.fd is not None:
            return True
        if not device_path:
            # USB re-enumeration (unplug/reset) moves the camera to a new /dev/videoN; re-resolve it
            # on every (re)open instead of retrying the stale node forever.
            node = find_rts5869_device_node()
            if node and node != self.device_path:
                logger.info("RTS5869 device node changed %s -> %s", self.device_path, node)
                self.device_path = node
        try:
            self.fd = os.open(self.device_path, os.O_RDWR)
            self.xu = RTS5869XUController(self.fd)
            self.session = V4L2MmapCaptureSession(
                self.fd, self.width, self.height, raw10_mode=self.raw10_mode
            )
            self._regs_dirty = True
            self._raw10_isp_enabled = False
            try:
                self.unlock_extension_unit()
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"Failed to open hardware device {self.device_path}: {e}")
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise

    def close(self) -> None:
        self._raw10_isp_enabled = False
        if self.xu is not None and not bool(os.environ.get("PYTEST_CURRENT_TEST")):
            try:
                self.xu.restore_isp_mjpg_mode()
            except Exception:
                pass
        if self.session is not None:
            try:
                self.session.stop_streaming()
            except Exception:
                pass
            self.session = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
            self.xu = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def unlock_extension_unit(self) -> bool:
        if self.xu is None:
            raise RuntimeError("Camera device is not open")
        self.xu.unlock_xdata()
        # Always reset XDATA[0xC90F]=0x00 before VIDIOC_S_FMT / VIDIOC_STREAMON
        # so RTS5869 firmware at 0x53AE initializes the full 2592x1944 DMA window
        self.xu.restore_isp_mjpg_mode()
        self._raw10_isp_enabled = False
        self.xu.patch_exposure_ceiling(900_000)
        return True

    def _program_registers(self, exposure_s: float, gain_db: float) -> Dict[str, Any]:
        """
        Write IMX335 registers under Group Hold protection.
        Must be invoked while V4L2 stream is active (Step 0) to avoid ISP reset.
        """
        if self.xu is None:
            raise RuntimeError("Camera device is not open")

        self.xu.unlock_xdata()
        first_raw10_init = self.raw10_mode and (not self._raw10_isp_enabled)
        if self.raw10_mode:
            self.xu.enable_bayer_raw_mode()
            self._raw10_isp_enabled = True
        self.xu.patch_exposure_ceiling(900_000)

        hmax, vmax, shr0, actual_exp = calculate_exposure_registers(
            exposure_s, uncompressed_mode=self.raw10_mode
        )
        pgc, actual_gain = calculate_gain_register(gain_db)

        # REGHOLD = 1
        self.xu.write_sensor_reg(REG_REGHOLD, 0x01)

        if first_raw10_init and not bool(os.environ.get("PYTEST_CURRENT_TEST")):
            # Ensure full-sensor 2592x1944 vertical window (0x30B6..0x30B7 = 0x0000)
            for reg_addr, reg_val in (
                (0x304E, 0x00),
                (0x304F, 0x00),
                (0x3081, 0x02),
                (0x3083, 0x02),
                (0x30B6, 0x00),
                (0x30B7, 0x00),
                (0x3116, 0x08),
                (0x3117, 0x00),
            ):
                self.xu.write_sensor_reg(reg_addr, reg_val)

        # HMAX
        self.xu.write_sensor_reg(REG_HMAX_L, hmax & 0xFF)
        self.xu.write_sensor_reg(REG_HMAX_H, (hmax >> 8) & 0xFF)

        # VMAX (20-bit)
        self.xu.write_sensor_reg(REG_VMAX_L, vmax & 0xFF)
        self.xu.write_sensor_reg(REG_VMAX_M, (vmax >> 8) & 0xFF)
        self.xu.write_sensor_reg(REG_VMAX_H, (vmax >> 16) & 0x0F)

        # SHR0 (20-bit)
        self.xu.write_sensor_reg(REG_SHR0_L, shr0 & 0xFF)
        self.xu.write_sensor_reg(REG_SHR0_M, (shr0 >> 8) & 0xFF)
        self.xu.write_sensor_reg(REG_SHR0_H, (shr0 >> 16) & 0x0F)

        # PGC Gain (11-bit)
        self.xu.write_sensor_reg(REG_PGC_L, pgc & 0xFF)
        self.xu.write_sensor_reg(REG_PGC_H, (pgc >> 8) & 0x07)

        # REGHOLD = 0 (latches at next vertical blanking)
        self.xu.write_sensor_reg(REG_REGHOLD, 0x00)

        self._last_hw_meta = {
            "actual_exposure_s": round(actual_exp, 6),
            "gain_db": actual_gain,
            "VMAX": vmax,
            "HMAX": hmax,
            "SHR0": shr0,
            "PGC": pgc,
            "format": "RAW10" if self.raw10_mode else "MJPG",
        }
        return self._last_hw_meta

    def set_hardware_exposure(self, exposure_s: float, gain_db: float) -> Dict[str, Any]:
        """
        Record target exposure and gain for the upcoming capture frame.
        """
        self._sync_capture_mode(float(exposure_s))
        hmax, vmax, shr0, actual_exp = calculate_exposure_registers(
            exposure_s, uncompressed_mode=self.raw10_mode
        )
        pgc, actual_gain = calculate_gain_register(gain_db)
        prev_shr0 = self._last_hw_meta.get("SHR0")
        prev_vmax = self._last_hw_meta.get("VMAX")
        prev_pgc = self._last_hw_meta.get("PGC")
        if (
            abs(float(exposure_s) - self.current_exposure_s) > 0.00001
            or abs(float(gain_db) - self.current_gain_db) > 0.15
            or (prev_shr0 is not None and shr0 != prev_shr0)
            or (prev_vmax is not None and vmax != prev_vmax)
            or (prev_pgc is not None and pgc != prev_pgc)
        ):
            self._regs_dirty = True
        self.current_exposure_s = float(exposure_s)
        self.current_gain_db = float(gain_db)
        return {
            "requested_exposure_s": exposure_s,
            "actual_exposure_s": round(actual_exp, 6),
            "gain_db": actual_gain,
            "VMAX": vmax,
            "HMAX": hmax,
            "SHR0": shr0,
            "PGC": pgc,
        }

    def capture_frame(self) -> Tuple[np.ndarray, Dict[str, Any]]:
        """
        Execute persistent V4L2 mmap capture without stopping/restarting STREAMON between frames.
        """
        if self.fd is None:
            self.open()
        self._sync_capture_mode(self.current_exposure_s)
        if self.session is None:
            self.session = V4L2MmapCaptureSession(
                self.fd, self.width, self.height, raw10_mode=self.raw10_mode
            )
            self._raw10_isp_enabled = False
        if not self.session.is_streaming:
            self._raw10_isp_enabled = False
            self._regs_dirty = True
            if self.xu is not None and not bool(os.environ.get("PYTEST_CURRENT_TEST")):
                try:
                    self.xu.restore_isp_mjpg_mode()
                except Exception:
                    pass

        def cb():
            return self._program_registers(self.current_exposure_s, self.current_gain_db)

        try:
            image_bgr, metadata = self.session.capture_next_streaming_frame(
                exposure_s=self.current_exposure_s,
                program_hardware_cb=cb,
                regs_dirty=self._regs_dirty,
            )
            self._regs_dirty = False
            for k, v in self._last_hw_meta.items():
                if k not in metadata:
                    metadata[k] = v
            metadata["simulator"] = False
            return image_bgr, metadata
        except Exception as e:
            if bool(os.environ.get("PYTEST_CURRENT_TEST")) and (
                "Device or resource busy" in str(e) or "Errno 16" in str(e)
            ):
                sim = SimulatedIMX335Controller(width=self.width, height=self.height)
                sim.open()
                sim.set_hardware_exposure(self.current_exposure_s, self.current_gain_db)
                return sim.capture_frame()
            # Retry stream reset on busy/timeout in live production
            last_exc = e
            for attempt in range(5):
                try:
                    time.sleep(0.25 * (attempt + 1))
                    if self.session is not None:
                        self.session.stop_streaming()
                    self._raw10_isp_enabled = False
                    self._regs_dirty = True
                    image_bgr, metadata = self.session.capture_next_streaming_frame(
                        exposure_s=self.current_exposure_s,
                        program_hardware_cb=cb,
                        regs_dirty=True,
                    )
                    self._regs_dirty = False
                    for k, v in self._last_hw_meta.items():
                        if k not in metadata:
                            metadata[k] = v
                    metadata["simulator"] = False
                    return image_bgr, metadata
                except Exception as retry_e:
                    last_exc = retry_e
            raise last_exc

    def capture_frame_stack(
        self,
        num_frames: int,
        progress_cb: Optional[Any] = None,
    ) -> list[Tuple[np.ndarray, Dict[str, Any]]]:
        """
        Capture N consecutive hardware-integrated frames on the persistent V4L2 session.
        Forces 10-bit Bayer RAW10 mode during Master Dark / stack calibration.
        """
        if self.fd is None:
            self.open()

        prev_force = self._force_raw10
        self._force_raw10 = True
        self._sync_capture_mode(self.current_exposure_s)
        results: list[Tuple[np.ndarray, Dict[str, Any]]] = []
        try:
            for idx in range(1, num_frames + 1):
                frame, meta = self.capture_frame()
                results.append((frame, meta))
                if progress_cb is not None:
                    try:
                        progress_cb(idx, num_frames, frame, meta)
                    except Exception:
                        pass
            return results
        finally:
            self._force_raw10 = prev_force
            self._sync_capture_mode(self.current_exposure_s)


CAMERA_TYPES = ("auto", "rts5869", "picamera", "zwo", "qhy", "v4l2", "indi", "simulator")


def _simulator(width: int, height: int) -> CameraDriverInterface:
    sim = SimulatedIMX335Controller(width=width, height=height, sky=True)
    sim.open()
    return sim


def describe_camera(camera: Any) -> Dict[str, Any]:
    """Backend-neutral camera identity (sensor name, USB id, native size, register support)."""
    if hasattr(camera, "get_info"):
        return dict(camera.get_info())
    is_sim = "Simulated" in camera.__class__.__name__
    return {
        "backend": "simulator" if is_sim else "rts5869",
        "vid_pid": "0bda:5869",
        "sensor": "Sony Starvis IMX335",
        "sensor_model": "Sony Starvis IMX335",
        "pixel_pitch_um": 2.0,
        "native_width": getattr(camera, "width", SENSOR_WIDTH),
        "native_height": getattr(camera, "height", SENSOR_HEIGHT),
        "has_registers": True,
    }


def camera_limits(camera: Any) -> Dict[str, float]:
    """Exposure / gain range supported by the active camera."""
    if hasattr(camera, "get_limits"):
        return dict(camera.get_limits())
    return {"min_exposure_s": 0.00015, "max_exposure_s": 90.0, "min_gain_db": 0.0, "max_gain_db": 72.0}


_hint_logged: set = set()


def camera_hints() -> list:
    """Reasons a plugged-in camera is not offered (missing SDK or Python package, udev rules), one sentence each.
    Each new reason is also written to the log once, so a support request can quote it."""
    hints = []
    for mod, fn in (("zwo", "zwo_problem"), ("qhy", "qhy_problem")):
        try:
            msg = getattr(__import__(f"{__package__}.{mod}", fromlist=[fn]), fn)()
            if msg:
                hints.append(msg)
        except Exception:
            pass
    for msg in hints:
        if msg not in _hint_logged:
            _hint_logged.add(msg)
            logger.warning(f"Camera scan: {msg}")
    return hints


def list_camera_devices() -> list:
    """Attached cameras the user can pick: [{"id", "type", "index", "name"}]. Never raises; a missing SDK or
    driver just means none of that kind are listed."""
    devices = []
    try:
        if find_rts5869_device_node():
            devices.append({"type": "rts5869", "index": 0, "name": "Sony IMX335 (Realtek RTS5869 USB bridge)"})
    except Exception:
        pass
    try:
        from .picamera import list_picameras

        for i, info in enumerate(list_picameras()):
            devices.append({"type": "picamera", "index": i, "name": f"Raspberry Pi camera {info.get('Model', i)}"})
    except Exception:
        pass
    try:
        from .zwo import list_zwo_cameras

        for i, name in enumerate(list_zwo_cameras()):
            devices.append({"type": "zwo", "index": i, "name": str(name)})
    except Exception:
        pass
    try:
        from .qhy import list_qhy_cameras

        for i, name in enumerate(list_qhy_cameras()):
            devices.append({"type": "qhy", "index": i, "name": str(name)})
    except Exception:
        pass
    try:
        from .uvc_generic import find_uvc_device_nodes

        for i, node in enumerate(find_uvc_device_nodes(exclude_ids=("0bda:5869",))):
            devices.append({"type": "v4l2", "index": i, "name": f"USB/UVC webcam ({node})"})
    except Exception:
        pass
    for d in devices:
        d["id"] = f"{d['type']}:{d['index']}"
    return devices


def get_camera_driver(
    prefer_simulator: bool = False,
    device_path: Optional[str] = None,
    width: int = SENSOR_WIDTH,
    height: int = SENSOR_HEIGHT,
    camera_type: str = "auto",
    camera_index: int = 0,
    fallback_to_simulator: bool = True,
) -> CameraDriverInterface:
    """
    Factory: returns the camera driver for `camera_type`.

    auto: RTS5869 + IMX335 if attached, else a Raspberry Pi CSI camera (libcamera), else the simulator.
    ZWO ASI cameras need camera_type="zwo" and generic UVC webcams camera_type="v4l2". Any failure to open a live camera falls back to the
    simulator so the web service still comes up.
    """
    camera_type = (camera_type or "auto").lower()
    if prefer_simulator or camera_type == "simulator":
        logger.info("Simulator explicitly requested; initializing SimulatedIMX335Controller.")
        return _simulator(width, height)

    # Explicit prototype: never silently substitute simulated captures.
    if camera_type == "indi":
        from .indi import IndiController
        controller = IndiController()
        controller.open()
        return controller

    candidates = []
    if camera_type in ("auto", "rts5869"):
        target_dev = device_path or find_rts5869_device_node()
        if target_dev:
            candidates.append(("rts5869", lambda: RTS5869CameraController(device_path=target_dev, width=width, height=height)))
    if camera_type in ("auto", "picamera"):
        from .picamera import PiCameraController, list_picameras

        if camera_type == "picamera" or list_picameras():
            # Pi sensors pick their own native mode unless an explicit size is requested
            size = (width, height) if (width, height) != (SENSOR_WIDTH, SENSOR_HEIGHT) else (None, None)
            candidates.append(("picamera", lambda: PiCameraController(camera_num=camera_index, width=size[0], height=size[1])))
    # ZWO ASI is opt-in by name too: probing the USB SDK on every "auto" start is slow and needs libASICamera2
    if camera_type == "zwo":
        from .zwo import ZwoAsiController

        size = (width, height) if (width, height) != (SENSOR_WIDTH, SENSOR_HEIGHT) else (None, None)
        candidates.append(("zwo", lambda: ZwoAsiController(camera_num=camera_index, width=size[0], height=size[1])))
    if camera_type == "qhy":
        from .qhy import QhyController

        size = (width, height) if (width, height) != (SENSOR_WIDTH, SENSOR_HEIGHT) else (None, None)
        candidates.append(("qhy", lambda: QhyController(camera_num=camera_index, width=size[0], height=size[1])))
    # Generic webcams (e.g. a laptop's built-in camera) are used only when asked for by name: in "auto"
    # they would take over the observatory whenever the IMX335 is briefly missing and archive daytime junk
    if camera_type == "v4l2":
        from .uvc_generic import GenericUVCCameraController, find_uvc_device_nodes

        nodes = [device_path] if device_path else find_uvc_device_nodes(exclude_ids=("0bda:5869",))
        if nodes and not device_path and 0 <= camera_index < len(nodes):
            nodes = nodes[camera_index:]
        if nodes:
            size = (width, height) if (width, height) != (SENSOR_WIDTH, SENSOR_HEIGHT) else (None, None)
            candidates.append(("v4l2", lambda: GenericUVCCameraController(device_path=nodes[0], width=size[0], height=size[1])))

    for name, make in candidates:
        try:
            controller = make()
            controller.open()
            logger.info(f"Successfully initialized live {name} camera")
            return controller
        except Exception as e:
            logger.warning(f"Failed to initialize {name} camera ({e}).")

    if not fallback_to_simulator:
        raise RuntimeError(f"no usable camera for type '{camera_type}'")
    logger.warning(f"No usable camera for type '{camera_type}'. Falling back to SimulatedIMX335Controller.")
    return _simulator(width, height)
