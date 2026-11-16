"""Large, naked-eye satellites (space stations and the CelesTrak "visual" list) for the live overlay.

TLEs come from CelesTrak (refreshed daily, cached on disk so the overlay keeps working offline); positions
are propagated with SGP4, turned into az/alt for the station and projected through the star-solved camera
pointing like aircraft. A satellite can only be seen when the sky is dark and it is lit by the sun (outside
the Earth's shadow).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

from src.net import urlopen as net_urlopen
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from src.analytics.adsb import _ecef, frame_xy_from_enu

GROUPS = ("stations", "visual")  # default groups
GROUP_CHOICES = ("stations", "visual", "weather", "noaa", "gps-ops", "galileo", "science", "starlink-dtc")  # groups offered in Settings
CELESTRAK_GROUP = {"starlink-dtc": "starlink"}  # virtual groups filtered from a larger CelesTrak feed
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
SATNOGS_URL = "https://db.satnogs.org/api/tle/?format=3le"  # one file for every satellite in SatNOGS DB (no groups)
SATNOGS_GROUP = "satnogs"
REFRESH_S = 24 * 3600.0
EARTH_R_KM = 6371.0
WGS84_A_KM = 6378.137
WGS84_E2 = 6.69437999014e-3
MAX_SATS = 2500


def _parse_tle(text: str, group: str) -> List[Dict[str, str]]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if name.startswith("0 "):  # 3le format (SatNOGS) prefixes the name with "0 "
            name = name[2:].strip()
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append({"name": name, "l1": l1, "l2": l2, "group": group})
    return out


def _gmst_rad(jd: float) -> float:
    t = (jd - 2451545.0) / 36525.0
    g = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t ** 3 / 38710000.0
    return math.radians(g % 360.0)


def _sun_eci_dir(jd: float) -> np.ndarray:
    """Unit vector to the sun in the equatorial frame of date (low precision, ~0.01 deg)."""
    n = jd - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360.0)
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    return np.array([math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)])


class SatelliteTracker:
    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._sats: List[Any] = []  # (name, group, Satrec)
        self._fetched_at = 0.0
        self._fetching = False
        self.groups: List[str] = list(GROUPS)
        self.refresh_s = REFRESH_S
        self.url_template = TLE_URL
        self._single = False  # the source serves everything in one file (SatNOGS)
        self.last_error = ""
        self._load_cache()

    def configure(self, groups: Optional[List[str]] = None, refresh_s: Optional[float] = None,
                  url_template: Optional[str] = None) -> None:
        """Apply the user's settings; a change of groups or source forces a fresh download."""
        groups = [g for g in (groups or self.groups) if g in GROUP_CHOICES] or list(GROUPS)
        url = url_template or TLE_URL
        with self._lock:
            changed = groups != self.groups or url != self.url_template
            self.groups, self.url_template = groups, url
            self._single = "{group}" not in url
            if refresh_s:
                self.refresh_s = float(refresh_s)
            # Satellites of a group that was switched off disappear at once; a new group arrives with the next download
            keep = [SATNOGS_GROUP] if self._single else self.groups
            self._sats = [t for t in self._sats if t[1] in keep]
            if changed:
                self._fetched_at = 0.0

    def refresh_now(self) -> bool:
        """Start a download now (False when one is already running)."""
        if self._fetching:
            return False
        self._fetching = True
        threading.Thread(target=self._fetch, daemon=True, name="tle-fetch").start()
        return True

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path) as fh:
                d = json.load(fh)
            self._set_tles(d.get("tles") or [], float(d.get("fetched_at") or 0.0))
        except Exception:
            pass

    def _set_tles(self, tles: List[Dict[str, str]], fetched_at: float) -> None:
        from sgp4.api import Satrec

        sats, seen = [], set()
        for t in tles:
            try:
                sat = Satrec.twoline2rv(t["l1"], t["l2"])
            except Exception:
                continue
            if sat.satnum in seen:
                continue
            seen.add(sat.satnum)
            sats.append((t["name"], t["group"], sat))
        with self._lock:
            self._sats, self._fetched_at = sats[:MAX_SATS], fetched_at

    def _fetch(self) -> None:
        try:
            tles: List[Dict[str, str]] = []
            sources = [(self.url_template, SATNOGS_GROUP)] if self._single else [
                (self.url_template.format(group=CELESTRAK_GROUP.get(g, g)), g) for g in list(self.groups)]
            for url, g in sources:
                with net_urlopen(url, timeout=30) as r:
                    parsed = _parse_tle(r.read().decode("ascii", "replace"), g)
                    if g == "starlink-dtc":
                        # CelesTrak marks the cellular spacecraft in the Starlink feed explicitly,
                        # e.g. "STARLINK-11740 [DTC]".  Keep that exact subset rather than loading
                        # the full (10,000+) constellation onto a small station host.
                        parsed = [t for t in parsed if "[DTC]" in t["name"].upper()]
                    tles += parsed
            if not tles:
                raise ValueError("the source returned no TLE records")
            now = time.time()
            self._set_tles(tles, now)
            self.last_error = ""
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w") as fh:
                json.dump({"fetched_at": now, "tles": tles}, fh)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._fetched_at = max(self._fetched_at, time.time() - self.refresh_s + 1800.0)  # retry in 30 min
        finally:
            self._fetching = False

    def _maybe_refresh(self) -> None:
        if self._fetching or time.time() - self._fetched_at < self.refresh_s:
            return
        self._fetching = True
        threading.Thread(target=self._fetch, daemon=True, name="tle-fetch").start()

    def status(self) -> Dict[str, Any]:
        return {"count": len(self._sats), "fetched_at": self._fetched_at or None, "groups": list(self.groups),
                "refresh_h": round(self.refresh_s / 3600.0, 2), "error": self.last_error, "fetching": self._fetching,
                "source": self.url_template.split("/")[2] if self.url_template.count("/") >= 2 else ""}

    def _context(self, station: Dict[str, float], when: datetime) -> Dict[str, Any]:
        """Earth rotation, observer and sun for one instant."""
        from sgp4.api import jday

        when = when.astimezone(timezone.utc)
        jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute, when.second + when.microsecond / 1e6)
        jd_full = jd + fr
        gmst = _gmst_rad(jd_full)
        cg, sg = math.cos(gmst), math.sin(gmst)
        lat0, lon0 = station["latitude_deg"], station["longitude_deg"]
        la, lo = math.radians(lat0), math.radians(lon0)
        sun = _sun_eci_dir(jd_full)
        sun_ecef = np.array([cg * sun[0] + sg * sun[1], -sg * sun[0] + cg * sun[1], sun[2]])
        up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
        return {"jd": jd, "fr": fr, "cg": cg, "sg": sg, "la": la, "lo": lo, "sun": sun,
                "obs": _ecef(lat0, lon0, float(station.get("elevation_m") or 0.0)) / 1000.0,
                "sun_alt": math.degrees(math.asin(float(np.clip(sun_ecef @ up, -1, 1))))}

    @staticmethod
    def _state(c: Dict[str, Any], sat: Any) -> Optional[Dict[str, Any]]:
        err, r, _v = sat.sgp4(c["jd"], c["fr"])
        if err != 0:
            return None
        r = np.array(r)
        cg, sg, la, lo = c["cg"], c["sg"], c["la"], c["lo"]
        ecef = np.array([cg * r[0] + sg * r[1], -sg * r[0] + cg * r[1], r[2]])  # TEME -> ECEF (no polar motion)
        d = ecef - c["obs"]
        e = -math.sin(lo) * d[0] + math.cos(lo) * d[1]
        n = -math.sin(la) * math.cos(lo) * d[0] - math.sin(la) * math.sin(lo) * d[1] + math.cos(la) * d[2]
        u = math.cos(la) * math.cos(lo) * d[0] + math.cos(la) * math.sin(lo) * d[1] + math.sin(la) * d[2]
        rng = math.sqrt(e * e + n * n + u * u)
        sun = c["sun"]
        along = float(r @ sun)
        sunlit = not (along < 0.0 and float(np.linalg.norm(r - along * sun)) < EARTH_R_KM)
        # geodetic latitude / longitude / height (iterated, WGS84)
        x, y, z = ecef
        p = math.hypot(x, y)
        lat = math.atan2(z, p * (1 - WGS84_E2))
        for _ in range(4):
            nn = WGS84_A_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
            h = p / max(1e-9, math.cos(lat)) - nn
            lat = math.atan2(z, p * (1 - WGS84_E2 * nn / (nn + h)))
        return {"e": e, "n": n, "u": u, "rng": rng, "alt": math.degrees(math.asin(u / rng)), "sunlit": sunlit,
                "lat": math.degrees(lat), "lon": math.degrees(math.atan2(y, x)), "h": h,
                "az": math.degrees(math.atan2(e, n)) % 360.0}

    def current(self, station: Dict[str, float], solution: Optional[Dict[str, Any]] = None,
                when: Optional[datetime] = None, track_min: float = 0.0, track_for: int = 8,
                include_below: bool = False, track_ids: Optional[set] = None, trail_s: float = 0.0) -> Dict[str, Any]:
        """Satellites above the horizon now: az/alt, range, whether sunlit, and frame position. With
        track_min, the first track_for of them (highest first) also get their path from 5 minutes ago to
        track_min minutes ahead as [[lat, lon, height_km, seconds_from_now, alt_deg, visible], ...]."""
        self._maybe_refresh()
        when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        c = self._context(station, when)
        sun_alt = c["sun_alt"]
        out = []
        with self._lock:
            sats = list(self._sats)
        for name, group, sat in sats:
            st = self._state(c, sat)
            if st is None or (st["alt"] < 0.0 and not include_below):
                continue
            rec = {"name": name, "norad": int(sat.satnum), "group": group,
                   "az_deg": round(st["az"], 1), "alt_deg": round(st["alt"], 1),
                   "range_km": round(st["rng"]), "height_km": round(st["h"]),
                   "lat": round(st["lat"], 4), "lon": round(st["lon"], 4),
                   "above_horizon": st["alt"] >= 0.0, "sunlit": st["sunlit"],
                   "visible": bool(st["sunlit"] and sun_alt < -3.0 and st["alt"] >= 0.0)}
            xy = frame_xy_from_enu(np.array([st["e"], st["n"], st["u"]]) * 1000.0, solution)
            if xy:
                rec["frame_xy"] = xy
            out.append(rec)
        out.sort(key=lambda s: (not s.get("frame_xy"), not s["visible"], -s["alt_deg"]))
        if trail_s > 0:
            # Recent path of every listed satellite as [[lat, lon, height_km, seconds_from_now], ...], oldest first
            by_norad = {int(sat.satnum): sat for _n, _g, sat in sats}
            trails: Dict[int, List[List[float]]] = {r["norad"]: [] for r in out}
            for dt in range(-int(trail_s) // 30 * 30, 0, 30):
                ci = self._context(station, when + timedelta(seconds=dt))
                for r in out:
                    st = self._state(ci, by_norad[r["norad"]])
                    if st is not None:
                        trails[r["norad"]].append([round(st["lat"], 3), round(st["lon"], 3), round(st["h"]), dt])
            for r in out:
                r["trail"] = trails[r["norad"]] + [[r["lat"], r["lon"], r["height_km"], 0]]
        if track_min > 0:
            by_norad = {int(sat.satnum): sat for _n, _g, sat in sats}
            wanted = [r for i, r in enumerate(out) if i < track_for or r["norad"] in (track_ids or ())]
            for rec in wanted:
                sat = by_norad[rec["norad"]]
                pts = []
                for dt in range(-300, int(track_min * 60) + 1, 30):
                    ci = self._context(station, when + timedelta(seconds=dt))
                    st = self._state(ci, sat)
                    if st is not None:
                        pts.append([round(st["lat"], 3), round(st["lon"], 3), round(st["h"]), dt, round(st["alt"], 1),
                                    int(st["sunlit"] and ci["sun_alt"] < -3.0 and st["alt"] >= 0.0)])
                rec["track"] = pts
        return {"sun_alt_deg": round(sun_alt, 1), "tle_count": len(sats), "tle_age_h":
                round((time.time() - self._fetched_at) / 3600.0, 1) if self._fetched_at else None,
                "satellites": out}
