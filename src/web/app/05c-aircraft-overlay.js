/* Zenivo dashboard (part of the former app.js): ADS-B aircraft, radiosonde and satellite overlays, viewport fit and overlay refresh wiring.
 * Classic script: continues 05-sky-overlay.js; top-level declarations are shared with the other src/web/app/*.js files; load order is fixed in index.html. */

// ---- ADS-B aircraft overlay: marks planes that fall inside the camera's field of view ----
const AIRCRAFT_MAX_KM = 100;   // farther aircraft are not drawn on the frame or the sky view
let aircraftList = [];
let aircraftFetchedAt = 0;
let aircraftT = 0;       // server time of the positions in aircraftList
let liveFrameT = null;   // server time the displayed frame was taken (mid-exposure)
let aircraftFrameT = null; // frame time already fetched; one historical request per displayed frame
let aircraftDrawQueued = false;
function scheduleAircraftDraw() {
    if (aircraftDrawQueued) return;
    aircraftDrawQueued = true;
    requestAnimationFrame(() => { aircraftDrawQueued = false; drawAircraft(); });
}
// Layers pushed over the live WebSocket ({type:"overlay"}); REST polling below only runs while no push arrives
const overlayPush = { at: {}, want: [] };
// The server skips pushes whose content did not change, so "live" means subscribed and acknowledged, not "recently heard"
function overlayPushLive(layer) { return window.overlayPushOn(layer); }
window.overlayPushOn = function overlayPushOn(layer) { return !!((!layer || overlayPush.want.includes(layer)) && state.ws && state.ws.readyState === 1 && overlayPush.acked && Date.now() - overlayPush.acked < 3600000); };
// Every layer the Live page lists in a card is always subscribed; the overlay switch only decides whether it is drawn on the
// picture and how often it comes (overlay on: its own pace; card only: every 10 s, see overlaySlowLayers)
const OVERLAY_CARDS = { aircraft: "flights-card", satellites: "sats-card", sondes: "sondes-card" };
function overlayChecked(layer) {
    const c = layer === "aircraft" ? chkAircraft : layer === "satellites" ? chkSatellite : chkSonde;
    return !!(c && c.checked);
}
function overlayLayersWanted() {
    if (currentPage !== "tab-live") return [];
    const out = ["events"];
    if (window.zenivoAuth && window.zenivoAuth.admin) out.push("logs");
    Object.keys(OVERLAY_CARDS).forEach((l) => { if (overlayChecked(l) || document.getElementById(OVERLAY_CARDS[l])) out.push(l); });
    return out;
}
function sendOverlaySubscription() {
    const layers = overlayLayersWanted();
    overlayPush.want = layers;
    // Overlay off: the card alone needs the layer, at the card's pace
    const slow = layers.filter((l) => OVERLAY_CARDS[l] && !overlayChecked(l));
    if (state.ws && state.ws.readyState === 1 && currentPage === "tab-live") state.ws.send(JSON.stringify({ action: "subscribe", layers, slow }));
}
// A hidden tab asks the server to stop pushing (pictures slow to a 30 s heartbeat, layers pause) and resumes when shown again
function sendIdle() {
    if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify({ action: "idle", on: document.hidden }));
}
document.addEventListener("visibilitychange", sendIdle);
// Packed pushes ({type:"ov"}, src/api/overlay_pack.py): decoded by the shared /static/overlay_decode.js (also used by the Overhead page)
function applyPackedOverlay(msg) {
    const d = window.ZenivoOverlay && window.ZenivoOverlay.apply(msg);
    if (d) applyOverlayPush(msg.l, d);
}
function applyOverlayPush(layer, d) {
    overlayPush.at[layer] = Date.now();
    if (layer === "events") { if (typeof loadLiveEvents === "function") loadLiveEvents(); return; }
    if (layer === "logs") { if (window.zenivoPollLogs) window.zenivoPollLogs(); return; }

    // One push serves the card (always) and the overlay (when its switch is on)
    if (layer === "aircraft") {
        if (typeof renderFlights === "function" && document.getElementById("flights-card")) renderFlights({ ...d, aircraft: ((d && d.aircraft) || []).slice(0, 12) });
        aircraftList = (d && d.aircraft) || []; aircraftFetchedAt = Date.now(); aircraftT = (d && d.t) || 0;
    } else if (layer === "sondes") {
        const list = ((d && d.sondes) || []).slice().sort((a, b) => (a.distance_km ?? 1e9) - (b.distance_km ?? 1e9));
        if (typeof renderSondes === "function" && document.getElementById("sondes-card")) renderSondes({ ...d, sondes: list.slice(0, 8) });
        sondeList = list; sondeT = (d && d.t) || 0; sondeFetchedAt = Date.now();
    } else if (layer === "satellites") {
        if (typeof renderSats === "function") renderSats(d);
        satelliteList = (d && d.satellites) || [];
    }
    drawAircraft();
}
function refreshAircraft() {
    if (!chkAircraft || !chkAircraft.checked || document.hidden) return;
    const frameT = Number.isFinite(liveFrameT) ? liveFrameT : null;
    if (frameT != null && aircraftFrameT != null && Math.abs(frameT - aircraftFrameT) < 0.05) {
        drawAircraft();
        return;
    }
    // Packed pushes serve the current live view.  A timestamped displayed frame still
    // needs a historical query so aircraft positions align with its exposure midpoint.
    if (frameT == null && overlayPushLive("aircraft")) return;
    const at = frameT == null ? "" : `&at=${encodeURIComponent(frameT)}`;
    fetch(`/api/adsb/aircraft?limit=60${at}`)
        .then((r) => r.json())
        .then((d) => {
            if (frameT != null && liveFrameT != null && Math.abs(frameT - liveFrameT) >= 0.05) return;
            aircraftList = (d && d.aircraft) || [];
            aircraftFetchedAt = Date.now();
            aircraftT = (d && d.t) || 0;
            aircraftFrameT = frameT;
            drawAircraft();
        })
        .catch(() => {});
}

