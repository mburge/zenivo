import math

import pytest

from src.hardware.indi import IndiController


def test_touptek_native_gain_maps_to_zenivo_db():
    camera = IndiController()
    camera._gain_min_native = 100.0
    camera._gain_max_native = 15000.0
    camera._gain_step_native = 1.0

    assert camera._native_gain_to_db(100) == pytest.approx(0.0)
    assert camera._native_gain_to_db(200) == pytest.approx(20.0 * math.log10(2.0))
    assert camera._db_to_native_gain(20.0 * math.log10(2.0)) == 200.0
    assert camera._db_to_native_gain(80.0) == 15000.0
