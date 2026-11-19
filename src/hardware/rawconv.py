"""Shared RAW16 -> 8-bit BGR conversion for the vendor-SDK USB cameras (ZWO, QHY)."""

from typing import Optional

import numpy as np

# Bayer pattern name (as written in camera docs: row 0 then row 1) -> OpenCV code. OpenCV names the
# pattern by its second row/column offset, so RGGB data is COLOR_BayerBG2BGR.
BAYER_TO_CV = {"RGGB": "COLOR_BayerBG2BGR", "BGGR": "COLOR_BayerRG2BGR", "GRBG": "COLOR_BayerGB2BGR", "GBRG": "COLOR_BayerGR2BGR"}


def raw16_to_bgr8(raw: np.ndarray, bayer: Optional[str] = None) -> np.ndarray:
    """RAW (uint16, full 16-bit scale) -> uint8 BGR. `bayer` None means a mono sensor (grey replicated to 3 channels)."""
    if raw.dtype == np.uint16:
        raw = raw >> 8
    raw = raw.astype(np.uint8, copy=False)
    if bayer:
        import cv2

        return cv2.cvtColor(raw, getattr(cv2, BAYER_TO_CV.get(bayer, BAYER_TO_CV["RGGB"])))
    return np.repeat(raw[:, :, None], 3, axis=2)


def calibrate_mono16(
    raw: np.ndarray,
    master_dark: np.ndarray,
    hot_pixel_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Subtract and repair a native uint16 mono frame before display conversion."""
    if raw.dtype != np.uint16 or master_dark.dtype != np.uint16:
        raise ValueError("mono calibration requires uint16 light and dark frames")
    if raw.ndim != 2 or master_dark.shape != raw.shape:
        raise ValueError("mono light and dark frames must be matching 2-D arrays")

    corrected = np.clip(
        raw.astype(np.int32) - master_dark.astype(np.int32), 0, 65535
    ).astype(np.uint16)
    if hot_pixel_mask is not None:
        if hot_pixel_mask.shape != raw.shape:
            raise ValueError("hot-pixel mask does not match mono frame")
        mask = hot_pixel_mask.astype(bool)
        if np.any(mask):
            import cv2

            local_median = cv2.medianBlur(corrected, 3)
            corrected[mask] = local_median[mask]
    return corrected