// Radiosondes (SondeHub weather balloons)
let sondeList = [];
let sondeT = 0;
let sondeFetchedAt = 0;
function refreshSondes() {
    if (!chkSonde || !chkSonde.checked || document.hidden || overlayPushLive("sondes")) return;
    fetch("/api/radiosondes")
        .then((r) => r.json())
        .then((d) => {
            sondeList = (d && d.sondes) || [];
            sondeT = (d && d.t) || 0;
            sondeFetchedAt = Date.now();
            drawAircraft();
        })
        .catch(() => {});
}
if (chkSonde && currentPage === "tab-live") {
    setInterval(refreshSondes, 3000);
    setTimeout(refreshSondes, 600);
}

// Large satellites (space stations, bright satellites): only those lit by the sun under a dark sky are drawn
let satelliteList = [];
function refreshSatellites() {
    if (!chkSatellite || !chkSatellite.checked || document.hidden || overlayPushLive("satellites")) return;
    fetch("/api/satellites")
        .then((r) => r.json())
        .then((d) => {
            satelliteList = (d && d.satellites) || [];
            drawAircraft();
        })
        .catch(() => {});
}

// Airports (OurAirports) that fall inside the picture: the server projects them like the aircraft marks
let airportList = [], airportsAt = 0;
function refreshAirports() {
    if (!chkAirports || !chkAirports.checked) { drawAircraft(); return; }
    if (document.hidden || Date.now() - airportsAt < 25000) { drawAircraft(); return; }
    airportsAt = Date.now();
    fetch("/api/map/airports/frame")
        .then((r) => r.json())
        .then((d) => {
            airportList = (d && d.airports) || [];
            if (d && d.ready === false) airportsAt = Date.now() - 20000;   // still downloading: ask again soon
            drawAircraft();
        })
        .catch(() => {});
}

