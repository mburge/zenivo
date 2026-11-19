from types import SimpleNamespace

import numpy as np

from src.api.calibration_mixin import CalibrationMixin
from src.hardware.rawconv import calibrate_mono16


def test_mono16_subtraction_and_hot_pixel_repair():
    master = np.full((9, 9), 1000, dtype=np.uint16)
    master[4, 4] = 12000
    light = np.full((9, 9), 1800, dtype=np.uint16)
    light[4, 4] = 50000
    hot = np.zeros((9, 9), dtype=bool)
    hot[4, 4] = True

    corrected = calibrate_mono16(light, master, hot)

    assert corrected.dtype == np.uint16
    assert np.all(corrected != 0)
    assert corrected[0, 0] == 800
    assert corrected[4, 4] == 800


class _MonoHarness(CalibrationMixin):
    STANDARD_DARK_PROFILES = [
        {
            "id": "long_high",
            "label": "Long high gain",
            "exposure_s": 8.0,
            "gain_db": 48.0,
            "num_frames": 5,
        }
    ]

    def __init__(self, calibration_dir):
        self.camera = SimpleNamespace(backend="indi", max_gain_db=43.5218)
        self.calibration_dir = str(calibration_dir)
        self._dark_cache = {}


def test_mono16_library_uses_actual_camera_gain_and_applies_before_conversion(tmp_path):
    service = _MonoHarness(tmp_path)
    filename = "master_dark_mono16_exp8.0s_gain44dB.npy"
    master = np.full((8, 8), 1024, dtype=np.uint16)
    master[3, 3] = 8192
    np.save(tmp_path / filename, master)
    hot = np.zeros((8, 8), dtype=bool)
    hot[3, 3] = True
    np.save(tmp_path / "hot_pixels_exp8.0s_gain44dB.npy", hot)

    profile = service.get_profiles_matrix()[0]
    assert profile["format"] == "mono16"
    assert profile["mono16_ready"] is True
    assert profile["raw10_ready"] is False
    assert profile["gain_db"] == 43.5218
    assert profile["filename"] == filename

    light = np.full((8, 8), 2048, dtype=np.uint16)
    light[3, 3] = 60000
    fallback_bgr = np.zeros((8, 8, 3), dtype=np.uint8)
    calibrated, matched = service._apply_closest_master_dark(
        fallback_bgr, 8.0, 43.5218, mono16=light
    )

    assert matched == filename
    assert calibrated.shape == (8, 8, 3)
    assert calibrated.dtype == np.uint8
    assert np.all(calibrated[0, 0] == 4)
    assert np.all(calibrated[3, 3] == 4)


def test_mono16_preview_reports_full_scale_saturation(tmp_path):
    service = _MonoHarness(tmp_path)
    filename = "master_dark_mono16_exp8.0s_gain44dB.npy"
    master = np.full((8, 8), 1024, dtype=np.uint16)
    master[0, 0] = 65535
    np.save(tmp_path / filename, master)

    png, stats = service.get_dark_preview(filename)
    assert png.startswith(b"\x89PNG")
    assert stats["dtype"] == "uint16"
    assert stats["max"] == 65535.0
