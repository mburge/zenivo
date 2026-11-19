"""
ADS-B flights for sky events: who was that aircraft?

A light background thread reads the SBS-1 (BaseStation, port 30003) feed of a local ADS-B receiver
(dump1090 / readsb / Flightradar24 feeder), keeping the last few minutes of positions per aircraft. When a
plane (or would-be meteor) event is recorded, each aircraft's track over the event's time span is turned
into az/alt from the station and projected into the frame with the star-solved camera pointing; the
aircraft that lands on the event's streak / dot track names the flight.
"""

import json
import math
import os
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

FT = 0.3048
WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def _ecef(lat_deg: float, lon_deg: float, h_m: float) -> np.ndarray:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    return np.array([(n + h_m) * math.cos(lat) * math.cos(lon),
                     (n + h_m) * math.cos(lat) * math.sin(lon),
                     (n * (1.0 - WGS84_E2) + h_m) * math.sin(lat)])


def enu_from_station(lat0: float, lon0: float, h0: float, lat: float, lon: float, h: float) -> np.ndarray:
    """East/North/Up metres of a point as seen from the station."""
    d = _ecef(lat, lon, h) - _ecef(lat0, lon0, h0)
    la, lo = math.radians(lat0), math.radians(lon0)
    e = -math.sin(lo) * d[0] + math.cos(lo) * d[1]
    n = -math.sin(la) * math.cos(lo) * d[0] - math.sin(la) * math.sin(lo) * d[1] + math.cos(la) * d[2]
    u = math.cos(la) * math.cos(lo) * d[0] + math.cos(la) * math.sin(lo) * d[1] + math.sin(la) * d[2]
    return np.array([e, n, u])


def pointing_rotation(az_deg: float, alt_deg: float, roll_deg: float) -> np.ndarray:
    """Camera -> ENU rotation (columns: image right, image down, optical axis) from the solved pointing,
    the inverse of bright_star_solver._pointing_result."""
    alt, az = math.radians(alt_deg), math.radians(az_deg)
    axis = np.array([math.cos(alt) * math.sin(az), math.cos(alt) * math.cos(az), math.sin(alt)])
    zen = np.array([0.0, 0.0, 1.0])
    vert = zen - axis * (zen @ axis)
    vert /= np.linalg.norm(vert) + 1e-12
    r = math.radians(roll_deg)
    up = math.cos(r) * vert + math.sin(r) * np.cross(axis, vert)
    y = -up
    return np.column_stack([np.cross(y, axis), y, axis])


