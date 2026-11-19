"""Master dark library: profiles, preview, trigger and auto-build.

Mixin of AllSkyService (src/api/service.py): methods only, all state lives on the service instance."""

import contextlib
import os
import re
import time
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import numpy as np
import cv2
from src.hardware.v4l2_mmap import demosaic_raw10_bayer
from src.hardware.rawconv import calibrate_mono16, raw16_to_bgr8

class CalibrationMixin:
    def _native_dark_format(self) -> str:
        """Native linear calibration plane supplied by the active camera backend."""
        return "mono16" if getattr(self.camera, "backend", "") == "indi" else "raw10"

    def _native_dark_label(self) -> str:
        return "16-bit mono RAW" if self._native_dark_format() == "mono16" else "10-bit Bayer RAW"

    def _profile_gain(self, requested_gain_db: float) -> float:
        """Use the real camera limit so filenames describe the gain that was captured."""
        maximum = float(getattr(self.camera, "max_gain_db", requested_gain_db))
        return min(float(requested_gain_db), maximum)

    @staticmethod
    def _native_dark_filename(fmt: str, exposure_s: float, gain_db: float) -> str:
        return f"master_dark_{fmt}_exp{exposure_s:.1f}s_gain{gain_db:.0f}dB.npy"

    @contextlib.contextmanager
    def _lens_covered(self):
        """A simulated camera sees no sky while the dark frames are taken (a real one has its lens capped)."""
        cam = self.camera
        had = hasattr(cam, "lens_covered")
        if had:
            cam.lens_covered = True
        try:
            yield
        finally:
            if had:
                cam.lens_covered = False


    def _cleanup_legacy_dark_images(self) -> None:
        """Remove legacy .webp/.png dark preview files once 10-bit RAW (.npy) master darks exist."""
        if bool(os.environ.get("PYTEST_CURRENT_TEST")):
            return
        try:
            if not os.path.isdir(self.calibration_dir):
                return
            for fname in os.listdir(self.calibration_dir):
                if fname.startswith("master_dark_exp") and fname.endswith((".webp", ".png")):
                    try:
                        os.remove(os.path.join(self.calibration_dir, fname))
                    except Exception:
                        pass
        except Exception:
            pass

    def get_profiles_matrix(self) -> List[Dict[str, Any]]:
        """Return status of the standard native-RAW Master Dark profiles."""
        self._cleanup_legacy_dark_images()
        existing = {item["filename"]: item for item in self._list_calibration_files()}
        matrix: List[Dict[str, Any]] = []
        native_format = self._native_dark_format()
        for prof in self.STANDARD_DARK_PROFILES:
            gain_db = self._profile_gain(prof["gain_db"])
            fname_native = self._native_dark_filename(native_format, prof["exposure_s"], gain_db)
            fname_webp = f"master_dark_exp{prof['exposure_s']:.1f}s_gain{gain_db:.0f}dB.webp"
            fname_png = f"master_dark_exp{prof['exposure_s']:.1f}s_gain{gain_db:.0f}dB.png"
            info = existing.get(fname_native) or existing.get(fname_webp) or existing.get(fname_png)
            fname = info["filename"] if info else fname_native
            native_exists = fname_native in existing
            matrix.append({
                "id": prof["id"],
                "label": prof["label"],
                "exposure_s": prof["exposure_s"],
                "gain_db": gain_db,
                "num_frames": prof["num_frames"],
                "format": native_format,
                "filename": fname,
                "raw_filename": fname_native if native_exists else None,
                "ready": info is not None,
                "native_ready": native_exists,
                "raw10_ready": native_exists and native_format == "raw10",
                "mono16_ready": native_exists and native_format == "mono16",
                "size_kb": info["size_kb"] if info else 0.0,
                "modified_at": info["modified_at"] if info else None,
            })
        return matrix

    def _load_existing_calibration_summary(self) -> None:
        """Populate initial last_result if calibration files exist on disk."""
        try:
            self._cleanup_legacy_dark_images()
            library = self._list_calibration_files()
            matrix = self.get_profiles_matrix()
            ready_count = sum(1 for p in matrix if p["ready"])
            total_count = len(matrix)
            if library:
                latest = library[0]
                self.calibration_status["last_result"] = {
                    "success": True,
                    "filename": latest["filename"],
                    "file_path": latest["file_path"],
                    "size_bytes": latest["size_bytes"],
                    "timestamp": latest["modified_at"],
                }
                if ready_count == total_count:
                    self.calibration_status["state"] = "completed"
                    self.calibration_status["progress_pct"] = 100.0
                    self.calibration_status["message"] = (
                        f"All {total_count}/{total_count} Standard {self._native_dark_label()} Master Dark Profiles Ready — native subtraction active."
                    )
                else:
                    self.calibration_status["message"] = (
                        f"{ready_count}/{total_count} Standard {self._native_dark_label()} Master Dark Profiles Ready (Latest: {latest['filename']})"
                    )
        except Exception:
            pass

    def get_dark_preview(self, filename: str):
        """Return (png_bytes, stats) for a saved native RAW master dark."""
        import cv2
        name = os.path.basename(filename or "")
        valid_prefix = name.startswith("master_dark_raw10_") or name.startswith("master_dark_mono16_")
        if name != filename or not (valid_prefix and name.endswith(".npy")):
            return None
        mono16 = name.startswith("master_dark_mono16_")
        saturated_dn = 65535 if mono16 else 1023
        path = os.path.join(self.calibration_dir, name)
        if not os.path.isfile(path):
            return None
        arr = np.load(path)
        a = arr.astype(np.float32)
        med = float(np.median(a))
        p1, p99 = float(np.percentile(a, 1)), float(np.percentile(a, 99.5))
        lo, hi = p1, max(p99, p1 + 1.0)
        if hi - lo < 8.0:
            # nearly flat (e.g. mostly zeros): widen to the real extremes, sqrt curve so sparse pixels show
            hi = max(float(np.percentile(a, 99.99)), float(a.max()) * 0.25, lo + 8.0)
            img = (np.sqrt(np.clip((a - lo) / (hi - lo), 0, 1)) * 255.0).astype(np.uint8)
        else:
            img = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
        h, w = img.shape[:2]
        scale = 1000.0 / max(h, w)
        if scale < 1.0:
            k = int(np.ceil(1.0 / scale))
            img = cv2.dilate(img, np.ones((k, k), np.uint8))  # max-pool so lone hot pixels survive the downscale
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_NEAREST)
        ok, buf = cv2.imencode(".png", img)
        n = a.size
        warnings = []
        if med > (16384 if mono16 else 150):
            warnings.append("dark median is too bright (light leak / not dark), and will be rejected")
        gm = re.search(r"gain(\d+)dB", name)
        gain = float(gm.group(1)) if gm else 0.0
        info = []
        if not mono16 and med < 20:
            if gain >= 21:
                info.append("High gain: the sensor black pedestal (36 DN @12 dB, 13 @18 dB) falls to 0 from ~21 dB and clips, so this dark holds only hot pixels. Expected.")
            else:
                warnings.append("median < 20 DN at low gain: black level should still be ~13-36 DN, dark is suspect")
        if float((a >= saturated_dn).sum()) / n > 0.001:
            warnings.append(f"more than 0.1% saturated pixels ({saturated_dn})")
        stats = {
            "filename": name, "shape": [int(h), int(w)], "dtype": str(arr.dtype),
            "min": float(a.min()), "max": float(a.max()), "mean": round(float(a.mean()), 2),
            "median": round(med, 2), "std": round(float(a.std()), 2),
            "zero_pct": round(float((a == 0).sum()) / n * 100, 2),
            "saturated_pct": round(float((a >= saturated_dn).sum()) / n * 100, 3),
            "stretch": [round(lo, 1), round(hi, 1)], "warnings": warnings, "info": info,
        }
        return (buf.tobytes() if ok else b""), stats

    def _list_calibration_files(self) -> List[Dict[str, Any]]:
        """List saved native RAW master dark files sorted newest first."""
        files_info: List[Dict[str, Any]] = []
        is_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        try:
            if not os.path.isdir(self.calibration_dir):
                return files_info
            for fname in os.listdir(self.calibration_dir):
                if (
                    fname.startswith(("master_dark_raw10_", "master_dark_mono16_"))
                    and fname.endswith(".npy")
                ):
                    pass
                elif is_pytest and fname.startswith("master_dark_") and fname.endswith((".webp", ".png")):
                    pass
                else:
                    continue
                fpath = os.path.join(self.calibration_dir, fname)
                st = os.stat(fpath)
                files_info.append({
                    "filename": fname,
                    "file_path": fpath,
                    "size_bytes": st.st_size,
                    "size_kb": round(st.st_size / 1024.0, 1),
                    "modified_at": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(),
                    "_mtime": st.st_mtime,
                })
            files_info.sort(key=lambda x: x["_mtime"], reverse=True)
            for item in files_info:
                item.pop("_mtime", None)
        except Exception:
            pass
        return files_info

    def get_calibration_status(self) -> Dict[str, Any]:
        """Return live calibration progress, preset matrix, latest result, and saved calibration library (lock-free)."""
        status_copy = dict(self.calibration_status)
        status_copy["native_format"] = self._native_dark_format()
        status_copy["native_format_label"] = self._native_dark_label()
        status_copy["library"] = self._list_calibration_files()
        status_copy["profiles"] = self.get_profiles_matrix()
        status_copy["ready_profiles_count"] = sum(1 for p in status_copy["profiles"] if p["ready"])
        status_copy["total_profiles_count"] = len(status_copy["profiles"])
        return status_copy

    def _apply_closest_master_dark(
        self, frame: np.ndarray, exp_s: float, gain_db: float,
        bayer10: Optional[np.ndarray] = None, mono16: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, Optional[str]]:
        """
        Find and subtract the closest matching Master Dark from data/calibration/darks/,
        and repair all hot pixels recorded in the matching hot_pixels_*.npy map.
        When bayer10 is present, performs linear 10-bit Bayer RAW-to-RAW subtraction
        (Light_RAW10 - Dark_RAW10) and Bayer sub-plane hot-pixel repair BEFORE demosaicing.
        """
        try:
            ready_profiles = [p for p in self.get_profiles_matrix() if p["ready"]]
            if not ready_profiles:
                return frame, None
            best = min(
                ready_profiles,
                key=lambda p: abs(p["exposure_s"] - exp_s) + abs(p["gain_db"] - gain_db) / 12.0,
            )

            hot_key = f"hot_pixels_exp{best['exposure_s']:.1f}s_gain{best['gain_db']:.0f}dB.npy"
            hot_mask = self._dark_cache.get(hot_key)
            if hot_mask is None:
                hot_path = os.path.join(self.calibration_dir, hot_key)
                if os.path.isfile(hot_path):
                    hot_mask = np.load(hot_path)
                    self._dark_cache[hot_key] = hot_mask

            # 1. Native 16-bit mono subtraction and repair before display conversion
            if mono16 is not None:
                mono_fname = self._native_dark_filename(
                    "mono16", best["exposure_s"], best["gain_db"]
                )
                master_mono16 = self._dark_cache.get(mono_fname)
                if master_mono16 is None:
                    mono_path = os.path.join(self.calibration_dir, mono_fname)
                    if os.path.isfile(mono_path):
                        master_mono16 = np.load(mono_path)
                        self._dark_cache[mono_fname] = master_mono16
                if master_mono16 is not None and master_mono16.shape == mono16.shape:
                    # A covered full-scale mono sensor should stay well below 25% of range.
                    if float(np.median(master_mono16[::16, ::16])) <= 16384.0:
                        calibrated_mono16 = calibrate_mono16(mono16, master_mono16, hot_mask)
                        return raw16_to_bgr8(calibrated_mono16), mono_fname

            # 2. True 10-bit Bayer RAW-to-RAW dark subtraction BEFORE demosaicing
            if bayer10 is not None:
                raw_fname = f"master_dark_raw10_exp{best['exposure_s']:.1f}s_gain{best['gain_db']:.0f}dB.npy"
                master_raw10 = self._dark_cache.get(raw_fname)
                if master_raw10 is None:
                    raw_path = os.path.join(self.calibration_dir, raw_fname)
                    if os.path.isfile(raw_path):
                        master_raw10 = np.load(raw_path)
                        self._dark_cache[raw_fname] = master_raw10
                if master_raw10 is not None and master_raw10.shape == bayer10.shape:
                    # Guard against light-contaminated dark frames (valid 10-bit darks have median <= 80 DN)
                    valid_dark = master_raw10 if float(np.median(master_raw10[::16, ::16])) <= 150.0 else None
                    calibrated_bgr8 = demosaic_raw10_bayer(
                        bayer10,
                        master_dark_bayer10=valid_dark,
                        hot_pixel_mask=hot_mask if valid_dark is not None else None,
                    )
                    return calibrated_bgr8, (raw_fname if valid_dark is not None else None)

            # 3. Fallback image-space subtraction (used in simulator/unit tests)
            fname = best["filename"]
            master = self._dark_cache.get(fname)
            if master is None:
                fpath = os.path.join(self.calibration_dir, fname)
                if os.path.isfile(fpath):
                    master = cv2.imread(fpath, cv2.IMREAD_COLOR)
                    if master is not None:
                        self._dark_cache[fname] = master
            if master is not None and master.shape == frame.shape:
                pedestal = int(np.median(master))
                dark_excess = np.maximum(0, master.astype(np.int16) - pedestal)
                calibrated = np.clip(frame.astype(np.int16) - dark_excess, 0, 255).astype(np.uint8)

                if hot_mask is not None and hot_mask.shape == calibrated.shape[:2] and np.any(hot_mask):
                    med3 = cv2.medianBlur(calibrated, 3)
                    calibrated[hot_mask] = med3[hot_mask]

                return calibrated, fname
        except Exception:
            pass
        return frame, None

    def trigger_master_dark(self, num_frames: int, exposure_s: float, gain_db: float) -> Dict[str, Any]:
        """
        Capture N dark frames at specified exposure & gain, generate a native RAW master dark,
        detect hot pixels in the native plane, and save the lossless master as .npy.
        """
        native_label = self._native_dark_label()
        prev_exp = getattr(self.camera, "current_exposure_s", 0.2)
        prev_gain = getattr(self.camera, "current_gain_db", 12.0)

        self.calibration_status.update({
            "state": "running",
            "current_frame": 0,
            "total_frames": num_frames,
            "progress_pct": 5.0,
            "exposure_s": exposure_s,
            "gain_db": gain_db,
            "message": f"Configuring sensor ({exposure_s:.2f}s @ {gain_db:.0f} dB) — preparing {num_frames} frames...",
            "updated_at": datetime.now().astimezone().isoformat(),
        })

        self.activity.info("calibration", f"Master dark started: {num_frames} frames at {exposure_s:.2f}s / {gain_db:.0f} dB")
        try:
            with self.lock, self._lens_covered():
                self.camera.set_hardware_exposure(exposure_s, gain_db)
                gain_db = float(getattr(self.camera, "current_gain_db", gain_db))
                self.calibration_status["gain_db"] = gain_db
                dark_frames: List[np.ndarray] = []
                raw_bayers: List[np.ndarray] = []
                mono16_frames: List[np.ndarray] = []

                def _on_frame_progress(idx: int, total: int, frame_bgr: np.ndarray, meta: Dict[str, Any]):
                    b10 = meta.pop("bayer10", None)
                    if b10 is not None:
                        raw_bayers.append(b10)
                    m16 = meta.pop("mono16", None)
                    if m16 is not None:
                        mono16_frames.append(m16)
                    self.last_frame = frame_bgr
                    self.last_metadata = meta
                    self.last_capture_time = time.time()
                    pct = round(5.0 + (idx / max(1, total)) * 80.0, 1)
                    self.calibration_status.update({
                        "state": "running",
                        "current_frame": idx,
                        "total_frames": total,
                        "progress_pct": pct,
                        "message": f"Capturing {native_label} dark frame {idx} / {total} ({exposure_s:.2f}s @ {gain_db:.1f} dB)...",
                        "updated_at": datetime.now().astimezone().isoformat(),
                    })

                if hasattr(self.camera, "capture_frame_stack") and not bool(os.environ.get("PYTEST_CURRENT_TEST")):
                    try:
                        stack_results = self.camera.capture_frame_stack(num_frames, progress_cb=_on_frame_progress)
                        dark_frames = [f for f, _ in stack_results]
                    except Exception:
                        pass

                while len(dark_frames) < num_frames:
                    idx_next = len(dark_frames) + 1
                    self.camera.set_hardware_exposure(exposure_s, gain_db)
                    frame, meta = self.camera.capture_frame()
                    dark_frames.append(frame)
                    _on_frame_progress(idx_next, num_frames, frame, meta)

                self.calibration_status.update({
                    "state": "running",
                    "current_frame": num_frames,
                    "total_frames": num_frames,
                    "progress_pct": 90.0,
                    "message": f"Median stacking {num_frames} {native_label} frames & extracting hot pixel map...",
                    "updated_at": datetime.now().astimezone().isoformat(),
                })

                raw_filename = f"master_dark_raw10_exp{exposure_s:.1f}s_gain{gain_db:.0f}dB.npy"
                raw_path = os.path.join(self.calibration_dir, raw_filename)
                if mono16_frames:
                    master_mono16 = np.round(np.median(np.stack(mono16_frames, axis=0), axis=0)).astype(np.uint16)
                    filename = self._native_dark_filename("mono16", exposure_s, gain_db)
                    file_path = os.path.join(self.calibration_dir, filename)
                    np.save(file_path, master_mono16)
                    self._dark_cache[filename] = master_mono16
                    pedestal = float(np.median(master_mono16[::16, ::16]))
                    hot_mask = (master_mono16.astype(np.float32) - pedestal) >= 4096.0
                    mean_dn = round(float(np.mean(master_mono16)), 2)
                    std_dn = round(float(np.std(master_mono16)), 2)
                    max_dn = int(np.max(master_mono16))
                    raw_filename = filename
                elif raw_bayers:
                    master_raw10 = np.round(np.median(np.stack(raw_bayers, axis=0), axis=0)).astype(np.uint16)
                    np.save(raw_path, master_raw10)
                    self._dark_cache[raw_filename] = master_raw10
                    ped10 = float(np.median(master_raw10))
                    hot_mask = (master_raw10.astype(np.float32) - ped10) >= 60.0
                    mean_dn = round(float(np.mean(master_raw10)), 2)
                    std_dn = round(float(np.std(master_raw10)), 2)
                    max_dn = int(np.max(master_raw10))
                    filename = raw_filename
                    file_path = raw_path
                    self._cleanup_legacy_dark_images()
                else:
                    master_dark = self.dark_engine.create_master_dark(dark_frames)
                    hot_mask = self.dark_engine.build_hot_pixel_map(master_dark, threshold=100)
                    mean_dn = round(float(np.mean(master_dark)), 2)
                    std_dn = round(float(np.std(master_dark)), 2)
                    max_dn = int(np.max(master_dark))
                    filename = f"master_dark_exp{exposure_s:.1f}s_gain{gain_db:.0f}dB.webp"
                    file_path = os.path.join(self.calibration_dir, filename)
                    if not cv2.imwrite(file_path, master_dark, [cv2.IMWRITE_WEBP_QUALITY, 95]):
                        raise RuntimeError(f"OpenCV could not write {file_path}")
                    self._dark_cache[filename] = master_dark

                hot_pixels = int(np.sum(hot_mask))

                hot_map_filename = f"hot_pixels_exp{exposure_s:.1f}s_gain{gain_db:.0f}dB.npy"
                hot_map_path = os.path.join(self.calibration_dir, hot_map_filename)
                np.save(hot_map_path, hot_mask)

                # Restore previous exposure/gain if in auto mode
                if self.mode == "auto":
                    try:
                        self.camera.set_hardware_exposure(prev_exp, prev_gain)
                    except Exception:
                        pass

                # Cache in memory for fast live dark subtraction
                self._dark_cache[hot_map_filename] = hot_mask

                completed_at = datetime.now().astimezone().isoformat()
                result = {
                    "success": True,
                    "filename": filename,
                    "raw_filename": raw_filename if (raw_bayers or mono16_frames) else None,
                    "file_path": file_path,
                    "hot_map_filename": hot_map_filename,
                    "frames_stacked": num_frames,
                    "exposure_s": exposure_s,
                    "gain_db": gain_db,
                    "hot_pixels_detected": hot_pixels,
                    "mean_dn": mean_dn,
                    "std_dn": std_dn,
                    "max_dn": max_dn,
                    "timestamp": completed_at,
                }

                self.calibration_status.update({
                    "state": "completed",
                    "current_frame": num_frames,
                    "total_frames": num_frames,
                    "progress_pct": 100.0,
                    "message": (
                        f"Completed: {filename} ({num_frames} {native_label} frames stacked, "
                        f"{hot_pixels} hot pixels detected, mean DN {mean_dn})"
                    ),
                    "last_result": result,
                    "updated_at": completed_at,
                })
                self.activity.success(
                    "calibration",
                    f"Master dark saved: {filename} ({num_frames} frames, {hot_pixels} hot pixels, mean {mean_dn} DN)",
                )
                return result
        except Exception as exc:
            self.activity.error("calibration", f"Master dark failed ({exposure_s:.2f}s / {gain_db:.0f} dB): {exc}")
            err_time = datetime.now().astimezone().isoformat()
            self.calibration_status.update({
                "state": "error",
                "progress_pct": 0.0,
                "message": f"Calibration failed: {exc}",
                "updated_at": err_time,
            })
            raise

    def trigger_auto_dark_library(self, only_missing: bool = False) -> Dict[str, Any]:
        """
        Automatically capture & stack all standard Master Dark profiles (or only missing ones)
        in the active camera's native RAW format (.npy). If some profiles are already present on disk, only captures
        the missing profiles unless all are already present.
        """
        matrix = self.get_profiles_matrix()
        missing = [p for p in matrix if not p.get("native_ready")]
        if only_missing:
            targets = missing
        elif 0 < len(missing) < len(matrix):
            targets = missing
        else:
            targets = list(matrix)

        if not targets:
            total = len(matrix)
            self.calibration_status.update({
                "state": "completed",
                "progress_pct": 100.0,
                "message": f"All {total}/{total} Standard {self._native_dark_label()} Master Dark Profiles are already built and ready!",
                "updated_at": datetime.now().astimezone().isoformat(),
            })
            return {
                "success": True,
                "profiles_built": 0,
                "total_profiles": total,
                "results": [],
                "message": f"All {total}/{total} Standard {self._native_dark_label()} Master Dark Profiles Ready.",
            }

        prev_exp = getattr(self.camera, "current_exposure_s", 0.2)
        prev_gain = getattr(self.camera, "current_gain_db", 12.0)
        results: List[Dict[str, Any]] = []
        total_targets = len(targets)
        native_label = self._native_dark_label()

        try:
            for p_idx, prof in enumerate(targets, start=1):
                exp_s = float(prof["exposure_s"])
                gain_db = float(prof["gain_db"])
                n_frames = int(prof["num_frames"])
                label = prof["label"]

                base_pct = ((p_idx - 1) / total_targets) * 100.0
                span_pct = 100.0 / total_targets

                self.calibration_status.update({
                    "state": "running",
                    "current_profile": p_idx,
                    "total_profiles": total_targets,
                    "current_frame": 0,
                    "total_frames": n_frames,
                    "exposure_s": exp_s,
                    "gain_db": gain_db,
                    "progress_pct": round(base_pct + 2.0, 1),
                    "message": f"[Profile {p_idx}/{total_targets}: {label} — {exp_s:.1f}s @ {gain_db:.1f}dB] Configuring {native_label} sensor...",
                    "updated_at": datetime.now().astimezone().isoformat(),
                })

                with self.lock, self._lens_covered():
                    self.camera.set_hardware_exposure(exp_s, gain_db)
                    gain_db = float(getattr(self.camera, "current_gain_db", gain_db))
                    self.calibration_status["gain_db"] = gain_db
                    dark_frames: List[np.ndarray] = []
                    raw_bayers: List[np.ndarray] = []
                    mono16_frames: List[np.ndarray] = []

                    def _on_prof_frame(idx: int, total: int, frame_bgr: np.ndarray, meta: Dict[str, Any]):
                        b10 = meta.pop("bayer10", None)
                        if b10 is not None:
                            raw_bayers.append(b10)
                        m16 = meta.pop("mono16", None)
                        if m16 is not None:
                            mono16_frames.append(m16)
                        self.last_frame = frame_bgr
                        self.last_metadata = meta
                        self.last_capture_time = time.time()
                        pct = round(base_pct + (idx / max(1, total)) * (span_pct * 0.85), 1)
                        self.calibration_status.update({
                            "state": "running",
                            "current_profile": p_idx,
                            "total_profiles": total_targets,
                            "current_frame": idx,
                            "total_frames": total,
                            "progress_pct": pct,
                            "message": (
                                f"[Profile {p_idx}/{total_targets}: {label} — {exp_s:.1f}s @ {gain_db:.0f}dB] "
                                f"Capturing {native_label} frame {idx}/{total}..."
                            ),
                            "updated_at": datetime.now().astimezone().isoformat(),
                        })

                    if hasattr(self.camera, "capture_frame_stack") and not bool(os.environ.get("PYTEST_CURRENT_TEST")):
                        try:
                            stack_res = self.camera.capture_frame_stack(n_frames, progress_cb=_on_prof_frame)
                            dark_frames = [f for f, _ in stack_res]
                        except Exception:
                            pass

                    while len(dark_frames) < n_frames:
                        idx_next = len(dark_frames) + 1
                        self.camera.set_hardware_exposure(exp_s, gain_db)
                        frame, meta = self.camera.capture_frame()
                        dark_frames.append(frame)
                        _on_prof_frame(idx_next, n_frames, frame, meta)

                    raw_filename = f"master_dark_raw10_exp{exp_s:.1f}s_gain{gain_db:.0f}dB.npy"
                    raw_path = os.path.join(self.calibration_dir, raw_filename)
                    if mono16_frames:
                        master_mono16 = np.round(np.median(np.stack(mono16_frames, axis=0), axis=0)).astype(np.uint16)
                        filename = self._native_dark_filename("mono16", exp_s, gain_db)
                        file_path = os.path.join(self.calibration_dir, filename)
                        np.save(file_path, master_mono16)
                        self._dark_cache[filename] = master_mono16
                        pedestal = float(np.median(master_mono16[::16, ::16]))
                        hot_mask = (master_mono16.astype(np.float32) - pedestal) >= 4096.0
                        mean_dn = round(float(np.mean(master_mono16)), 2)
                        raw_filename = filename
                    elif raw_bayers:
                        master_raw10 = np.round(np.median(np.stack(raw_bayers, axis=0), axis=0)).astype(np.uint16)
                        np.save(raw_path, master_raw10)
                        self._dark_cache[raw_filename] = master_raw10
                        ped10 = float(np.median(master_raw10))
                        hot_mask = (master_raw10.astype(np.float32) - ped10) >= 60.0
                        mean_dn = round(float(np.mean(master_raw10)), 2)
                        filename = raw_filename
                        file_path = raw_path
                        self._cleanup_legacy_dark_images()
                    else:
                        master_dark = self.dark_engine.create_master_dark(dark_frames)
                        hot_mask = self.dark_engine.build_hot_pixel_map(master_dark, threshold=100)
                        mean_dn = round(float(np.mean(master_dark)), 2)
                        filename = f"master_dark_exp{exp_s:.1f}s_gain{gain_db:.0f}dB.webp"
                        file_path = os.path.join(self.calibration_dir, filename)
                        if not cv2.imwrite(file_path, master_dark, [cv2.IMWRITE_WEBP_QUALITY, 95]):
                            raise RuntimeError(f"OpenCV could not write {file_path}")
                        self._dark_cache[filename] = master_dark

                    hot_pixels = int(np.sum(hot_mask))

                    hot_map_filename = f"hot_pixels_exp{exp_s:.1f}s_gain{gain_db:.0f}dB.npy"
                    np.save(os.path.join(self.calibration_dir, hot_map_filename), hot_mask)
                    self._dark_cache[hot_map_filename] = hot_mask

                    item_res = {
                        "profile_id": prof["id"],
                        "label": label,
                        "filename": filename,
                        "raw_filename": raw_filename if (raw_bayers or mono16_frames) else None,
                        "frames_stacked": n_frames,
                        "exposure_s": exp_s,
                        "gain_db": gain_db,
                        "hot_pixels_detected": hot_pixels,
                        "mean_dn": mean_dn,
                    }
                    results.append(item_res)
                    self.calibration_status["last_result"] = {
                        "success": True,
                        **item_res,
                        "file_path": file_path,
                        "timestamp": datetime.now().astimezone().isoformat(),
                    }

            if self.mode == "auto":
                with self.lock:
                    try:
                        self.camera.set_hardware_exposure(prev_exp, prev_gain)
                    except Exception:
                        pass

            total_all = len(self.STANDARD_DARK_PROFILES)
            done_msg = (
                f"Auto-Calibration Complete: {len(results)} profile(s) built — "
                f"All {total_all}/{total_all} {native_label} Master Dark Profiles Ready for automatic native subtraction!"
            )
            self.calibration_status.update({
                "state": "completed",
                "current_profile": total_targets,
                "total_profiles": total_targets,
                "progress_pct": 100.0,
                "message": done_msg,
                "updated_at": datetime.now().astimezone().isoformat(),
            })
            self.activity.success("calibration", done_msg)
            return {
                "success": True,
                "profiles_built": len(results),
                "total_profiles": total_all,
                "results": results,
                "message": done_msg,
            }
        except Exception as exc:
            self.activity.error("calibration", f"Dark library build failed: {exc}")
            self.calibration_status.update({
                "state": "error",
                "message": f"Auto-library build failed: {exc}",
                "updated_at": datetime.now().astimezone().isoformat(),
            })
            raise