function drawAircraft() {
    if (!aircraftCanvas || !viewportContainer) return;
    const w = viewportContainer.clientWidth;
    const h = viewportContainer.clientHeight;
    if (w <= 10 || h <= 10) return;
    const pxRatio = Math.min(4, (window.devicePixelRatio || 1) * view.zoom);
    aircraftCanvas.width = Math.round(w * pxRatio);
    aircraftCanvas.height = Math.round(h * pxRatio);
    const ctx = aircraftCanvas.getContext("2d");
    ctx.setTransform(pxRatio, 0, 0, pxRatio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const showAircraft = !!(chkAircraft && chkAircraft.checked);
    const showSat = !!(chkSatellite && chkSatellite.checked);
    const showSonde = !!(chkSonde && chkSonde.checked);
    const showAirports = !!(chkAirports && chkAirports.checked);
    if (!showAircraft && !showSat && !showSonde && !showAirports) return;

    const st = state.station || {};
    const sensor = st.camera_sensor || {};
    const sensorW = Number(sensor.width_px || 2592);
    const sensorH = Number(sensor.height_px || 1944);
    const containerAspect = w / Math.max(1, h);
    const sensorAspect = sensorW / Math.max(1, sensorH);
    let renderW = w, renderH = h, offsetX = 0, offsetY = 0;
    if (containerAspect > sensorAspect) {
        renderW = h * sensorAspect;
        offsetX = (w - renderW) / 2.0;
    } else {
        renderH = w / sensorAspect;
        offsetY = (h - renderH) / 2.0;
    }
    const tf = st.image_transform || {};
    const rot180 = Number(tf.rotate_deg || 0) % 360 === 180;
    const mirrorX = !!tf.flip_horizontal !== rot180;
    const mirrorY = !!tf.flip_vertical !== rot180;

    ctx.save();
    ctx.beginPath();
    ctx.rect(offsetX, offsetY, renderW, renderH);
    ctx.clip();
    ctx.font = "11px monospace";
    ctx.textBaseline = "middle";
    // Marks sit where the plane was when the shown frame was taken: the fetched position slid along its own frame velocity
    const age = (Date.now() - aircraftFetchedAt) / 1000.0;
    const shift = aircraftT && liveFrameT != null ? liveFrameT - aircraftT : age;
    const lag = Math.max(-5, Math.min(20, shift));
    if (showAirports) {
        airportList.forEach((ap) => {
            if (!ap.frame_xy) return;
            let fx = ap.frame_xy[0], fy = ap.frame_xy[1];
            if (mirrorX) fx = 1 - fx;
            if (mirrorY) fy = 1 - fy;
            const x = offsetX + fx * renderW;
            const y = offsetY + fy * renderH;
            ctx.strokeStyle = "rgba(134, 239, 172, 0.95)";
            ctx.fillStyle = "rgba(134, 239, 172, 0.95)";
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.arc(x, y, 4, 0, Math.PI * 2);
            ctx.stroke();
            if (ap.type === "small" && view.zoom < 2) return;   // small airfields: marker only until zoomed in
            ctx.save();
            ctx.font = "10px monospace";
            ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
            ctx.shadowBlur = 4;
            ctx.textAlign = x > offsetX + renderW * 0.8 ? "right" : "left";
            ctx.fillText(ap.icao, x + (ctx.textAlign === "left" ? 8 : -8), y);
            ctx.restore();
        });
    }
    if (showSat) {
        satelliteList.forEach((sat) => {
            if (!sat.frame_xy || !sat.in_view) return;
            const dim = !sat.visible;   // above the camera but not visible to the eye now (daylight or in the Earth's shadow)
            let fx = sat.frame_xy[0], fy = sat.frame_xy[1];
            if (mirrorX) fx = 1 - fx;
            if (mirrorY) fy = 1 - fy;
            const x = offsetX + fx * renderW;
            const y = offsetY + fy * renderH;
            ctx.strokeStyle = dim ? "rgba(34, 211, 238, 0.45)" : "rgba(34, 211, 238, 0.95)";
            ctx.fillStyle = dim ? "rgba(34, 211, 238, 0.6)" : "rgba(34, 211, 238, 0.95)";
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x, y - 12); ctx.lineTo(x + 12, y); ctx.lineTo(x, y + 12); ctx.lineTo(x - 12, y); ctx.closePath();
            ctx.save();
            ctx.globalAlpha = 0.6;
            ctx.stroke();
            ctx.restore();
            const txt = `${sat.name} ${sat.height_km}km`;
            ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
            ctx.shadowBlur = 4;
            ctx.textAlign = x > offsetX + renderW * 0.75 ? "right" : "left";
            ctx.fillText(txt, x + (ctx.textAlign === "left" ? 16 : -16), y);
            ctx.shadowBlur = 0;
        });
    }
    if (showSonde) {
        const sLag = Math.max(-5, Math.min(20, sondeT && liveFrameT != null ? liveFrameT - sondeT : (Date.now() - sondeFetchedAt) / 1000.0));
        sondeList.forEach((sd) => {
            if (!sd.frame_xy || !sd.in_view) return;
            let fx = sd.frame_xy[0], fy = sd.frame_xy[1];
            if (sd.frame_v) { fx += sd.frame_v[0] * sLag; fy += sd.frame_v[1] * sLag; }
            if (mirrorX) fx = 1 - fx;
            if (mirrorY) fy = 1 - fy;
            const x = offsetX + fx * renderW;
            const y = offsetY + fy * renderH;
            ctx.strokeStyle = "rgba(244, 114, 182, 0.95)";
            ctx.fillStyle = "rgba(244, 114, 182, 0.95)";
            ctx.lineWidth = 1.5;
            ctx.save();
            ctx.globalAlpha = 0.6;
            ctx.beginPath();
            ctx.rect(x - 11, y - 11, 22, 22);
            ctx.moveTo(x - 4, y - 4); ctx.lineTo(x + 4, y + 4); ctx.moveTo(x + 4, y - 4); ctx.lineTo(x - 4, y + 4);
            ctx.stroke();
            ctx.restore();
            const txt = `${sd.serial} ${(sd.altitude_m / 1000).toFixed(1)}km ${sd.distance_km}km`;
            ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
            ctx.shadowBlur = 4;
            ctx.textAlign = x > offsetX + renderW * 0.75 ? "right" : "left";
            ctx.fillText(txt, x + (ctx.textAlign === "left" ? 16 : -16), y);
            ctx.shadowBlur = 0;
        });
    }
    if (showAircraft) aircraftList.forEach((a) => {
        if (!a.frame_xy) return;
        if (a.distance_km != null && a.distance_km > AIRCRAFT_MAX_KM) return;   // too far to be a visible light: skip
        let fx = a.frame_xy[0], fy = a.frame_xy[1];
        if (a.frame_v) { fx += a.frame_v[0] * lag; fy += a.frame_v[1] * lag; }
        if (mirrorX) fx = 1 - fx;
        if (mirrorY) fy = 1 - fy;
        const x = offsetX + fx * renderW;
        const y = offsetY + fy * renderH;
        ctx.strokeStyle = "rgba(251, 191, 36, 0.95)";
        ctx.fillStyle = "rgba(251, 191, 36, 0.95)";
        ctx.lineWidth = 1.5;
        // The circle is a margin for the timing error (~1.5 s of travel): bigger for a plane that crosses the frame faster
        const rad = a.frame_v ? Math.max(16, Math.min(44, 14 + Math.hypot(a.frame_v[0] * renderW, a.frame_v[1] * renderH) * 1.5)) : 16;
        ctx.beginPath();
        ctx.arc(x, y, rad, 0, Math.PI * 2);
        ctx.save();
        ctx.globalAlpha = 0.6;
        ctx.stroke();
        ctx.restore();
        const name = a.callsign || a.icao || "?";
        const alt = a.altitude_m != null ? `FL${String(Math.round(a.altitude_m / 30.48)).padStart(3, "0")}` : "";
        const dist = a.distance_km != null ? `${a.distance_km}km` : "";
        const txt = [name, alt, dist].filter(Boolean).join(" ");
        ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
        ctx.shadowBlur = 4;
        ctx.textAlign = x > offsetX + renderW * 0.75 ? "right" : "left";
        ctx.fillText(txt, x + (ctx.textAlign === "left" ? rad + 3 : -(rad + 3)), y);
        ctx.shadowBlur = 0;
    });
    ctx.restore();
}

