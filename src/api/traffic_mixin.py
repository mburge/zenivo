"""Aircraft / satellites / radiosondes / ADS-B and the traffic log.

Mixin of AllSkyService (src/api/service.py): methods only, all state lives on the service instance."""

import math
import os
import re
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
import numpy as np
import cv2
import logging

logger = logging.getLogger("imx335.api")


class TrafficMixin:

    def _fr24(self):
        """Flightradar24 global & Enterprise feed helper (created on first use)."""
        if getattr(self, "fr24", None) is None:
            from src.analytics.fr24 import Fr24Feed
            cfg = self.station_config.get("adsb") or {}
            self.fr24 = Fr24Feed(
                enabled=bool(cfg.get("fr24_enabled", 0)),
                email=str(cfg.get("fr24_email") or ""),
                password=str(cfg.get("fr24_password") or ""),
                token=str(cfg.get("fr24_token") or ""),
            )
        return self.fr24

    def _receiver_status(self) -> Optional[Dict[str, Any]]:
        return self.adsb.status() if self.adsb is not None else None

    def _public_needed(self) -> bool:
        """The public network is polled only when the fallback switch is on and the receiver is not delivering."""
        from src.analytics.adsb_public import receiver_delivering
        return bool((self.station_config.get("adsb") or {}).get("public_fallback", 0)) \
            and not receiver_delivering(self._receiver_status())

    def _source_in_use(self) -> str:
        """'receiver' | 'public' | 'none' (see adsb_public.choose_source)."""
        from src.analytics.adsb_public import choose_source
        on = bool((self.station_config.get("adsb") or {}).get("public_fallback", 0))
        src = choose_source(self._receiver_status(), on)
        return "none" if src == "public" and self.adsb_public is None else src

    def _aircraft_feed(self):
        """The one feed every aircraft consumer reads: the receiver while it delivers, else the public network when
        the fallback is on, else the receiver (silent or None)."""
        return self.adsb_public if self._source_in_use() == "public" else self.adsb

    def _public_status(self) -> Dict[str, Any]:
        pf = self.adsb_public
        if pf is None:
            return {"provider": None, "last_ok": None, "last_error": None, "aircraft": 0, "active": False}
        st = pf.status()
        return {"provider": pf.provider["name"], "link": pf.provider["link"], "last_ok": st["last_ok"],
                "last_error": st["last_error"], "aircraft": st["aircraft"], "active": pf.active()}

    def _contribute_status(self) -> Dict[str, Any]:
        c = getattr(self, "adsb_contrib", None)
        if c is None:
            return {"active": False, "last_ok": None, "last_error": None, "last_sent": 0, "posts": 0}
        return c.status()

    def get_adsb_config(self) -> Dict[str, Any]:
        cfg = self.station_config.get("adsb") or {}
        sol = self.station_config.get("bright_star_solution") or {}
        fr = self._fr24()
        return {
            "enabled": int(bool(cfg.get("enabled", 1))) if cfg.get("host") else 0,
            "host": cfg.get("host") or "",
            "port": int(cfg.get("port") or self.ADSB_DEFAULT_PORT),
            "pointing_solved": bool(sol.get("success")),
            "status": self.adsb.status() if self.adsb is not None else None,
            "public_fallback": int(bool(cfg.get("public_fallback", 0))),
            "public_status": self._public_status(),
            "contribute": int(bool(cfg.get("contribute", 0))),
            "contribute_status": self._contribute_status(),
            "network_host": self._network_base(),
            "source_in_use": self._source_in_use(),
            "radiosondes_enabled": int(bool(self.station_config.get("radiosondes_enabled", 1))),
            "fr24_enabled": int(bool(cfg.get("fr24_enabled", 0))),
            "fr24_email": str(cfg.get("fr24_email") or ""),
            "fr24_has_token": bool(fr.subscription_key),
            "fr24_authenticated": bool(fr.logged_in),
            "fr24": fr.status(),
        }

    def set_adsb_config(self, host: Optional[str] = None, port: Optional[int] = None,
                        enabled: Optional[bool] = None, fr24_enabled: Optional[bool] = None,
                        fr24_email: Optional[str] = None, fr24_password: Optional[str] = None,
                        fr24_token: Optional[str] = None,
                        radiosondes_enabled: Optional[bool] = None,
                        public_fallback: Optional[bool] = None,
                        contribute: Optional[bool] = None) -> Dict[str, Any]:
        if radiosondes_enabled is not None:
            self.station_config["radiosondes_enabled"] = int(bool(radiosondes_enabled))
        cfg = dict(self.station_config.get("adsb") or {})
        if host is not None:
            host = host.strip()
            if host and not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", host):
                raise ValueError("host must be a hostname or IP address")
            cfg["host"] = host
        if port is not None:
            if not 1 <= int(port) <= 65535:
                raise ValueError("port must be 1..65535")
            cfg["port"] = int(port)
        if enabled is not None:
            cfg["enabled"] = int(bool(enabled))
        if public_fallback is not None:
            cfg["public_fallback"] = int(bool(public_fallback))
        if contribute is not None:
            cfg["contribute"] = int(bool(contribute))
        if fr24_enabled is not None:
            cfg["fr24_enabled"] = int(bool(fr24_enabled))
        if fr24_email is not None:
            cfg["fr24_email"] = fr24_email.strip()
        if fr24_token is not None:
            cfg["fr24_token"] = fr24_token.strip()

        fr = self._fr24()
        fr.enabled = bool(cfg.get("fr24_enabled", 0))
        if fr24_token is not None:
            fr.token = cfg["fr24_token"]
            fr.subscription_key = cfg["fr24_token"]
            fr.logged_in = bool(cfg["fr24_token"])
            fr.account_tier = "enterprise" if cfg["fr24_token"] else "public"
            fr.last_error = None
            if not cfg["fr24_token"]:
                fr.password = ""  # "Clear token" also forgets the in-memory password
        fr24_login = None
        if fr24_email and fr24_password and time.time() - getattr(self, "_fr24_login_failed_at", 0.0) < 60.0:
            # One failed sign-in per minute at most: never hammer the Flightradar24 login with a wrong password
            fr24_login = {"ok": False, "message": "the last sign-in failed less than a minute ago, wait and try again"}
        elif fr24_email and fr24_password:
            try:
                fr.login(fr24_email.strip(), fr24_password)
                cfg["fr24_email"] = fr.email
                cfg["fr24_token"] = fr.subscription_key
                fr24_login = {"ok": True}
            except ValueError as exc:
                self._fr24_login_failed_at = time.time()
                fr24_login = {"ok": False, "message": str(exc)}

        self.station_config["adsb"] = cfg
        self._save_station_config()
        self.start_adsb()
        c = self.get_adsb_config()
        if fr24_login is not None:
            c["fr24_login"] = fr24_login
        self.activity.info("events", f"ADS-B flight matching: {c['host']}:{c['port']}" if c["enabled"] else "ADS-B flight matching updated")
        return {"success": True, **c}

    def get_fr24_flight_detail(self, fr24_id: str) -> Dict[str, Any]:
        """Full Flightradar24 flight detail and historical trail from departure airport."""
        return self._fr24().flight_detail(fr24_id)

    def _star_ref(self) -> Optional[Dict[str, Any]]:
        """Solved pointing + station + now, for the star-visibility part of the cloud estimate."""
        sol = self.station_config.get("bright_star_solution") or {}
        if not sol.get("success"):
            return None
        return {"solution": sol, "lat": self.auto_exposure.lat, "lon": self.auto_exposure.lon,
                "when": datetime.now().astimezone()}

    def _lens_circle(self) -> Optional[tuple]:
        """Fisheye image circle (cx, cy, r) as fractions of the frame (width, height, width), from the star solution:
        the 90 degree radius of an equidistant lens. None for other lenses or without a solution."""
        sol = self.station_config.get("bright_star_solution") or {}
        try:
            if not sol.get("success") or sol.get("projection") != "equidistant":
                return None
            cx, cy = (float(v) for v in sol["optical_center_px"])
            fw = float(sol.get("frame_width") or 2.0 * cx)
            fh = float(sol.get("frame_height") or 2.0 * cy)
            r = float(sol["focal_px"]) * 1.5708 * 0.97 / fw   # a little inside, the rim itself is soft
            return (cx / fw, cy / fh, r) if 0.2 < r < 0.8 else None
        except (KeyError, TypeError, ValueError):
            return None

    def _overlay_solution(self) -> Optional[Dict[str, Any]]:
        """Star-solved camera pointing for sky overlays, with the manual boresight correction applied."""
        sol = self.station_config.get("bright_star_solution") or {}
        sol = sol if sol.get("success") else None
        acfg = self.station_config.get("adsb") or {}
        if sol and (acfg.get("az_offset_deg") or acfg.get("alt_offset_deg")):
            # Manual boresight correction when the camera has shifted since the last star solve
            sol = dict(sol, pointing_az_deg=(sol["pointing_az_deg"] + float(acfg.get("az_offset_deg") or 0.0)) % 360.0,
                       pointing_alt_deg=sol["pointing_alt_deg"] + float(acfg.get("alt_offset_deg") or 0.0))
        return sol

    def _overlay_station(self) -> Dict[str, float]:
        return {"latitude_deg": self.auto_exposure.lat, "longitude_deg": self.auto_exposure.lon,
                "elevation_m": float(self.station_config.get("elevation_m") or 0.0)}

    def get_satellites_now(self, track_min: float = 0.0, below: bool = False, limit: int = 60,
                          track_ids: Optional[set] = None, trail_s: float = 0.0) -> Dict[str, Any]:
        """Large satellites above the horizon (stations + CelesTrak visual list) with their camera position."""
        if not self._satellite_settings()["enabled"]:
            return {"satellites": [], "disabled": True, "station": {"lat": self.auto_exposure.lat, "lon": self.auto_exposure.lon}}
        self._sat_tracker_get()
        try:
            res = self._sat_tracker.current(self._overlay_station(), self._overlay_solution(), track_min=track_min,
                                            include_below=below, track_ids=track_ids, trail_s=trail_s)
        except Exception as exc:
            # A missing sgp4 or an unreachable TLE feed must not hide the camera pointing the /sky page needs
            res = {"satellites": [], "error": f"{type(exc).__name__}: {exc}"}
        obstructions = [np.array(p, np.float32) for p in ((self.station_config.get("sky_mask") or {}).get("obstruction") or [])]
        for r in res["satellites"]:
            xy = r.get("frame_xy")
            r["in_view"] = bool(xy) and not any(
                cv2.pointPolygonTest(p, (float(xy[0]), float(xy[1])), False) >= 0 for p in obstructions)
        res["satellites"] = res["satellites"][:limit]
        res["station"] = {"lat": self.auto_exposure.lat, "lon": self.auto_exposure.lon}
        sol = self._overlay_solution()
        if sol:
            res["camera"] = {k: sol.get(k) for k in ("pointing_az_deg", "pointing_alt_deg", "roll_deg", "fov_horizontal_deg", "fov_vertical_deg")}
            # /sky draws the picture as displayed (night orientation): turn the stored native-frame solution into
            # the displayed frame, and convert the roll to the skyview convention (opposite sign to the solver's).
            tf = self.station_config.get("image_transform") or {}
            rot180 = int(tf.get("rotate_deg", 0)) % 360 == 180
            fh = bool(tf.get("flip_horizontal")) != rot180
            fv = bool(tf.get("flip_vertical")) != rot180
            roll = -float(sol.get("roll_deg") or 0.0)
            try:
                # Lens in sensor pixels (station optics), whatever size the star solve ran at; the page maps the picture by proportion
                opt = self.station_config.get("optics") or {}
                sens = self.station_config.get("camera_sensor") or {}
                w, h = float(sens.get("width_px") or 0.0), float(sens.get("height_px") or 0.0)
                if opt.get("focal_length_px") and opt.get("optical_center_x_px") and w and h:
                    f, cx, cy = float(opt["focal_length_px"]), float(opt["optical_center_x_px"]), float(opt["optical_center_y_px"])
                    kd, proj = float(opt.get("distortion_k") or 0.0), str(opt.get("lens_projection") or "equidistant")
                else:
                    cx, cy = (float(v) for v in sol["optical_center_px"])
                    f, kd, proj = float(sol["focal_px"]), float(sol.get("distortion_k") or 0.0), str(sol.get("projection") or "equidistant")
                    w = float(sol.get("frame_width") or 2 * cx)
                    h = float(sol.get("frame_height") or 2 * cy)
                if fh and fv:
                    roll += 180.0
                # optics are pixel indices; the page works in continuous picture coordinates (pixel i covers [i, i+1))
                cx = w - cx - 0.5 if fh else cx + 0.5
                cy = h - cy - 0.5 if fv else cy + 0.5
                res["camera"]["view_roll_deg"] = ((roll + 180.0) % 360.0) - 180.0
                res["camera"]["lens"] = {"f": f, "k": kd, "cx": cx, "cy": cy, "w": w, "h": h, "proj": proj}
            except (KeyError, TypeError, ValueError):
                res["camera"]["view_roll_deg"] = ((roll + 180.0) % 360.0) - 180.0
        return res

    def get_radiosondes_now(self, limit: int = 40) -> Dict[str, Any]:
        """Weather balloons (SondeHub) near the station with their sky position and camera frame position."""
        from src.analytics.adsb import sky_position
        from src.analytics.adsb_public import snap_half_degree
        from src.analytics.radiosonde import RadiosondeFeed

        if getattr(self, "_sonde_feed", None) is None:
            self._sonde_feed = RadiosondeFeed()
        t_now = time.time()
        if not int(self.station_config.get("radiosondes_enabled", 1)):
            return {"t": t_now, "enabled": False, "sondes": [], "total": 0, "in_view": 0, "error": ""}
        station = self._overlay_station()
        sol = self._overlay_solution()
        obstructions = [np.array(p, np.float32) for p in ((self.station_config.get("sky_mask") or {}).get("obstruction") or [])]
        # Only a 0.5-degree grid cell centre (about 55 km) is sent to SondeHub; the exact station stays local (sky_position below).
        q_lat = snap_half_degree(station["latitude_deg"])
        q_lon = snap_half_degree(station["longitude_deg"])
        feed = self._sonde_feed.current(q_lat, q_lon, at=t_now)
        later = {s["serial"]: s for s in self._sonde_feed.current(q_lat, q_lon, at=t_now + 1.0)["sondes"]}
        out = []
        for s in feed["sondes"]:
            pos = sky_position(station, s["lat"], s["lon"], s["alt"], sol)
            xy = pos.get("frame_xy")
            nxt = later.get(s["serial"])
            if xy and nxt:
                xy2 = sky_position(station, nxt["lat"], nxt["lon"], nxt["alt"], sol).get("frame_xy")
                if xy2:
                    pos["frame_v"] = [round(xy2[0] - xy[0], 5), round(xy2[1] - xy[1], 5)]
            in_view = bool(xy) and not any(cv2.pointPolygonTest(p, (float(xy[0]), float(xy[1])), False) >= 0 for p in obstructions)
            out.append({
                "serial": s["serial"], "type": s["type"], "altitude_m": round(s["alt"]),
                "climb_m_s": round(s["vel_v"], 1), "speed_kmh": round(s["vel_h"] * 3.6),
                "heading_deg": round(s["heading"]), "freq_mhz": s["freq"], "temp_c": s["temp"],
                "lat": round(s["lat"], 5), "lon": round(s["lon"], 5), "age_s": s["age_s"], **pos,
                "in_view": in_view, "above_horizon": pos["alt_deg"] > 0,
            })
        out.sort(key=lambda r: (not r["in_view"], not r["above_horizon"], r["distance_km"]))
        return {"t": t_now, "enabled": True, "sondes": out[:limit], "total": len(out), "in_view": sum(r["in_view"] for r in out),
                "error": feed["error"]}

    def get_aircraft_now(self, limit: int = 30, trail_s: float = 0.0,
                         bounds: Optional[str] = None, include_fr24: Optional[bool] = None,
                         compact: bool = False, at: Optional[float] = None) -> Dict[str, Any]:
        """Aircraft heard now, or at a recent frame time from the receiver's position buffer
        (local ADS-B receiver + Flightradar24 global feed when bounds is given):
        flight, altitude, speed, distance, direction, and whether it is inside the camera's open sky."""
        from src.analytics.adsb import motion_fields, sky_position

        fr = self._fr24()
        use_fr24 = bool(bounds) and (include_fr24 if include_fr24 is not None else fr.enabled)
        feed = self._aircraft_feed()
        if feed is None and not use_fr24:
            return {"enabled": False, "connected": False, "aircraft": []}
        feed_source = "public" if feed is not None and feed is self.adsb_public else "local"

        wall_now = time.time()
        self._adsb_view_ts = wall_now  # someone is looking: keep the feed on, day or night
        sol = self._overlay_solution()
        station = self._overlay_station()
        st_lat = float(station["latitude_deg"])
        st_lon = float(station["longitude_deg"])
        cos_st = math.cos(math.radians(st_lat))
        obstructions = [np.array(p, np.float32) for p in ((self.station_config.get("sky_mask") or {}).get("obstruction") or [])]
        out: List[Dict[str, Any]] = []
        by_icao: Dict[str, Dict[str, Any]] = {}
        t_now = wall_now if at is None else float(at)

        if feed is not None:
            # Where each aircraft will be one second later: lets the page slide every mark to the instant of the frame it is shown on
            later = {r["icao"]: r for r in feed.current(at=t_now + 1.0)} if sol else {}
            for a in feed.current(trail_s=trail_s, at=t_now):
                pos = sky_position(station, a["lat"], a["lon"], a["altitude_m"], sol)
                xy = pos.get("frame_xy")
                nxt = later.get(a["icao"])
                if xy and nxt:
                    xy2 = sky_position(station, nxt["lat"], nxt["lon"], nxt["altitude_m"], sol).get("frame_xy")
                    if xy2:
                        pos["frame_v"] = [round(xy2[0] - xy[0], 5), round(xy2[1] - xy[1], 5)]   # frame fractions per second
                in_view = bool(xy) and not any(cv2.pointPolygonTest(p, (float(xy[0]), float(xy[1])), False) >= 0 for p in obstructions)
                rec: Dict[str, Any] = {
                    "icao": a["icao"], "callsign": a["callsign"] or None, "source": feed_source,
                    "altitude_m": round(a["altitude_m"]), **motion_fields(a), **pos,
                    "in_view": in_view, "above_horizon": pos["alt_deg"] > 0, "age_s": round(a["age_s"], 1),
                }
                if feed_source == "public":
                    rec["provider"] = self.adsb_public.provider["name"]   # shown with a link: the data terms require the credit
                if trail_s > 0:
                    rec.update(lat=round(a["lat"], 5), lon=round(a["lon"], 5), trail=a.get("trail") or [],
                               **self._aircraft_info().describe(a["icao"], a["callsign"] or None))
                out.append(rec)
                by_icao[a["icao"]] = rec

        local_count = len(out)
        fr24_count = 0
        global_total = 0

        if use_fr24:
            fr_list, global_total = fr.current(
                bounds=bounds,
                station_lat=st_lat,
                station_lon=st_lon,
                trail_s=trail_s,
            )
            ac_info = self._aircraft_info()
            for f in fr_list:
                icao = f["icao"]
                cs = f.get("callsign") or ""
                icao3 = f.get("airline_icao") or (cs[:3] if len(cs) >= 4 and cs[:3].isalpha() else "")
                al = ac_info._airline(icao3) if icao3 else None
                existing = by_icao.get(icao)
                if existing is not None:
                    existing["source"] = "both"
                    existing["fr24_id"] = f["fr24_id"]
                    if not existing.get("callsign") and cs:
                        existing["callsign"] = cs
                    if not existing.get("type") and f.get("type"):
                        existing["type"] = f["type"]
                    if not existing.get("reg") and f.get("reg"):
                        existing["reg"] = f["reg"]
                    if not existing.get("route") and f.get("route"):
                        existing["route"] = f["route"]
                    if al and not existing.get("airline_iata"):
                        existing["airline_iata"], existing["airline"] = al
                    elif f.get("airline_iata") and not existing.get("airline_iata"):
                        existing["airline_iata"] = f["airline_iata"]
                    continue

                flat = float(f["lat"])
                flon = float(f["lon"])
                falt = float(f["altitude_m"])
                # Fast geodetic filter: aircraft > 450 km away cannot be above the optical horizon
                if abs(flat - st_lat) <= 4.2 and abs(flon - st_lon) <= 4.5:
                    pos = sky_position(station, flat, flon, falt, None)
                    above_hor = pos["alt_deg"] > 0
                else:
                    dlat = flat - st_lat
                    dlon = (flon - st_lon) * cos_st
                    dist_km = round(math.hypot(dlat, dlon) * 111.13, 1)
                    az_deg = round((math.degrees(math.atan2(dlon, dlat)) + 360.0) % 360.0, 1)
                    pos = {"distance_km": dist_km, "az_deg": az_deg, "alt_deg": -5.0}
                    above_hor = False

                rec = {
                    "icao": icao,
                    "fr24_id": f["fr24_id"],
                    "callsign": cs or None,
                    "source": "fr24",
                    "altitude_m": round(falt),
                    "speed_kmh": round(f["speed_kmh"]) if f.get("speed_kmh") is not None else None,
                    "heading_deg": round(f["heading_deg"]) if f.get("heading_deg") is not None else None,
                    "climb_m_min": round(f["climb_m_min"]) if f.get("climb_m_min") is not None else None,
                    **pos,
                    "in_view": False,
                    "above_horizon": above_hor,
                    "age_s": round(f["age_s"], 1),
                    "lat": round(flat, 4),
                    "lon": round(flon, 4),
                    "trail": f.get("trail") or [],
                }
                if f.get("type"):
                    rec["type"] = f["type"]
                if f.get("reg"):
                    rec["reg"] = f["reg"]
                if f.get("route"):
                    rec["route"] = f["route"]
                if al:
                    rec["airline_iata"], rec["airline"] = al
                elif f.get("airline_iata"):
                    rec["airline_iata"] = f["airline_iata"]
                out.append(rec)
                by_icao[icao] = rec
                fr24_count += 1

        out.sort(key=lambda r: (not r["in_view"], r.get("source") == "fr24", not r["above_horizon"], r["distance_km"]))
        if bounds and bounds.strip().lower() in ("global", "world", "all") and len(out) > limit:
            near = [r for r in out if r.get("source") != "fr24" or r["distance_km"] <= 350.0][:160]
            near_ids = {r["icao"] for r in near}
            far = [r for r in out if r["icao"] not in near_ids]
            rem = max(0, limit - len(near))
            if rem > 0 and len(far) > rem:
                step = len(far) / float(rem)
                far = [far[int(i * step)] for i in range(rem)]
            sliced = (near + far)[:limit]
        else:
            sliced = out[:limit]

        compact_rows: List[List[Any]] = []
        if compact:
            full_list: List[Dict[str, Any]] = []
            for r in sliced:
                if r.get("source") != "fr24" or r["distance_km"] <= 350.0 or len(r.get("trail") or []) > 1:
                    full_list.append(r)
                else:
                    compact_rows.append([
                        r["icao"],
                        r.get("fr24_id") or "",
                        r.get("callsign") or "",
                        r.get("type") or "",
                        r.get("airline_iata") or "",
                        r.get("airline") or "",
                        r.get("reg") or "",
                        r.get("route") or "",
                        r["lat"],
                        r["lon"],
                        r["altitude_m"],
                        r.get("speed_kmh"),
                        r.get("heading_deg"),
                        r.get("climb_m_min"),
                        r["distance_km"],
                        r["az_deg"],
                        r.get("age_s", 0.0),
                    ])
            sliced = full_list

        st = feed.status() if feed is not None else {"connected": False}
        res: Dict[str, Any] = {
            "enabled": True,
            "connected": bool(st["connected"] or (use_fr24 and (fr24_count > 0 or global_total > 0))),
            "local_connected": bool(st["connected"]) and feed_source == "local",
            "source_in_use": self._source_in_use(),
            "public_provider": self.adsb_public.provider if feed_source == "public" else None,
            "t": t_now,
            "total": len(out),
            "local_count": local_count,
            "fr24_count": fr24_count,
            "global_total": global_total,
            "in_view": sum(1 for r in out if r["in_view"]),
            "aircraft": sliced,
            "fr24": fr.status(),
        }
        if compact_rows:
            res["compact_aircraft"] = compact_rows
        if trail_s > 0 or use_fr24:
            res["station"] = {"lat": station["latitude_deg"], "lon": station["longitude_deg"], "elevation_m": station["elevation_m"]}
            if sol:
                # Lens + frame from the station optics (sensor pixels); the solve may have run at another size
                opt = self.station_config.get("optics") or {}
                sens = self.station_config.get("camera_sensor") or {}
                w, h = float(sens.get("width_px") or 0.0), float(sens.get("height_px") or 0.0)
                if opt.get("focal_length_px") and w and h:
                    f = float(opt["focal_length_px"])
                    proj = str(opt.get("lens_projection") or "equidistant")
                    kd = float(opt.get("distortion_k") or 0.0)
                else:
                    f = float(sol["focal_px"])
                    cx, cy = sol["optical_center_px"]
                    w = float(sol.get("frame_width") or 2 * cx)
                    h = float(sol.get("frame_height") or 2 * cy)
                    proj = str(sol.get("projection") or "rectilinear")
                    kd = float(sol.get("distortion_k") or 0.0)
                res["camera"] = {"az_deg": sol["pointing_az_deg"], "alt_deg": sol["pointing_alt_deg"],
                                 "roll_deg": sol.get("roll_deg", 0.0),
                                 "hfov_deg": sol.get("fov_horizontal_deg"), "vfov_deg": sol.get("fov_vertical_deg"),
                                 "projection": proj, "distortion_k": kd,
                                 "half_w": round(w / 2.0 / f, 5), "half_h": round(h / 2.0 / f, 5)}
        return res

    def _aircraft_info(self):
        """Type / airline / route lookups for the 3D map (created on first use)."""
        if getattr(self, "aircraft_info", None) is None:
            from src.analytics.aircraft_info import AircraftInfo
            self.aircraft_info = AircraftInfo(os.path.join(self.output_dir, "cache"))
        return self.aircraft_info

    def get_aircraft_info(self, icao: str, callsign: str = "") -> Dict[str, Any]:
        """Type, registration, airline and route of one aircraft as far as they are cached; the rest is queued for
        lookup (a client asks again a few seconds later). Nothing here is private, so guests may ask, but only about
        an aircraft the station hears right now: an arbitrary code would make the station query outside services."""
        heard = {str(a.get("icao", "")).upper() for a in self.get_aircraft_now(limit=6000).get("aircraft", [])}
        if icao.upper() not in heard:
            return {}
        return self._aircraft_info().describe(icao, callsign or None)

    def get_airports(self) -> Dict[str, Any]:
        """Airports and runways around the station (OurAirports) for the 3D flights map."""
        if getattr(self, "airports", None) is None:
            from src.analytics.airports import Airports
            self.airports = Airports(os.path.join(self.output_dir, "cache"))
        return self.airports.get(self.auto_exposure.lat, self.auto_exposure.lon)

    def get_airports_frame(self, max_km: float = 250.0, limit: int = 60) -> Dict[str, Any]:
        """Airports that fall inside the camera picture, as frame fractions (same projection as the aircraft marks), for
        the Airports layer on the live picture. Public data only: no station coordinates."""
        from src.analytics.adsb import sky_position
        data = self.get_airports()
        sol = self._overlay_solution()
        station = self._overlay_station()
        rows: List[Dict[str, Any]] = []
        for a in data.get("airports", []):
            pos = sky_position(station, a["lat"], a["lon"], a.get("elev_m", 0.0), sol)
            if not pos.get("frame_xy") or pos["distance_km"] > max_km:
                continue
            rows.append({"icao": a["ident"], "iata": a.get("iata") or "", "name": a["name"], "type": a["type"],
                         "frame_xy": pos["frame_xy"], "az_deg": pos["az_deg"], "alt_deg": pos["alt_deg"],
                         "distance_km": pos["distance_km"]})
        rows.sort(key=lambda r: ({"large": 0, "medium": 1}.get(r["type"], 2), r["distance_km"]))
        return {"ready": data.get("ready", False), "airports": rows[:limit]}

    def get_map_config(self) -> Dict[str, Any]:
        """Basemap settings for the 3D flights map. Only Google Maps (Map Tiles API, key in station config
        "google_maps_key") is offered on top of the elevation-only terrain; no OpenStreetMap-based maps."""
        return {"google_maps_key": str(self.station_config.get("google_maps_key") or "")}

    # ------------------------------------------------------------------
    # Traffic log: aircraft and satellites inside the camera view, kept for replay (retention-limited)
    # ------------------------------------------------------------------
    def traffic_log(self):
        """The on-disk log of what flew through the camera view (created on first use)."""
        from src.analytics.traffic_log import TrafficLog
        cfg = self.station_config.get("traffic_log") or {}
        tl = getattr(self, "_traffic_log", None)
        if tl is None:
            tl = self._traffic_log = TrafficLog(os.path.join(self.output_dir, "traffic"), cfg)
        else:
            tl.configure(cfg)
        return tl

    def _traffic_sample(self, now: float) -> Dict[str, List[List[Any]]]:
        """Aircraft (local ADS-B receiver first, else the public network when the fallback is on, plus Flightradar24 when enabled) and sunlit satellites
        that are above the horizon and inside the camera frame right now, as compact rows."""
        from src.analytics.adsb import motion_fields, sky_position

        sol = self._overlay_solution()
        if not sol:
            return {"a": [], "s": []}  # no star-solved pointing: nothing can be placed in the frame
        station = self._overlay_station()
        lat0, lon0 = float(station["latitude_deg"]), float(station["longitude_deg"])
        rows: Dict[str, List[Any]] = {}

        def add(icao, callsign, reg, typ, lat, lon, alt_m, spd, hdg):
            pos = sky_position(station, float(lat), float(lon), float(alt_m), sol)
            xy = pos.get("frame_xy")
            if not xy or pos["alt_deg"] <= 0 or icao in rows:
                return
            rows[icao] = [icao, callsign or None, reg or None, typ or None, round(float(lat), 5), round(float(lon), 5),
                          round(float(alt_m)), spd, hdg, pos["az_deg"], pos["alt_deg"], xy[0], xy[1]]

        feed = self._aircraft_feed()
        if feed is not None:
            try:
                for a in feed.current(at=now):
                    mf = motion_fields(a)
                    add(a["icao"], a.get("callsign"), None, None, a["lat"], a["lon"], a["altitude_m"],
                        mf.get("speed_kmh"), mf.get("heading_deg"))
            except Exception:
                pass
        fr = self._fr24()
        if fr.enabled:
            try:
                bounds = f"{lat0 + 3.0:.3f},{lat0 - 3.0:.3f},{lon0 - 3.0:.3f},{lon0 + 3.0:.3f}"
                for f in fr.current(bounds=bounds, station_lat=lat0, station_lon=lon0)[0]:
                    add(f["icao"], f.get("callsign"), f.get("reg"), f.get("type"), f["lat"], f["lon"], f["altitude_m"],
                        round(f["speed_kmh"]) if f.get("speed_kmh") is not None else None,
                        round(f["heading_deg"]) if f.get("heading_deg") is not None else None)
            except Exception:
                pass
        sats: List[List[Any]] = []
        from src.calibration.auto_exposure import calculate_solar_elevation
        if calculate_solar_elevation(lat0, lon0) < 0.0:  # a satellite is only a visible streak while the sky is dark
            for r in self.get_satellites_now(limit=400).get("satellites", []):
                xy = r.get("frame_xy")
                if xy and r.get("above_horizon") and r.get("sunlit"):
                    sats.append([r.get("norad"), r.get("name"), r.get("az_deg"), r.get("alt_deg"), xy[0], xy[1],
                                 r.get("range_km"), 1])
        return {"a": list(rows.values()), "s": sats}

    def _traffic_worker(self) -> None:
        """Every few seconds: store what is in the camera view. Never raises; ends with the service."""
        stop = self._retention_stop
        while not stop.wait(max(1.0, float(self.traffic_log().cfg["interval_s"]))):
            try:
                tl = self.traffic_log()
                if not tl.cfg["enabled"]:
                    continue
                now = time.time()
                smp = self._traffic_sample(now)
                tl.append(now, smp["a"], smp["s"])
            except Exception:
                logger.debug("traffic log sample failed", exc_info=True)

    def _adsb_wanted(self) -> bool:
        """Read the feed all the time by default ("record": the 3D map then has every aircraft's last 15
        minutes the moment it opens); with recording off, only while aircraft events can happen (plane
        detection on) or while the Flights panel is open (polled in the last 2 minutes)."""
        if (self.station_config.get("adsb") or {}).get("record", 1):
            return True
        if time.time() - getattr(self, "_adsb_view_ts", 0.0) < 120.0:
            return True
        return bool(self.get_event_config()["enabled"].get("plane"))

    def _network_base(self) -> str:
        from src.analytics.adsb_public import network_base
        return network_base(self.station_config)

    def start_adsb(self) -> None:
        """(Re)start the ADS-B reader from the station config; stops it when turned off."""
        from src.analytics.adsb import AdsbFeed
        from src.analytics.adsb_public import PublicAdsbFeed

        cfg = self.get_adsb_config()
        if cfg["public_fallback"] and self.adsb_public is None:
            self.adsb_public = PublicAdsbFeed(
                station=lambda: (float(self.auto_exposure.lat), float(self.auto_exposure.lon)),
                wanted=self._adsb_wanted, needed=self._public_needed, network=self._network_base)
            self.adsb_public.start()
        elif not cfg["public_fallback"] and self.adsb_public is not None:
            self.adsb_public.stop()
            self.adsb_public = None
        old = self.adsb
        if old is not None and (not cfg["enabled"] or (old.host, old.port) != (cfg["host"], cfg["port"])):
            old.stop()
            self.adsb = old = None
        if cfg["enabled"] and old is None:
            self.adsb = AdsbFeed(cfg["host"], cfg["port"], wanted=self._adsb_wanted)
            self.adsb.load_trails(self._adsb_trails_path())
            self.adsb.start()
        # Contributing: only the receiver feed (self.adsb) is ever handed over, never adsb_public or Flightradar24.
        if cfg["contribute"] and getattr(self, "adsb_contrib", None) is None:
            from src.analytics.adsb_contribute import AdsbContributor
            self.adsb_contrib = AdsbContributor(receiver=lambda: self.adsb, enabled=self._contribute_on,
                                                base=self._network_base, identity=self.community.identity)
            self.adsb_contrib.start()
        elif not cfg["contribute"] and getattr(self, "adsb_contrib", None) is not None:
            self.adsb_contrib.stop()
            self.adsb_contrib = None

    def _contribute_on(self) -> bool:
        return bool((self.station_config.get("adsb") or {}).get("contribute", 0))

    def _adsb_trails_path(self) -> str:
        return os.path.join(self.output_dir, "cache", "adsb_trails.json")

    def _match_flight(self, ev: Dict[str, Any]) -> None:
        """Name the flight behind a streak / dot event; a 'meteor' or 'satellite' that matches an aircraft
        becomes a plane."""
        feed = self._aircraft_feed()
        if feed is None or ev.get("type") not in ("plane", "meteor", "satellite") or not ev.get("line"):
            return
        sol = self.station_config.get("bright_star_solution") or {}
        if not sol.get("success"):
            return
        from src.analytics.adsb import frame_to_azalt, match_flight

        if sol.get("projection", "rectilinear") in ("rectilinear", "equidistant"):
            ln = ev["line"]
            ev["sky_from"] = dict(zip(("az_deg", "alt_deg"), frame_to_azalt(ln[0], ln[1], sol)))
            ev["sky_to"] = dict(zip(("az_deg", "alt_deg"), frame_to_azalt(ln[2], ln[3], sol)))
        t0 = float(ev.get("start_ts", ev["ts"]))
        t1 = float(ev.get("end_ts", t0))
        station = {"latitude_deg": self.auto_exposure.lat, "longitude_deg": self.auto_exposure.lon,
                   "elevation_m": float(self.station_config.get("elevation_m") or 0.0)}
        tracks = feed.tracks(t0, t1)
        flight = match_flight(ev, tracks, station, sol, lag_s=self.ADSB_FRAME_LAG_S)
        if flight is None:
            # Diagnostics: the nearest aircraft anywhere in the frame and how far off it was
            near = match_flight(ev, tracks, station, sol, lag_s=self.ADSB_FRAME_LAG_S, tol_px=5000.0, check_direction=False)
            if near is not None:
                ev["nearest_flight"] = {k: near.get(k) for k in ("callsign", "icao", "match_px", "match_angle_deg",
                                                                 "altitude_m", "distance_km", "az_deg", "alt_deg")}
            ev["adsb_aircraft"] = len(tracks)
            return
        if ev["type"] != "plane":
            # Re-label only on a close match that also runs the aircraft's way
            angle = flight.get("match_angle_deg")
            if flight["match_px"] > 20.0 or angle is None or angle > 20.0:
                return
            ev["reclassified_from"] = ev["type"]
            ev["type"] = "plane"
        ev["flight"] = flight