class AircraftStore:
    """Per-aircraft position store shared by the receiver feed and the public-network feed: recent fixes,
    a thinned path for the 3D map, tracks for flight matching and dead-reckoned current positions."""

    KEEP_S = 300.0       # seconds of positions kept per aircraft
    TRAIL_KEEP_S = 900.0  # seconds of thinned path kept per aircraft for the 3D map
    TRAIL_STEP_S = 4.0
    MAX_AIRCRAFT = 400

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ac: Dict[str, Dict[str, Any]] = {}

    def _entry(self, icao: str, now: float) -> Optional[Dict[str, Any]]:
        """The record of `icao` (created when new, None when the store is full); caller holds the lock."""
        ac = self._ac.get(icao)
        if ac is None:
            if len(self._ac) >= self.MAX_AIRCRAFT:
                return None
            ac = self._ac[icao] = {"icao": icao, "callsign": "", "pos": deque(maxlen=400), "seen": now,
                                   "trail": deque(maxlen=int(self.TRAIL_KEEP_S / self.TRAIL_STEP_S) + 8)}
        ac["seen"] = now
        return ac

    def _add_fix(self, ac: Dict[str, Any], t: float, lat: float, lon: float, alt_m: float) -> None:
        ac["pos"].append((t, lat, lon, alt_m))
        if not ac["trail"] or t - ac["trail"][-1][0] >= self.TRAIL_STEP_S:
            ac["trail"].append((t, lat, lon, alt_m))

    def save_trails(self, path: str) -> None:
        """Write the recent paths (e.g. on shutdown) so the 3D map has history again right after a restart."""
        with self._lock:
            data = {icao: {"callsign": a["callsign"], "trail": [[round(t, 1), lat, lon, round(alt)] for t, lat, lon, alt in a["trail"]]}
                    for icao, a in self._ac.items() if a["trail"]}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ts": round(time.time(), 1), "aircraft": data}, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass

    def load_trails(self, path: str) -> int:
        """Restore paths saved by save_trails that are still within TRAIL_KEEP_S; returns how many aircraft."""
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return 0
        now = time.time()
        n = 0
        with self._lock:
            for icao, a in (d.get("aircraft") or {}).items():
                pts = [tuple(p) for p in a.get("trail") or [] if now - p[0] <= self.TRAIL_KEEP_S]
                if not pts or icao in self._ac:
                    continue
                trail = deque(pts, maxlen=int(self.TRAIL_KEEP_S / self.TRAIL_STEP_S) + 8)
                self._ac[icao] = {"icao": icao, "callsign": a.get("callsign") or "", "pos": deque(maxlen=400),
                                  "seen": pts[-1][0], "trail": trail}
                n += 1
        return n

    def _prune(self, now: float) -> None:
        with self._lock:
            for icao in [k for k, a in self._ac.items() if now - a["seen"] > self.KEEP_S]:
                del self._ac[icao]
            for a in self._ac.values():
                q: Deque = a["pos"]
                while q and now - q[0][0] > self.KEEP_S:
                    q.popleft()
                q = a["trail"]
                while q and now - q[0][0] > self.TRAIL_KEEP_S:
                    q.popleft()

    def tracks(self, t0: float, t1: float, margin_s: float = 30.0) -> List[Dict[str, Any]]:
        """Aircraft with positions around [t0, t1]: [{"icao", "callsign", "pos": [(t, lat, lon, alt_m)]}]."""
        out = []
        with self._lock:
            for a in self._ac.values():
                pts = [p for p in a["pos"] if t0 - margin_s <= p[0] <= t1 + margin_s]
                if pts:
                    out.append({"icao": a["icao"], "callsign": a["callsign"], "pos": pts,
                                **{k: a[k] for k in ("speed_kt", "track_deg", "vrate_fpm") if k in a}})
        return out

    def latest_fixes(self, max_age_s: float = 30.0, at: Optional[float] = None) -> List[Dict[str, Any]]:
        """Last reported (not dead-reckoned) fix of every aircraft newer than max_age_s, freshest first:
        {icao, callsign, t, lat, lon, alt_m, [speed_kt, track_deg, vrate_fpm]}."""
        now = time.time() if at is None else at
        out = []
        with self._lock:
            for a in self._ac.values():
                if a["pos"] and now - a["pos"][-1][0] <= max_age_s:
                    t, lat, lon, alt = a["pos"][-1]
                    out.append({"icao": a["icao"], "callsign": a["callsign"], "t": t, "lat": lat, "lon": lon,
                                "alt_m": alt, **{k: a[k] for k in ("speed_kt", "track_deg", "vrate_fpm") if k in a}})
        out.sort(key=lambda r: -r["t"])
        return out

    def current(self, max_age_s: float = 60.0, trail_s: float = 0.0, at: Optional[float] = None) -> List[Dict[str, Any]]:
        """Latest state of every aircraft with a position in the last max_age_s seconds; with trail_s, also
        its recent path as [[lat, lon, alt_m, age_s], ...] (oldest first, one point per TRAIL_STEP_S, ending
        at the latest fix).  When `at` precedes the newest fix, interpolate the stored fixes instead of
        accidentally returning that future fix."""
        now = time.time() if at is None else at
        out = []
        with self._lock:
            for a in self._ac.values():
                positions = list(a["pos"])
                if not positions:
                    continue
                latest_t = positions[-1][0]
                historical = at is not None and now < latest_t
                if historical:
                    fix = _interp(positions, now)
                    if fix is None:
                        continue
                    lat, lon, alt = fix
                    t = now
                elif now - latest_t <= max_age_s:
                    t, lat, lon, alt = positions[-1]
                    fix = (lat, lon, alt)
                    lat, lon, alt = _dead_reckon(a, lat, lon, alt, now - t)
                else:
                    continue
                rec = {"icao": a["icao"], "callsign": a["callsign"], "lat": lat, "lon": lon,
                       "altitude_m": alt, "age_s": max(0.0, now - t),
                       **{k: a[k] for k in ("speed_kt", "track_deg", "vrate_fpm") if k in a}}
                if trail_s > 0:
                    if historical:
                        trail = [[round(plat, 5), round(plon, 5), round(palt), round(now - pt, 1)]
                                 for pt, plat, plon, palt in a["trail"]
                                 if 0.0 <= now - pt <= trail_s]
                        trail.append([round(lat, 5), round(lon, 5), round(alt), 0.0])
                    else:
                        trail = [[round(plat, 5), round(plon, 5), round(palt), round(now - pt, 1)]
                                 for pt, plat, plon, palt in a["trail"]
                                 if now - pt <= trail_s and (pt < t or (pt - t <= 0.1 and (plat != lat or plon != lon)))]
                        trail.append([round(fix[0], 5), round(fix[1], 5), round(fix[2]), round(now - t, 1)])
                        if fix != (lat, lon, alt):
                            trail.append([round(lat, 5), round(lon, 5), round(alt), 0.0])
                    rec["trail"] = trail
                out.append(rec)
        return out