if (chkAirports && currentPage === "tab-live") {
    setInterval(refreshAirports, 30000);
    setTimeout(refreshAirports, 1500);
}

if (chkAircraft && currentPage === "tab-live") {
    setInterval(refreshAircraft, 1500);
    window.addEventListener("resize", drawAircraft);
    setTimeout(refreshAircraft, 400);
}
if (chkSatellite && currentPage === "tab-live") {
    setInterval(refreshSatellites, 1500)  /* LEO satellites move up to ~1 deg/s */;
    setTimeout(refreshSatellites, 600);
}

if (chkSkyOverlay) chkSkyOverlay.addEventListener("change", drawReticle);
if (chkSkyLabels) chkSkyLabels.addEventListener("change", drawReticle);
if (chkGridOverlay) chkGridOverlay.addEventListener("change", drawReticle);
if (chkEqGrid) chkEqGrid.addEventListener("change", drawReticle);
// Match the viewport box to the camera's real aspect ratio (4:3 IMX335, 16:9 IMX708, ...)
function fitViewportToFrame() {
    if (!liveImg || !viewportContainer || !liveImg.naturalWidth || !liveImg.naturalHeight) return;
    const ratio = `${liveImg.naturalWidth} / ${liveImg.naturalHeight}`;
    // The server pre-sizes the box (data-frame-ratio); only touch it if the camera's ratio differs
    const [pw, ph] = (viewportContainer.dataset.frameRatio || "").split("/").map(Number);
    if (pw && ph && Math.abs(pw / ph - liveImg.naturalWidth / liveImg.naturalHeight) < 0.01) return;
    viewportContainer.dataset.frameRatio = ratio;
    viewportContainer.style.aspectRatio = ratio;
    applyView(false);
}
if (liveImg) liveImg.addEventListener("load", fitViewportToFrame);
fitViewportToFrame();
if (liveImg) liveImg.addEventListener("load", drawReticle);
window.addEventListener("resize", rafDebounced(drawReticle));
document.addEventListener("fullscreenchange", () => setTimeout(drawReticle, 80));

// Re-render overlay immediately when user tweaks optical/orientation inputs in Diagnostics tab
[
    stationLatInput,
    stationLonInput,
    optCxInput,
    optCyInput,
    optR90Input,
    oriNpaInput,
    oriTiltInput,
    oriAzInput,
].forEach((inp) => {
    if (inp) inp.addEventListener("input", drawReticle);
});

// Periodically refresh sky overlay as Earth rotates (every 5 seconds)
setInterval(() => {
    if (document.hidden) return;
    const anyOverlay =
        (chkSkyOverlay ? chkSkyOverlay.checked : true) ||
        (chkSkyLabels ? chkSkyLabels.checked : true) ||
        (chkGridOverlay ? chkGridOverlay.checked : false) ||
        (chkEqGrid ? chkEqGrid.checked : false);
    if (!anyOverlay) return;
    drawReticle();
}, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) drawReticle(); });
setTimeout(drawReticle, 150);
