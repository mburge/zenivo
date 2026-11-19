import numpy as np
import pytest

from src.api.traffic_mixin import TrafficMixin, _open_sky_contains


def _polygon(*points):
    return np.array(points, np.float32)


def test_overlay_point_must_be_in_sky_and_outside_obstruction():
    sky = [_polygon((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))]
    obstruction = [_polygon((0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6))]

    assert _open_sky_contains((0.2, 0.2), sky, obstruction)
    assert not _open_sky_contains((0.05, 0.2), sky, obstruction)
    assert not _open_sky_contains((0.5, 0.5), sky, obstruction)
    assert not _open_sky_contains(None, sky, obstruction)


def test_missing_positive_mask_preserves_rectangular_frame_behavior():
    obstruction = [_polygon((0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6))]

    assert _open_sky_contains((0.95, 0.95), [], obstruction)
    assert not _open_sky_contains((0.5, 0.5), [], obstruction)


def test_native_solver_point_is_transformed_to_display_mask_space():
    service = object.__new__(TrafficMixin)
    service.station_config = {
        "image_transform": {"flip_horizontal": True, "flip_vertical": True, "rotate_deg": 0}
    }

    assert service._mask_space_xy((0.8, 0.7), {"coordinate_space": "display"}) == (0.8, 0.7)
    x, y = service._mask_space_xy((0.8, 0.7), {"coordinate_space": "native"})
    assert x == pytest.approx(0.2)
    assert y == pytest.approx(0.3)
