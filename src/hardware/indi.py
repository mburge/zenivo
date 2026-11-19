"""AWAPS fixed-device mono INDI prototype. Not a generic camera discovery backend.

Uses the existing INDI server; indi-allsky must be stopped before capture.
The ToupTek native gain value is converted to dB as 20*log10(value/100), while
the sensor's high-conversion-gain mode remains fixed.
"""
import io
import math
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone

import numpy as np
import PyIndi
from astropy.io import fits

from .driver_interface import CameraDriverInterface
from .rawconv import raw16_to_bgr8


class _Client(PyIndi.BaseClient):
    def __init__(self, device_name):
        super().__init__()
        self.device_name = device_name
        self.blobs = queue.Queue(maxsize=2)

    def _receive(self, blob):
        payload = bytes(blob.getblobdata())
        try:
            self.blobs.put_nowait((time.time(), payload))
        except queue.Full:
            pass

    def newBLOB(self, blob):
        # Legacy callback is only used by older bindings.
        if blob.bvp.device == self.device_name:
            self._receive(blob)

    def updateProperty(self, prop):
        if hasattr(PyIndi.BaseMediator, "newNumber"):
            return
        if prop.getType() == PyIndi.INDI_BLOB:
            vector = PyIndi.PropertyBlob(prop)
            if vector.getDeviceName() == self.device_name and vector.getName() == "CCD1":
                self._receive(vector[0])


