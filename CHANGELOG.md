# Changelog

All notable changes to Zenivo are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.3.0-beta.3] - 2026-11-18

### Fixed

- Auto exposure now honors camera backend minimum and maximum gain limits; cameras below the generic 48 dB ceiling continue lengthening exposure instead of repeatedly requesting an unreachable gain.

- Events: on clear, crowded skies twinkling stars lined up by chance into faint "aircraft" and "satellite" dot tracks (most events in a three-day sample, none matching a flight). A weak dot track (under 60 DN) now needs six positions and must not sit on stars that are also bright in the other recent frames; strong strobes and flight-matched events are unchanged.
- RTS5869: the bridge's ISP gain default is restored at stream start (gain 0 halved MJPG frames and froze the daytime auto exposure on a white picture).

### Added

- Community: stations that sit close together on the map merge into one numbered badge with a ring showing how many are online, idle or offline; a tap zooms in, and stations at the same spot fan out so each can be opened.

### Changed

- Live: the Exposure card is back under the picture as one row (shutter, gain and Auto/Locked, open or closed remembered); the admin changes exposure there, a guest sees the live values with the controls disabled and a sign-in link. It is no longer shown in Settings > Image output.

## [1.3.0-beta.2] - 2026-11-17

Second public beta. Not every feature has been tested; see the notice in the README. Touch gestures on real phones (pinch, drag) have only been exercised with synthetic events.

### Added (web UI redesign)

- Mobile-first redesign of the whole web UI: phone header, a five-tab bottom bar (Live / Sky / Overhead / Archive / Menu), Menu and Layers sheets, self-hosted Inter and Space Grotesk fonts. Live, Overhead (`/overhead`, with a sky radar), Archive, the event viewer, Settings (grouped home list with search), Focus, Activity log, Community, Sign in, Sky and the 3D pages follow one design; desktop keeps the sidebar.
- Live: a tap or click on the picture shows an info card next to the object under it (star, planet, deep-sky object, constellation, aircraft, satellite, balloon) with rise and set times, aircraft type and route; a double tap, the wheel, a pinch or the − / + pill zoom. The Layers sheet gains **Overlay text** and **Airports**.
- Zoom and pan (wheel, pinch, drag, double tap, keys `+ - 0`, a − / + pill; 1x to 8x) in the Archive frame viewer (kept while a day plays), the gallery lightbox, the event viewer and timelapse videos. A click on the lightbox picture no longer closes it (backdrop, ✕ or Esc do); inline videos zoom with Ctrl + wheel.
- Sky page: rise/set times and lists for the Sun, Moon and planets (`GET /api/sky/riseset`, `GET /api/sky/bodies`, public).
- Events: **Copy link** (`/gallery?event=<id>`) and, for the admin, **Delete** (`GET`/`DELETE /api/events/{id}`).
- Settings > Admin: CPU, memory, disk and temperature tiles (`GET /api/system/resources`, admin). Settings > Image output now holds the Exposure card and a JPEG quality slider.
- `GET /api/adsb/info/{icao}` (type and route of an aircraft heard now) and `GET /api/map/airports/frame`.

### Changed

- Focus page: one primary button (**Start focus mode**, then **Finish & save**); **Stop without saving** and **Run autofocus sweep** are secondary, and the sweep button only shows for a motorised lens in Autofocus mode.
- The simulator (`--simulator`) always shows a night sky with stars, at any time of day, with night-mode auto exposure and picture analysis; astronomy read-outs (Sun altitude, rise/set, satellite visibility) stay real.
- Red Vision covers the new pages and controls; touch targets are at least 44 px on phones.

### Added

- Live page: aircraft, satellites and balloons are pushed over the live WebSocket (`subscribe` action, `overlay` messages) instead of one REST request every 1.5 s each; the old polling stays only as a fallback when the socket does not carry them.

- Weather and forecast around the station (Settings > Integrations > Weather and forecast, off by default): current conditions and the next 48 h from Open-Meteo (free, no key; the position is rounded to 0.1°; MET Norway answers when Open-Meteo refuses a shared IP address with HTTP 429), with a "clear hours in the next 12" outlook. New `GET /api/weather` (public), `POST /api/weather/{settings,refresh}` (admin); listed under Data sources.
- A warning (pill on the live picture) when the picture is too dark or too bright while exposure, gain or the target brightness is locked.
- The header and tab title show the station name when one is set. Community map: online / idle / offline icons, popups kept in view, the whole world fits on phones.

