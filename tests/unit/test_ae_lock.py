"""Auto exposure with exposure or gain locked moves only the other knob."""
from datetime import datetime, timezone

import numpy as np

from src.calibration.auto_exposure import DayNightAutoExposure

NIGHT = datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)  # 00:00 in Hanoi
NOON = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)


def _frame(level):
    return np.full((1080, 1920, 3), level, dtype=np.uint8)


def test_gain_lock_brightens_with_exposure_only():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=1.0, initial_gain_db=12.0)
    ae.lock_gain_db = 20.0
    exp, gain, telem = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert gain == 20.0 and exp > 1.0 and telem["lock"] == "gain"


def test_exposure_lock_brightens_with_gain_only():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=1.0, initial_gain_db=12.0)
    ae.lock_exposure_s = 2.0
    exp, gain, telem = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert exp == 2.0 and gain > 12.0 and telem["lock"] == "exposure"


def test_exposure_lock_darkens_with_gain_only_in_daylight():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=0.01, initial_gain_db=20.0)
    ae.lock_exposure_s = 0.01
    exp, gain, _ = ae.process_frame_feedback(_frame(250), NOON, live_camera=True)
    assert exp == 0.01 and gain < 20.0


def test_no_lock_reports_none():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, )
    _, _, telem = ae.process_frame_feedback(_frame(100), NIGHT, live_camera=True)
    assert telem["lock"] is None


def test_gain_lock_lets_exposure_run_to_60s():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=15.0, initial_gain_db=8.0)
    ae.lock_gain_db = 8.0
    ae.hw_max_exposure_s = 90.0
    for _ in range(10):
        exp, gain, _ = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert exp == 60.0 and gain == 8.0


def test_gain_lock_respects_camera_max_exposure():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=15.0, initial_gain_db=8.0)
    ae.lock_gain_db = 8.0
    ae.hw_max_exposure_s = 30.0
    for _ in range(10):
        exp, _, _ = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert exp == 30.0


def test_unlocked_auto_also_runs_to_60s():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=15.0, initial_gain_db=48.0)
    ae.hw_max_exposure_s = 90.0
    for _ in range(10):
        exp, _, _ = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert exp == 60.0


def test_unlocked_auto_respects_hardware_gain_ceiling_and_keeps_lengthening_exposure():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542,
                              initial_exposure_s=4.0, initial_gain_db=43.5)
    ae.hw_max_exposure_s = 90.0
    ae.hw_max_gain_db = 43.5
    exp, gain, _ = ae.process_frame_feedback(_frame(5), NIGHT, live_camera=True)
    assert gain == 43.5
    assert exp > 4.0


def test_stamp_writes_corner_and_keeps_input():
    from src.pipeline.overlay import stamp_frame_info, frame_brightness
    frame = np.zeros((972, 1296, 3), dtype=np.uint8)
    out = stamp_frame_info(frame, NIGHT, 15.0, 11.1, frame_brightness(frame))
    assert frame.max() == 0
    assert out[:120, :450].max() == 255
    assert out[300:, :].max() == 0  # nothing outside the top-left corner


def test_target_brightness_steers_frame_mean():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=1.0, initial_gain_db=12.0)
    ae.target_brightness = 150.0
    exp, gain, telem = ae.process_frame_feedback(_frame(100), NIGHT, live_camera=True)
    assert exp * 10 ** (gain / 20) > 1.0 * 10 ** (12 / 20) and telem["target_luma"] == 150.0
    ae.target_brightness = 60.0
    exp2, gain2, _ = ae.process_frame_feedback(_frame(100), NIGHT, live_camera=True)
    assert exp2 * 10 ** (gain2 / 20) < exp * 10 ** (gain / 20)


def test_target_brightness_holds_inside_band():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=1.0, initial_gain_db=12.0)
    ae.target_brightness = 100.0
    exp, gain, _ = ae.process_frame_feedback(_frame(103), NIGHT, live_camera=True)
    assert (exp, gain) == (1.0, 12.0)


def test_night_whiteout_reduces_long_exposure_before_draining_gain():
    ae = DayNightAutoExposure(latitude_deg=21.0285, longitude_deg=105.8542, initial_exposure_s=9.5, initial_gain_db=22.0)
    exp, gain, _ = ae.process_frame_feedback(_frame(245), NIGHT, live_camera=True)
    assert exp < 5.0
    assert gain == 22.0