class AdsbFeed(AircraftStore):
    """SBS-1 reader. Connects only while wanted() is true (night with aircraft detection on)."""

    def __init__(self, host: str, port: int = 30003, wanted: Optional[Callable[[], bool]] = None):
        super().__init__()
        self.host, self.port = host, int(port)
        self.wanted = wanted or (lambda: True)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_message_ts = 0.0
        self.messages = 0

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="adsb-feed", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.wanted():
                with self._lock:
                    self._ac.clear()
                self._stop.wait(5.0)
                continue
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as s:
                    s.settimeout(30.0)
                    self.connected, self.last_error = True, None
                    self._read(s)
            except OSError as exc:
                self.last_error = str(exc)
            self.connected = False
            self._stop.wait(20.0)

    def _read(self, s: socket.socket) -> None:
        buf = b""
        next_check = time.time() + 30.0
        while not self._stop.is_set():
            data = s.recv(65536)
            if not data:
                return
            buf += data
            *lines, buf = buf.split(b"\n")
            now = time.time()
            for ln in lines:
                # Only positions (MSG,3), velocities (MSG,4) and identities (MSG,1) matter; skip the rest cheaply
                if ln.startswith(b"MSG,3,") or ln.startswith(b"MSG,4,") or ln.startswith(b"MSG,1,"):
                    self._parse(ln, now)
            if now >= next_check:
                next_check = now + 30.0
                self._prune(now)
                if not self.wanted():
                    return

    def _parse(self, ln: bytes, now: float) -> None:
        f = ln.decode("ascii", "replace").rstrip("\r").split(",")
        if len(f) < 16:
            return
        icao = f[4].strip().upper()
        if not icao:
            return
        self.messages += 1
        self.last_message_ts = now
        with self._lock:
            ac = self._entry(icao, now)
            if ac is None:
                return
            if f[1] == "1":
                if f[10].strip():
                    ac["callsign"] = f[10].strip()
                return
            if f[1] == "4":
                try:
                    ac["speed_kt"] = float(f[12])
                    ac["track_deg"] = float(f[13])
                except ValueError:
                    pass
                if len(f) > 16 and f[16].strip():
                    try:
                        ac["vrate_fpm"] = float(f[16])
                    except ValueError:
                        pass
                return
            try:
                alt_ft, lat, lon = float(f[11]), float(f[14]), float(f[15])
            except ValueError:
                return
            self._add_fix(ac, now, lat, lon, alt_ft * FT)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._ac)
            with_pos = sum(1 for a in self._ac.values() if a["pos"])
        return {"host": self.host, "port": self.port, "connected": self.connected, "aircraft": n,
                "with_position": with_pos, "messages": self.messages, "last_error": self.last_error,
                "last_message_age_s": round(time.time() - self.last_message_ts, 1) if self.last_message_ts else None}