class IndiController(CameraDriverInterface):
    simulator = False
    backend = "indi"

    def __init__(self):
        self.device_name = os.environ.get("ZENIVO_INDI_DEVICE", "ToupTek AE676M")
        self.width = self.height = 0
        self.is_open = False
        self.client = self.device = None
        self.current_exposure_s = 0.01
        self.current_gain_db = 0.0
        self.min_exposure_s = 0.0001
        self.max_exposure_s = 90.0
        self.min_gain_db = 0.0
        self.max_gain_db = 0.0
        self._gain_unity_native = 100.0
        self._gain_min_native = 100.0
        self._gain_max_native = 100.0
        self._gain_step_native = 1.0
        self.pixel_size_um = 2.0
        self._lock = threading.RLock()
        self._inflight = False
        self._last_ts = None
        self.last_fits_header = {}

    def _number(self, name):
        vector = self.device.getNumber(name)
        if not vector:
            raise RuntimeError("Missing INDI property " + name)
        return vector

    @staticmethod
    def _indi_allsky_state():
        return subprocess.run(['systemctl', 'is-active', 'indi-allsky'],
                              capture_output=True, text=True, timeout=3).stdout.strip()

    def _require_exclusive_control(self):
        state = self._indi_allsky_state()
        if state not in ('inactive', 'failed'):
            raise RuntimeError('Stop indi-allsky before INDI control (state: ' + state + ')')

    def open(self, device_path=None):
        with self._lock:
            if self.is_open:
                return True
            self._require_exclusive_control()
            self.client = _Client(self.device_name)
            self.client.setServer(os.environ.get("ZENIVO_INDI_HOST", "localhost"),
                                  int(os.environ.get("ZENIVO_INDI_PORT", "7624")))
            try:
                if not self.client.connectServer():
                    raise RuntimeError("Cannot connect to existing INDI server")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    self.device = self.client.getDevice(self.device_name)
                    if self.device and self.device.getNumber("CCD_INFO") and self.device.getNumber("CCD_EXPOSURE"):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("INDI camera properties not available: " + self.device_name)
                info = {p.name: float(p.value) for p in self._number("CCD_INFO")}
                self.width, self.height = int(info["CCD_MAX_X"]), int(info["CCD_MAX_Y"])
                self.pixel_size_um = info["CCD_PIXEL_SIZE"]
                exposure = self._number("CCD_EXPOSURE")[0]
                self.min_exposure_s = max(1e-6, float(exposure.min))
                self.max_exposure_s = min(90.0, float(exposure.max))
                gain = next(p for p in self._number("CCD_CONTROLS") if p.name == "Gain")
                self._gain_min_native = max(1.0, float(gain.min))
                self._gain_max_native = max(self._gain_min_native, float(gain.max))
                self._gain_step_native = max(1.0, float(gain.step))
                self.min_gain_db = self._native_gain_to_db(self._gain_min_native)
                self.max_gain_db = self._native_gain_to_db(self._gain_max_native)
                self._configure_fixed_settings()
                self.client.setBLOBMode(PyIndi.B_ALSO, self.device_name, "CCD1")
                self.is_open = True
                return True
            except Exception:
                self.close()
                raise

    def _set_number(self, name, element, value):
        vector = self._number(name)
        target = next((p for p in vector if p.name == element), None)
        if target is None:
            raise RuntimeError('Missing INDI element ' + name + '.' + element)
        target.value = value
        self.client.sendNewNumber(vector)
        deadline = time.monotonic() + 5
        while vector.getState() == PyIndi.IPS_BUSY and time.monotonic() < deadline:
            time.sleep(0.05)
        if vector.getState() == PyIndi.IPS_ALERT:
            raise RuntimeError(name + ' rejected configuration')

    def _set_switch(self, name, element):
        vector = self.device.getSwitch(name)
        if not vector or element not in [p.name for p in vector]:
            raise RuntimeError('Missing INDI element ' + name + '.' + element)
        for item in vector:
            item.s = PyIndi.ISS_ON if item.name == element else PyIndi.ISS_OFF
        self.client.sendNewSwitch(vector)
        deadline = time.monotonic() + 5
        while vector.getState() == PyIndi.IPS_BUSY and time.monotonic() < deadline:
            time.sleep(0.05)
        if vector.getState() == PyIndi.IPS_ALERT:
            raise RuntimeError(name + ' rejected configuration')

    def _native_gain_to_db(self, native_gain):
        return 20.0 * math.log10(max(1.0, float(native_gain)) / self._gain_unity_native)

    def _db_to_native_gain(self, gain_db):
        raw = self._gain_unity_native * 10.0 ** (float(gain_db) / 20.0)
        clamped = max(self._gain_min_native, min(self._gain_max_native, raw))
        steps = round((clamped - self._gain_min_native) / self._gain_step_native)
        return self._gain_min_native + steps * self._gain_step_native

    def _configure_fixed_settings(self):
        self._set_number('CCD_CONTROLS', 'Gain', 100.0)
        for name, element in (("TC_CONVERSION_GAIN", "GAIN_HIGH"),
                              ("CCD_CAPTURE_FORMAT", "INDI_MONO_16"),
                              ("CCD_COMPRESSION", "INDI_DISABLED"),
                              ("UPLOAD_MODE", "UPLOAD_CLIENT")):
            self._set_switch(name, element)
        self._check_fixed_settings()

    def _check_fixed_settings(self):
        gain = next(p.value for p in self._number("CCD_CONTROLS") if p.name == "Gain")
        if not self._gain_min_native <= float(gain) <= self._gain_max_native:
            raise RuntimeError("INDI gain is outside the advertised camera range")
        self.current_gain_db = self._native_gain_to_db(gain)
        for name, element in (("TC_CONVERSION_GAIN", "GAIN_HIGH"),
                              ("CCD_CAPTURE_FORMAT", "INDI_MONO_16"),
                              ("CCD_COMPRESSION", "INDI_DISABLED"),
                              ("UPLOAD_MODE", "UPLOAD_CLIENT")):
            vector = self.device.getSwitch(name)
            if not vector or not any(p.name == element and p.s == PyIndi.ISS_ON for p in vector):
                raise RuntimeError("Prototype requires " + name + "." + element)

    def unlock_extension_unit(self):
        return True

    def set_hardware_exposure(self, exposure_s, gain_db):
        with self._lock:
            self.current_exposure_s = max(self.min_exposure_s, min(self.max_exposure_s, float(exposure_s)))
            requested_gain_db = float(gain_db)
            native_gain = self._db_to_native_gain(requested_gain_db)
            self._set_number('CCD_CONTROLS', 'Gain', native_gain)
            actual_native_gain = float(next(p.value for p in self._number("CCD_CONTROLS") if p.name == "Gain"))
            self.current_gain_db = self._native_gain_to_db(actual_native_gain)
            return {"requested_exposure_s": exposure_s, "actual_exposure_s": self.current_exposure_s,
                    "requested_gain_db": requested_gain_db, "gain_db": self.current_gain_db,
                    "indi_gain": actual_native_gain, "VMAX": 0, "HMAX": 0, "SHR0": 0, "PGC": 0}

    def _abort(self):
        vector = self.device.getSwitch("CCD_ABORT_EXPOSURE")
        if vector:
            vector[0].s = PyIndi.ISS_ON
            self.client.sendNewSwitch(vector)
        self._inflight = False

    def capture_frame(self):
        with self._lock:
            self._require_exclusive_control()
            if not self.is_open:
                self.open()
            self._check_fixed_settings()
            vector = self._number("CCD_EXPOSURE")
            if vector.getState() == PyIndi.IPS_BUSY:
                raise RuntimeError("Camera busy: another exposure controller may be active")
            while True:
                try:
                    self.client.blobs.get_nowait()
                except queue.Empty:
                    break
            requested = self.current_exposure_s
            expected_native_gain = float(next(p.value for p in self._number("CCD_CONTROLS") if p.name == "Gain"))
            started = time.time()
            vector[0].value = requested
            self._inflight = True
            self.client.sendNewNumber(vector)
            try:
                received, payload = self.client.blobs.get(timeout=requested + 15)
                with fits.open(io.BytesIO(payload), memmap=False) as hdus:
                    hdus.verify("exception")
                    raw, header = hdus[0].data, hdus[0].header
                    actual = float(header["EXPTIME"])
                    if raw.shape != (self.height, self.width) or raw.dtype != np.uint16:
                        raise RuntimeError(f"Unexpected FITS image: {raw.shape}, {raw.dtype}")
                    if abs(actual - requested) > max(1e-6, requested * 0.001):
                        raise RuntimeError("FITS exposure differs from requested exposure")
                    actual_native_gain = float(header.get("GAIN", -1))
                    if abs(actual_native_gain - expected_native_gain) > max(0.01, self._gain_step_native / 2.0):
                        raise RuntimeError("FITS gain differs from requested INDI gain")
                    if received < started:
                        raise RuntimeError("Stale INDI frame")
                    observed = datetime.fromisoformat(str(header['DATE-OBS']).replace('Z', '+00:00'))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    if not started - 0.25 <= observed.timestamp() <= received + 0.25:
                        raise RuntimeError('FITS start time does not belong to this exposure')
                    self.last_fits_header = {k: header.get(k) for k in ("DATE-OBS", "EXPTIME", "GAIN", "BITPIX", "BZERO")}
                    self.current_gain_db = self._native_gain_to_db(actual_native_gain)
                    mono16 = raw.copy(order="C")
                    frame = raw16_to_bgr8(mono16)
                self._inflight = False
            except Exception:
                self._abort()
                raise
            delta = received - (self._last_ts or started)
            self._last_ts = received
            return frame, {"hw_frame_delta": delta * 1000, "actual_exposure_s": actual,
                           "gain_db": self.current_gain_db, "indi_gain": actual_native_gain,
                           "indi_conversion_gain": "HCG",
                           "timestamp": received, "exposure_started_at": started,
                           "fits_date_obs": self.last_fits_header["DATE-OBS"],
                           "simulator": False, "camera_backend": "indi",
                           "VMAX": 0, "HMAX": 0, "SHR0": 0, "PGC": 0,
                           "mono16": mono16}

    def close(self):
        with self._lock:
            if self.client:
                if self._inflight:
                    self._abort()
                self.client.disconnectServer()
            self.client = self.device = None
            self.is_open = False

    def get_info(self):
        return {"backend": "indi", "sensor": self.device_name + " (mono, INDI)",
                "sensor_model": self.device_name, "pixel_pitch_um": self.pixel_size_um,
                "native_width": self.width, "native_height": self.height,
                "has_registers": False,
                "indi_gain": self._db_to_native_gain(self.current_gain_db),
                "gain_mode": "variable analog gain, fixed HCG"}

    def get_limits(self):
        return {"min_exposure_s": self.min_exposure_s, "max_exposure_s": self.max_exposure_s,
                "min_gain_db": self.min_gain_db, "max_gain_db": self.max_gain_db}