- Settings > Admin > Service: **Restart Zenivo** restarts the program from the web (for a camera that returns black pictures or hangs). Under systemd the service is brought back by `Restart=always`; run by hand it re-runs the same command line. New `POST /api/system/restart` (admin).
- Software update: the station checks GitHub for a new release once a day (on by default, switch in Settings > Admin; nothing about the station is sent). The admin installs it with one click (**Update now**): the release is downloaded, checked, the program files are backed up and swapped (data, settings and the virtual environment are never touched), changed Python requirements are installed and the service restarts. **Go back to the previous version** restores the backup. A git checkout is updated with `git merge --ff-only`. New `GET /api/update/status`, `POST /api/update/{check,settings,apply,rollback}` (the last four need the admin).
- Station timezone: Settings > Station has a **Station timezone** list (the operating system's zone by default, a whole-hour offset from the station coordinates, or any IANA zone). The clock, observing day and file names follow it, and the web UI shows the station's time instead of a fixed UTC+7 (daylight saving included). New `POST /api/station/timezone`; `timezone` and `timezone_info` in `GET /api/station/config`.
- Auto exposure resumes from the last exposure and gain when the service restarts within 2 hours, so the picture brightness does not jump after a restart.
- Satellites 3D shows the station and your own coordinates, with **Station** / **You** buttons to switch the view point; the station is the default and the browser location no longer moves the view on its own.
- OBS: the picture size of `/obs` is now the same as `/live` by default, and the OBS Dock has a **Size** button to send the full output resolution instead (`res`: `stream` or `full` in `/api/obs/overlay`; `?res=full|stream` in the URL still overrides it). Before, `/obs` always asked for the full frame unless `?res=stream` was in the URL.
- Community: stations show **Last seen** (when the station itself last announced to this one or answered a round) instead of the card's signing time, which could be up to 6 hours old on a healthy station. New local `seen` map in `/api/community/peers`; nothing changes in the cards.
- Settings > Sky Mask: **Auto-detect** proposes the open-sky and obstruction polygons from the current frame (`POST /api/station/sky-mask/auto`); you edit it by hand and press Apply, nothing is stored before that. Drawing the mask by hand works as before.
- `docs/FIRST_RUN.md`: step-by-step setup after installing (password, time zone, Station & Optics, lens, pointing check, sky mask, optional services). The installer and README point to it.

### Added (camera SDK)

- `scripts/install_camera_sdk.sh zwo|qhy|all` and `install.sh --camera-sdk`: installs `zwoasi`, the vendor library (into `vendor/lib`, no root) and the udev rules (asks before using sudo) for ZWO ASI / QHY cameras in one step (downloaded from a pinned INDI 3rd-party commit when you run it). `install.sh --camera` now accepts `zwo` and `qhy`.

### Added (Integrations)

- Settings > Integrations: a **Data sources** overview (state of ADS-B, public fallback, Flightradar24, satellites, weather balloons, airports) and a **Satellites** panel: switch, CelesTrak groups (stations, brightest, weather, NOAA, GPS, Galileo, science), refresh interval, Download now, an optional alternative TLE source, and an optional **SatNOGS DB** orbit source (about 1700 satellites in one file). New `GET/POST /api/satellites/config`, `POST /api/satellites/refresh`, `GET /api/data-sources`.

### Fixed

- Cloud cover at night: the star check scaled the catalogue positions from the optical centre instead of the frame size the pointing was solved on, which put every star about 15 px off and made a clear night read 30 to 50% cloud (and 100% before the shift search). It now uses the solved frame size; a re-solved station sees 0.98 to 1.0 of its catalogue stars on a clear night. Solutions without `frame_width` keep the old scaling.
- Content-Security-Policy (report-only) no longer reports Cloudflare's analytics beacon, which Cloudflare adds in front of some stations.
- Live page behind a proxy that drops WebSockets: after two sockets that fail to open or hold, the page switches to plain `fetch` polling (`GET /api/live/frame.jpg`, the same picture as the stream, 304 when unchanged, plus `/api/telemetry`), keeps a single request in flight instead of reopening sockets, remembers the bad network for 15 minutes and only probes the socket now and then (1 to 10 min apart). The old SSE + MJPEG fallback, which also hung behind such proxies, is gone.
- The Timelapse builder is shown to the admin only (the server already refused it for guests).
- Cloud cover: the dark rim and corners outside a fisheye lens circle are no longer judged as sky.
- Cloud cover: the star-visibility check (catalogue stars expected vs seen) now searches a small shift (up to 4% of the width) around the stored star-solved pointing. A mount that had drifted ~15 px made it see 0 of 34 stars on a clear night and report ~100% cloud. With no clear shift (real cloud) the stored pointing is used as before.
- A plugged-in ZWO or QHY camera that is not offered now says why (missing `zwoasi`, missing vendor SDK, missing udev rules) under Rescan and once in the log (`Camera scan: ...`).
- Choosing `rts5869` under Settings > Camera no longer fails with "no usable camera" (the device node is now found automatically).
- Satellites 3D zooms out far enough to show GEO and high-orbit satellites; the event viewer no longer draws a dashed path across the moving object.
- The simulator camera drew only noise (a black picture). It now has a synthetic sky: stars that drift, sky glow that follows exposure and gain, and a bright daylight sky by day so the auto exposure settles. Lens-capped dark calibration still sees no stars.
- Settings > Camera: the Rescan button was unstyled and gave no feedback; it now matches the other buttons and reports how many cameras it found.
- Meteor, plane and satellite detection failed on OpenCV 5 (`HoughLinesP` result shape); CI now passes on current library versions.

### Changed

- Large files split without behaviour change: `src/api/routes.py` became the `src/api/routes/` package, `service.py` shrank from 1199 to about 530 lines (four new mixins), `focus.js` became `src/web/focus/*.js`, the sky overlay and gallery scripts and `style.css` were split into parts. CI now also runs on push and lints with `ruff`; issue and pull request templates added.
- Code cleanup: removed unused imports and variables, fixed a missing `Tuple` import; added a `ruff` pyflakes-only configuration to `pyproject.toml`.

## [1.3.0-beta.1] - 2026-11-15

First public beta. Not every feature has been tested; see the notice in the README.

### Added

- ZWO ASI cameras: `--camera zwo` (`--camera-index N`) through the `zwoasi` wrapper and ZWO's `libASICamera2` SDK (`ZWO_ASI_LIB` sets its path). RAW16 frames are demosaiced to 8-bit BGR (mono sensors give grey), exposure 32 µs to the camera maximum, gain in 0.1 dB units, hardware binning from `--resolution`. Opt-in, never chosen by `auto`. Optional extra: `pip install zwoasi`. Tested against a fake SDK, and the real SDK loads and reports no camera; not yet run with a camera attached.
- QHYCCD cameras: `--camera qhy` (`--camera-index N`) through QHY's official `libqhyccd` SDK called with ctypes (no pip package; `QHY_SDK_LIB` sets its path). Single-frame 16-bit, colour sensors demosaiced on the CPU, mono sensors use hardware 2x2 binning for half-size output. QHY gain is in SDK units, mapped onto 0 to 30 "dB" for auto-exposure. The blocking SDK read runs in a worker thread with a deadline. Opt-in, never chosen by `auto`. Tested against a fake SDK and on a QHY163M (mono, 4656x3522, 1.2 s per frame over USB 3); the cooler is not controlled yet.
- Settings > Camera: pick the active camera when several are attached (ZWO ASI, IMX335 USB, Pi camera, webcams). `GET /api/camera/devices` (admin) and `POST /api/camera/select`; the choice is saved (`camera_select` in `station_config.json`) and used at start when `--camera` is `auto`. `--camera-index` now also selects among several `v4l2` webcams. Output resolution limits raised to 12000 px so large sensors can be selected.

## [1.2.0] - 2026-11-15

### Added

- security: the Flightradar24 account e-mail, password and token in Settings are sent only by the "Send to Flightradar24" button, never by autosave. A browser password manager could fill these fields with the admin sign-in, and autosave then posted it to flightradar24.com as a sign-in attempt. The server now refuses a non-e-mail account value and allows one failed sign-in per minute.
- security: `/obs/dock` (and `/obs/control`, `/dock`) are admin pages; the OBS preview page `/obs` stays public.
- Community preview relay: a station without a public URL pushes its large preview (signed upload, at most 200 KB) to stations that opted in; those re-encode it (JPEG, at most 1280 px), keep it 24 h (100 stations / 25 MB) and serve it at `GET /api/community/relay/<id>.jpg`. New setting `relay_previews` (default on; turning it off deletes what is held and refuses uploads), `POST /api/community/relay/<id>`, optional signed card fields `relays`, `preview_sha`, `preview_t` (stations that predate them do not show such a card), `relay` in status and announce replies, `relayed` map in `/peers`. The map falls back from the card url to the held copy, to the card's relays, to the embedded thumbnail. See docs/COMMUNITY.md.
- Settings: every card saves automatically on change and shows a status chip; buttons remain only for actions.
- Settings, Integrations: Flightradar24 block (switch, e-mail, password, token) and a "Weather balloons (SondeHub)" switch (`radiosondes_enabled`, saved through `POST /api/adsb`; `GET /api/radiosondes` returns `enabled`, and the Live page shows "turned off" and re-checks every 5 minutes).
- Settings, Detection: daytime object (birds, insects, aircraft) settings, `GET`/`POST /api/day-objects/config`; `GET /api/day-objects/status` also returns `config`, `classes` and `presets`.

### Changed

- The position sent to SondeHub is rounded to a 0.5 degree grid (search radius 350 km); the exact position is used only locally.

- New [docs/TRANSPARENCY.md](docs/TRANSPARENCY.md): every outbound connection, third-party browser load, built-in community seed, file on disk and guest/admin rule, with source files and verification commands; SECURITY, COMMUNITY, CONFIGURATION, API and INSTALL corrected and linked to it.
- The server binds to `127.0.0.1` by default (`--host`); use `--host 0.0.0.0` to expose it on the LAN.
- The Flightradar24 feed is off until `adsb.fr24_enabled` is set (opt-in outbound connection).
- A fresh install no longer ships a city: the factory station is "My station" at 0, 0 (`src/defaults.py`). Saved configs are unaffected.
- Three.js import maps on the 3D flights and satellites pages carry Subresource Integrity hashes.
- Removed a leftover third-party radiosonde link and wording; `GET /api/events` rejects `limit` above 100 unless `brief=true`.

### Fixed

- Flightradar24 token and password are write-only: `/api/station/config` no longer returns them and `GET /api/adsb` reports only `fr24_has_token`.
- Settings, Admin section was unreachable (`#admin` was missing from the section switcher), so the password could not be changed from the UI.
- Guest live view recovers when the server ends the MJPEG stream after 10 minutes (the image reloads itself).
- The 3D flights map shows "Aircraft are shown only near the station" when a guest pans out of range, and backs off instead of retrying.

## [1.1.0] - 2026-11-13

### Changed

- Community: station cards are kept for 30 days and up to 50 addresses that answered are remembered without expiry, so the network keeps working when the seed stations are down.
- CORS no longer allows credentials; the admin cookie is same-site only.
- Station activity messages no longer contain coordinates.

### Added

- Community (opt-in, off by default): stations share a signed station card (name, approximate location,
  sensor, optional small preview) with each other by gossip over HTTP(S), with no central server.
  Ed25519 identity in `data/community_key.pem`, signed and size-capped cards, SSRF-guarded fetching,
  endpoints under `/api/community/`, a Community section in Settings and a `/community` map page (Leaflet).
  See `docs/COMMUNITY.md`.
- Admin login (single admin, no new dependency): everything that changes or leaks is admin only and denied by default,
  including WebSocket commands; guests can watch. First-start setup code in `data/admin_setup_code.txt`, scrypt password,
  30-day sessions, login throttling, CSRF origin check, `run_allsky.py --set-admin-password` and `--reset-admin`,
  `/login` page, `/api/auth/*`, Settings, Admin. See `docs/SECURITY.md`.
- Guests see the station location rounded (`public_precision_deg`: exact, 0.01, 0.1 default or 1.0 degree); the community
  card uses the same setting (the Community card's own precision select is gone, the old `precision_deg` key is still read).
- New dependency: `cryptography>=38` (without it Community reports itself unavailable and everything else works).

## [1.0.0] - 2026-11-13

First release under the Zenivo name.

### Added

- Zenivo name, tagline ("All-sky observatory software"), logo (two rings and one star), colour
  palette, favicon, web app manifest and social image, in `src/web/brand/` and `src/web/tokens.css`.
  Red Vision is a real alternate palette for dark adaptation.
- Single version source `src/version.py`, shown by `python3 run_allsky.py --version`,
  `GET /api/version` and the UI sidebar.
- `install.sh`: creates the virtual environment, installs the dependencies and a systemd unit
  (`--dir`, `--port`, `--host`, `--camera`, `--user`, `--service-name`, `--no-systemd`, `--dry-run`).
- `deploy/zenivo.service` (systemd template), `deploy/nginx-zenivo.example.conf` and
  `deploy/README.md`.
- Documentation: `README.md`, `docs/INSTALL.md`, `docs/CONFIGURATION.md`, `docs/API.md`,
  `docs/BRAND.md`, and an updated `docs/SDD.md`.
- Navigation, brand block and page head generated for every page from one definition:
  `python3 scripts/sync_nav.py [--check]`.

### Changed

- Host services, install folders and the GitHub repository renamed to zenivo (legacy `~/allsky-ng` symlink kept).
- Menu reorganised into Live / Sky / Archive / Camera / Broadcast / Settings. On phones the tab bar
  shows 4 tabs and a More sheet.
- `AllSkyService` (5.6k lines) split into focused mixins: calibration, products, events, traffic,
  station, focus, lens and plate solve. The public API is unchanged.
- The 5.2k-line `app.js` split into 8 ordered scripts in `src/web/app/`. They share one global scope
  and run in the same order as before.
- Outbound HTTPS uses one shared SSL context and a keep-alive connection pool (`src/net.py`)
  instead of a new context per request. This lowers CPU use on a Raspberry Pi.
- The sync worker waits on an event instead of sleeping, so it stops promptly on shutdown.
- Each page loads only the data of its own tab, and `/api/focus` and the sky mask are no longer
  fetched twice.
- `run_allsky.py` has a `--version` flag and a new description.

### Fixed

- Phone tab bar used 7 columns for 5 tabs; toasts now sit compactly above the tab bar; the timelapse
  form no longer overflows on phones; the cloud legend wraps on phones.
- Histogram display is smoothed, so gain-stretched 8-bit frames no longer show a comb pattern.
- Event detection: aircraft strobe trails whose dots never drop below the threshold are classified
  as planes, not meteors; slow, long dot tracks (stars drifting at the sidereal rate) are no longer
  reported as aircraft.

### Known issues

Five tests fail at 1.0.0 (`python3 -m pytest -q`):

- Events limit validation: 2 tests.
- Keogram and startrails dimension mismatch: 2 tests.
- Day-rotation sky-analysis throttle: 1 test.

Other known limits:

- The API has no authentication. Bind to `127.0.0.1` behind a reverse proxy, or use a trusted network.
- The sync client has no persistent queue: frames missed during an outage are not re-sent.
- `--reload` is parsed but not passed to uvicorn.
- The default `--sync-target` is `http://localhost:3005`; pass `--sync-target ""` on installs without a sync server.
- No licence has been chosen: all rights are reserved by the author until one is added.

## Before 1.0.0

Development started on 2026-10-17 as "allsky-ng" and ran unversioned until the Zenivo rename.
Main steps, newest first:

- **Day and traffic** (November 2026): sky events by day and night; birds, insects and aircraft
  followed by day; rain detection from drops on the dome; a log of aircraft and satellites in the
  camera view; per-pixel ISP shading calibration and AWB lock for the RTS5869 camera; MJPG by day
  and an `ALLSKY_DISABLE_RAW10` switch for hosts where the RAW10 route stalls; exposure before gain
  at dusk and night; RA/Dec grid layer; master dark preview on `/calibration`.
- **Sky pages and plate solving**: `/sky` planetarium view with the camera frame, aircraft,
  satellites and radiosondes; `/flights3d` and `/satellites3d`; station alignment by clicking stars;
  fisheye lens model for the sky overlay; J2000 catalogue precessed to the date of observation.
- **ADS-B and traffic**: local SBS-1 receiver and Flightradar24 feed, aircraft overlay on the live
  view, satellite overlay with SGP4, SondeHub radiosonde tracking, an ADS-B feeder service for
  an external ingest endpoint.
- **Storage**: frames archived as AVIF with JPEG fallback and a conversion tool.
- **Live review**: frame viewer with an event timeline on the cloud chart; every sky event keeps a
  clean archived frame.
- **Cloud and focus**: cloud cover analysis with history, star-visibility term, focus assistant
  with motorised lens control, plate solving with sky mask, observer-local time handling.
- **Initial import** (2026-10-17): FastAPI service with RTS5869/IMX335 and picamera2 drivers,
  dark calibration, auto-exposure, keogram, startrails and timelapse.