def _dead_reckon(a: Dict[str, Any], lat: float, lon: float, alt: float, dt: float,
                 max_dt: float = 30.0) -> Tuple[float, float, float]:
    """Advance the last ADS-B fix by its age along track/speed/vertical rate (fixes are seconds old and a
    jet covers ~250 m/s, which at a few km range is many degrees on the sky)."""
    dt = min(max(dt, 0.0), max_dt)
    spd, trk = a.get("speed_kt"), a.get("track_deg")
    if dt <= 0.0 or spd is None or trk is None:
        return lat, lon, alt
    d = float(spd) * 0.514444 * dt
    lat += d * math.cos(math.radians(trk)) / 111320.0
    lon += d * math.sin(math.radians(trk)) / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    if a.get("vrate_fpm") is not None:
        alt += float(a["vrate_fpm"]) * 0.00508 * dt
    return lat, lon, alt


def _project_cam_to_px(cam: np.ndarray, solution: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Project unit camera-frame vector to image pixel (x, y) for rectilinear or equidistant fisheye."""
    cx, cy = solution["optical_center_px"]
    f = float(solution["focal_px"])
    proj = solution.get("projection", "rectilinear")
    if proj == "equidistant":
        theta = math.acos(max(-1.0, min(1.0, float(cam[2]))))
        if theta > math.radians(110.0):
            return None
        k = float(solution.get("distortion_k") or 0.0)
        rho = math.hypot(float(cam[0]), float(cam[1]))
        if rho < 1e-12:
            return float(cx), float(cy)
        r = f * (theta + k * theta ** 3)
        return float(cx + r * cam[0] / rho), float(cy + r * cam[1] / rho)
    if cam[2] <= 0.05:
        return None
    xn, yn = float(cam[0] / cam[2]), float(cam[1] / cam[2])
    k = float(solution.get("distortion_k") or 0.0)
    s = 1.0 + k * (xn * xn + yn * yn)  # radial distortion r = f*t*(1 + k*t^2), as in bright_star_solver
    return float(f * xn * s + cx), float(f * yn * s + cy)


def frame_to_azalt(x_frac: float, y_frac: float, solution: Dict[str, Any]) -> Tuple[float, float]:
    """Sky direction (az, alt degrees) of a frame point (fractions), inverting the star-solved pointing."""
    cx, cy = solution["optical_center_px"]
    f = float(solution["focal_px"])
    R = pointing_rotation(solution["pointing_az_deg"], solution["pointing_alt_deg"], solution.get("roll_deg", 0.0))
    dx, dy = (x_frac * 2 * cx - cx) / f, (y_frac * 2 * cy - cy) / f
    if solution.get("projection", "rectilinear") == "equidistant":
        theta = math.hypot(dx, dy)
        k = float(solution.get("distortion_k") or 0.0)
        if k != 0.0:
            for _ in range(3):
                theta -= (theta + k * theta ** 3 - math.hypot(dx, dy)) / max(1e-6, 1.0 + 3.0 * k * theta ** 2)
        phi = math.atan2(dy, dx)
        v = np.array([math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)])
    else:
        k = float(solution.get("distortion_k") or 0.0)
        if k != 0.0:
            rd = math.hypot(dx, dy)
            t = rd
            for _ in range(10):
                t -= (t + k * t ** 3 - rd) / max(1e-3, 1.0 + 3.0 * k * t ** 2)
            if rd > 1e-12:
                dx, dy = dx * t / rd, dy * t / rd
        v = np.array([dx, dy, 1.0])
        v = v / np.linalg.norm(v)
    e = R @ v
    return round(math.degrees(math.atan2(e[0], e[1])) % 360.0, 1), round(math.degrees(math.asin(np.clip(e[2], -1, 1))), 1)


def motion_fields(a: Dict[str, Any]) -> Dict[str, Any]:
    """Speed (km/h), heading and climb rate (m/min) in friendly units, from SBS velocity fields."""
    out: Dict[str, Any] = {}
    if a.get("speed_kt") is not None:
        out["speed_kmh"] = round(a["speed_kt"] * 1.852)
    if a.get("track_deg") is not None:
        out["heading_deg"] = round(a["track_deg"]) % 360
    if a.get("vrate_fpm") is not None:
        out["climb_m_min"] = round(a["vrate_fpm"] * FT)
    return out


def sky_position(station: Dict[str, float], lat: float, lon: float, alt_m: float,
                 solution: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Az/alt/distance of an aircraft from the station, plus its pixel (frame fractions) when a pointing
    solution is given and it lies in front of the camera."""
    enu = enu_from_station(station["latitude_deg"], station["longitude_deg"], float(station.get("elevation_m") or 0.0),
                           lat, lon, alt_m)
    dist = float(np.linalg.norm(enu))
    out: Dict[str, Any] = {
        "az_deg": round(math.degrees(math.atan2(enu[0], enu[1])) % 360.0, 1),
        "alt_deg": round(math.degrees(math.asin(enu[2] / max(dist, 1.0))), 1),
        "distance_km": round(dist / 1000.0, 1),
        "ground_km": round(math.hypot(enu[0], enu[1]) / 1000.0, 1),
    }
    xy = frame_xy_from_enu(enu, solution)
    if xy:
        out["frame_xy"] = xy
    return out


def frame_xy_from_enu(enu: np.ndarray, solution: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    """Frame fractions of a direction (East/North/Up) through the solved camera pointing, or None when it
    falls behind or outside the frame."""
    dist = float(np.linalg.norm(enu))
    if solution and solution.get("projection", "rectilinear") in ("rectilinear", "equidistant") and dist > 0:
        cx, cy = solution["optical_center_px"]
        R = pointing_rotation(solution["pointing_az_deg"], solution["pointing_alt_deg"], solution.get("roll_deg", 0.0))
        cam = R.T @ (enu / dist)
        px_py = _project_cam_to_px(cam, solution)
        if px_py is not None:
            x, y = px_py
            if 0 <= x < 2 * cx and 0 <= y < 2 * cy:
                return [round(x / (2 * cx), 4), round(y / (2 * cy), 4)]
    return None


def _interp(pos: List[Tuple[float, float, float, float]], t: float, max_extrap_s: float = 15.0) -> Optional[Tuple[float, float, float]]:
    """Aircraft (lat, lon, alt_m) at time t, interpolating (or briefly extrapolating) its positions."""
    if not pos:
        return None
    if len(pos) == 1:
        p = pos[0]
        return (p[1], p[2], p[3]) if abs(p[0] - t) <= 3.0 else None
    for a, b in zip(pos, pos[1:]):
        if a[0] <= t <= b[0]:
            k = (t - a[0]) / max(1e-3, b[0] - a[0])
            return tuple(a[i] + (b[i] - a[i]) * k for i in (1, 2, 3))  # type: ignore[return-value]
    a, b = (pos[0], pos[1]) if t < pos[0][0] else (pos[-2], pos[-1])
    ref = pos[0] if t < pos[0][0] else pos[-1]
    if abs(t - ref[0]) > max_extrap_s or b[0] - a[0] < 0.5:
        return None
    k = (t - a[0]) / (b[0] - a[0])
    return tuple(a[i] + (b[i] - a[i]) * k for i in (1, 2, 3))  # type: ignore[return-value]


def _seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    k = float(np.clip((p - a) @ ab / max(1e-9, ab @ ab), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + k * ab)))


def match_flight(event: Dict[str, Any], tracks: List[Dict[str, Any]], station: Dict[str, float],
                 solution: Dict[str, Any], lag_s: float = 0.0, tol_px: Optional[float] = None,
                 check_direction: bool = True) -> Optional[Dict[str, Any]]:
    """The aircraft whose projected track lies on the event's line (frame fractions), or None.

    station: latitude_deg, longitude_deg, elevation_m. solution: the bright-star pointing solution
    (pointing_az_deg, pointing_alt_deg, roll_deg, focal_px, optical_center_px, rectilinear or equidistant)."""
    line = event.get("line")
    if not line or not tracks or not solution or solution.get("projection", "rectilinear") not in ("rectilinear", "equidistant"):
        return None
    cx, cy = solution["optical_center_px"]
    W, H = 2.0 * cx, 2.0 * cy
    R = pointing_rotation(solution["pointing_az_deg"], solution["pointing_alt_deg"], solution.get("roll_deg", 0.0))
    a = np.array([line[0] * W, line[1] * H])
    b = np.array([line[2] * W, line[3] * H])
    t0 = float(event.get("start_ts", event.get("ts"))) - lag_s
    t1 = float(event.get("end_ts", t0 + lag_s)) - lag_s
    ordered = bool(event.get("moving_dot") or int(event.get("frames", 1)) > 1)
    tol = tol_px if tol_px is not None else max(25.0, 0.035 * W)  # pointing good to ~10 px; timing adds the rest
    lat0, lon0, h0 = station["latitude_deg"], station["longitude_deg"], float(station.get("elevation_m") or 0.0)
    ev_dir = (b - a) / (np.linalg.norm(b - a) + 1e-9)
    best = None
    for tr in tracks:
        proj = []
        for t in ((t0, t1) if t1 - t0 > 0.5 else (t0,)):
            p = _interp(tr["pos"], t)
            if p is None:
                break
            enu = enu_from_station(lat0, lon0, h0, *p)
            dist = float(np.linalg.norm(enu))
            cam = R.T @ (enu / dist)
            px_py = _project_cam_to_px(cam, solution)
            if px_py is None:
                break
            proj.append((np.array(px_py), enu, dist, p))
        if not proj or len(proj) < (2 if t1 - t0 > 0.5 else 1):
            continue
        if len(proj) == 2 and ordered:
            err = (np.linalg.norm(proj[0][0] - a) + np.linalg.norm(proj[1][0] - b)) / 2.0
        else:
            err = float(np.mean([_seg_dist(q[0], a, b) for q in proj]))
        # Direction of the aircraft's motion in the image against the streak (sign-free for one-frame streaks)
        q0, q1 = _interp(tr["pos"], t0 - 2.0), _interp(tr["pos"], t0 + 2.0)
        angle = None
        if q0 and q1:
            v = []
            for q in (q0, q1):
                e = enu_from_station(lat0, lon0, h0, *q)
                c = R.T @ (e / np.linalg.norm(e))
                px_py = _project_cam_to_px(c, solution)
                v.append(np.array(px_py) if px_py is not None else None)
            if v[0] is not None and v[1] is not None and np.linalg.norm(v[1] - v[0]) > 0.5:
                d = (v[1] - v[0]) / np.linalg.norm(v[1] - v[0])
                cosv = float(d @ ev_dir)
                angle = math.degrees(math.acos(min(1.0, abs(cosv) if not ordered else max(-1.0, cosv))))
        if check_direction and angle is not None and angle > 30.0:
            continue
        if err <= tol and (best is None or err < best[0]):
            best = (err, tr, proj[-1], angle)
    if best is None:
        return None
    err, tr, (_, enu, dist, p), angle = best
    alt = math.degrees(math.asin(enu[2] / dist))
    az = math.degrees(math.atan2(enu[0], enu[1])) % 360.0
    return {
        "icao": tr["icao"],
        "callsign": tr["callsign"] or None,
        "altitude_m": round(p[2]),
        "distance_km": round(dist / 1000.0, 1),
        "ground_km": round(math.hypot(enu[0], enu[1]) / 1000.0, 1),
        "az_deg": round(az, 1),
        "alt_deg": round(alt, 1),
        "lat": round(p[0], 4),
        "lon": round(p[1], 4),
        "match_px": round(err, 1),
        "match_angle_deg": None if angle is None else round(angle, 1),
        **motion_fields(tr),
    }
