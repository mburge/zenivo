/* Zenivo dashboard (part of the former app.js): Sky overlay drawing (drawReticle: horizon, grids, constellations, stars, labels, badge).
 * Classic script: continues 05-sky-overlay.js; top-level declarations are shared with the other src/web/app/*.js files; load order is fixed in index.html. */

function drawReticle() {
    if (!reticleCanvas || !viewportContainer) return;
    const w = viewportContainer.clientWidth;
    const h = viewportContainer.clientHeight;
    if (w <= 10 || h <= 10) return;

    // Render the overlay at zoomed pixel density so it stays sharp when magnified
    const pxRatio = Math.min(4, (window.devicePixelRatio || 1) * view.zoom);
    reticleCanvas.width = Math.round(w * pxRatio);
    reticleCanvas.height = Math.round(h * pxRatio);
    const ctx = reticleCanvas.getContext("2d");
    ctx.setTransform(pxRatio, 0, 0, pxRatio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    // Everything drawn below is also registered here (stage px) so a tap on the picture can pick it (app/08d-live-ui.js)
    const hits = (window.zenSkyHits = []);
    if (typeof drawAircraft === "function") drawAircraft();

    const showSky = chkSkyOverlay ? chkSkyOverlay.checked : true;
    const showLabels = chkSkyLabels ? chkSkyLabels.checked : true;
    const showGrid = chkGridOverlay ? chkGridOverlay.checked : false;
    const showEqGrid = chkEqGrid ? chkEqGrid.checked : false;

    if (!showSky && !showLabels && !showGrid && !showEqGrid) return;

    // Read solved station, camera sensor, optics & orientation parameters
    const st = state.station || {};
    const sensor = st.camera_sensor || {};
    const optics = st.optics || st.camera || {};
    const ori = st.orientation || {};

    const sensorW = Number(sensor.width_px || 2592);
    const sensorH = Number(sensor.height_px || 1944);

    const latDeg =
        st.latitude_deg !== undefined
            ? Number(st.latitude_deg)
            : stationLatInput && !isNaN(parseFloat(stationLatInput.value))
            ? parseFloat(stationLatInput.value)
            : 0;
    const lonDeg =
        st.longitude_deg !== undefined
            ? Number(st.longitude_deg)
            : stationLonInput && !isNaN(parseFloat(stationLonInput.value))
            ? parseFloat(stationLonInput.value)
            : 0;

    const optX =
        optics.optical_center_x_px !== undefined
            ? Number(optics.optical_center_x_px)
            : optCxInput && !isNaN(parseFloat(optCxInput.value))
            ? parseFloat(optCxInput.value)
            : sensorW / 2.0;
    const optY =
        optics.optical_center_y_px !== undefined
            ? Number(optics.optical_center_y_px)
            : optCyInput && !isNaN(parseFloat(optCyInput.value))
            ? parseFloat(optCyInput.value)
            : sensorH / 2.0;
    const r90SensorPx =
        optics.horizon_radius_90_px !== undefined
            ? Number(optics.horizon_radius_90_px)
            : optR90Input && !isNaN(parseFloat(optR90Input.value))
            ? parseFloat(optR90Input.value)
            : Math.min(sensorW, sensorH) * 0.46;

    const northPaDeg =
        ori.north_position_angle_deg !== undefined
            ? Number(ori.north_position_angle_deg)
            : oriNpaInput && !isNaN(parseFloat(oriNpaInput.value))
            ? parseFloat(oriNpaInput.value)
            : 0.0;
    const tiltDeg =
        ori.zenith_tilt_deg !== undefined
            ? Number(ori.zenith_tilt_deg)
            : oriTiltInput && !isNaN(parseFloat(oriTiltInput.value))
            ? parseFloat(oriTiltInput.value)
            : 0.0;
    const tiltAzDeg =
        ori.azimuth_deg !== undefined
            ? Number(ori.azimuth_deg)
            : oriAzInput && !isNaN(parseFloat(oriAzInput.value))
            ? parseFloat(oriAzInput.value)
            : 180.0;

    // Compute rendered 4:3 image box inside viewportContainer (handles fullscreen letterboxing)
    const containerAspect = w / Math.max(1, h);
    const sensorAspect = sensorW / Math.max(1, sensorH);
    let renderW = w;
    let renderH = h;
    let offsetX = 0;
    let offsetY = 0;
    if (containerAspect > sensorAspect) {
        renderH = h;
        renderW = h * sensorAspect;
        offsetX = (w - renderW) / 2.0;
    } else {
        renderW = w;
        renderH = w / sensorAspect;
        offsetY = (h - renderH) / 2.0;
    }

    const scaleX = renderW / sensorW;
    const scaleY = renderH / sensorH;
    const scaleMean = (scaleX + scaleY) * 0.5;

    // Keep every overlay mark inside the displayed image (not in the letterbox bars)
    ctx.beginPath();
    ctx.rect(offsetX, offsetY, renderW, renderH);
    ctx.clip();

    // Native solver coordinates follow the configured image transform.  Imported solutions may
    // already be registered to displayed pixels; in that case applying it again mirrors the overlay.
    const tf = st.image_transform || {};
    const rot180 = Number(tf.rotate_deg || 0) % 360 === 180;
    const solutionIsDisplay = ori.coordinate_space === "display";
    const mirrorX = !solutionIsDisplay && (!!tf.flip_horizontal !== rot180);
    const mirrorY = !solutionIsDisplay && (!!tf.flip_vertical !== rot180);
    // Solver/optics coordinates are pixel indices (centre of pixel i at i); the canvas draws the picture with
    // pixel i covering [i, i+1), so add half a pixel, and mirror around the frame edge (index i -> W-1-i)
    const toImgX = (sx) => (mirrorX ? sensorW - sx - 0.5 : sx + 0.5);
    const toImgY = (sy) => (mirrorY ? sensorH - sy - 0.5 : sy + 0.5);

    const cxCanvas = offsetX + toImgX(optX) * scaleX;
    const cyCanvas = offsetY + toImgY(optY) * scaleY;
    const r90Canvas = r90SensorPx * scaleMean;

    const nowUtc = new Date();
    const lmstDeg = computeLmstDeg(lonDeg, nowUtc);
    const latRad = (latDeg * Math.PI) / 180.0;
    const sinLat = Math.sin(latRad);
    const cosLat = Math.cos(latRad);
    const northAngleRad = (northPaDeg * Math.PI) / 180.0;
    const tiltRad = (tiltDeg * Math.PI) / 180.0;
    const tiltAzRad = (tiltAzDeg * Math.PI) / 180.0;
    const cosTilt = Math.cos(tiltRad);
    const sinTilt = Math.sin(tiltRad);
    const cosTiltAz = Math.cos(tiltAzRad);
    const sinTiltAz = Math.sin(tiltAzRad);

    // Rectilinear (non-fisheye) lens solved by pointing: optical axis at (azimuth_deg, altitude_deg),
    // image "up" = local vertical rotated by the roll angle (same model as the bright-star solver)
    const hfovDeg = Number(optics.fov_horizontal_deg || 0);
    const lensProj = optics.lens_projection || (/gnomonic|rectilinear/i.test(String(optics.projection_type || "")) ? "rectilinear" : "");
    const isFisheyePointing = !!optics.pointing_model && lensProj === "equidistant" && Number(optics.focal_length_px) > 0;
    const isRectilinear = isFisheyePointing || (lensProj === "rectilinear" && hfovDeg > 1 && hfovDeg < 170);
    const distK = Number(optics.distortion_k || 0);
    let camX = null, camY = null, camZ = null, fPx = 0;
    if (isRectilinear) {
        const d2r = Math.PI / 180.0;
        const pAlt = Number(ori.altitude_deg !== undefined ? ori.altitude_deg : 90.0 - tiltDeg) * d2r;
        const pAz = tiltAzDeg * d2r;
        const roll = northPaDeg * d2r;
        fPx = Number(optics.focal_length_px) > 0
            ? Number(optics.focal_length_px)
            : (sensorW / 2.0) / Math.tan((hfovDeg / 2.0) * d2r);
        // ENU vectors [east, north, up]
        camZ = [Math.cos(pAlt) * Math.sin(pAz), Math.cos(pAlt) * Math.cos(pAz), Math.sin(pAlt)];
        let vert = [-camZ[2] * camZ[0], -camZ[2] * camZ[1], 1.0 - camZ[2] * camZ[2]];
        const vLen = Math.hypot(vert[0], vert[1], vert[2]) || 1;
        vert = vert.map((c) => c / vLen);
        const zxv = [
            camZ[1] * vert[2] - camZ[2] * vert[1],
            camZ[2] * vert[0] - camZ[0] * vert[2],
            camZ[0] * vert[1] - camZ[1] * vert[0],
        ];
        const up = vert.map((c, i) => c * Math.cos(roll) + zxv[i] * Math.sin(roll));
        camY = up.map((c) => -c); // image y grows downward
        camX = [
            camY[1] * camZ[2] - camY[2] * camZ[1],
            camY[2] * camZ[0] - camY[0] * camZ[2],
            camY[0] * camZ[1] - camY[1] * camZ[0],
        ];
    }

    // Project local Horizontal (altDeg, azDeg) -> Canvas (x, y)
    function projectAltAzToCanvas(altDeg, azDeg) {
        if (isRectilinear) {
            const a = (altDeg * Math.PI) / 180.0;
            const z = (azDeg * Math.PI) / 180.0;
            const v = [Math.cos(a) * Math.sin(z), Math.cos(a) * Math.cos(z), Math.sin(a)];
            const dot = (u) => u[0] * v[0] + u[1] * v[1] + u[2] * v[2];
            const cz = dot(camZ);
            const thetaRad = Math.acos(Math.max(-1, Math.min(1, cz)));
            const theta = (thetaRad * 180.0) / Math.PI;
            let sensorPx, sensorPy;
            if (isFisheyePointing) {
                // Equidistant fisheye with cubic distortion: r = f (theta + k theta^3)
                if (theta > 110) return { x: NaN, y: NaN, alt: altDeg, az: azDeg, theta, visible: false };
                const cx = dot(camX), cy = dot(camY);
                const rho = Math.hypot(cx, cy) || 1e-12;
                const r = fPx * (thetaRad + distK * thetaRad * thetaRad * thetaRad);
                sensorPx = optX + (r * cx) / rho;
                sensorPy = optY + (r * cy) / rho;
            } else {
                if (cz < 0.05) return { x: NaN, y: NaN, alt: altDeg, az: azDeg, theta, visible: false };
                const xn = dot(camX) / cz, yn = dot(camY) / cz;
                const sk = 1 + distK * (xn * xn + yn * yn); // radial distortion r = f t (1 + k t^2), same as bright_star_solver._project
                sensorPx = optX + fPx * xn * sk;
                sensorPy = optY + fPx * yn * sk;
            }
            const canvasX = offsetX + toImgX(sensorPx) * scaleX;
            const canvasY = offsetY + toImgY(sensorPy) * scaleY;
            const inViewport =
                canvasX >= offsetX - 10 && canvasX <= offsetX + renderW + 10 &&
                canvasY >= offsetY - 10 && canvasY <= offsetY + renderH + 10;
            return { x: canvasX, y: canvasY, alt: altDeg, az: azDeg, theta, visible: inViewport && altDeg >= -1.5 };
        }
        const altRad = (altDeg * Math.PI) / 180.0;
        const azRad = (azDeg * Math.PI) / 180.0;
        const cosAlt = Math.cos(altRad);
        const sinAlt = Math.sin(altRad);

        // 3D local horizon unit vector: N (+north), E (+east), Z (+zenith)
        let vN = cosAlt * Math.cos(azRad);
        let vE = cosAlt * Math.sin(azRad);
        let vZ = sinAlt;

        // Apply camera zenith tilt toward tiltAzDeg if non-zero
        if (Math.abs(tiltDeg) > 0.01) {
            const uPar = vN * cosTiltAz + vE * sinTiltAz;
            const uPerp = -vN * sinTiltAz + vE * cosTiltAz;
            const uParRot = uPar * cosTilt - vZ * sinTilt;
            vZ = uPar * sinTilt + vZ * cosTilt;
            vN = uParRot * cosTiltAz - uPerp * sinTiltAz;
            vE = uParRot * sinTiltAz + uPerp * cosTiltAz;
        }

        const thetaDeg = (Math.acos(Math.max(-1.0, Math.min(1.0, vZ))) * 180.0) / Math.PI;
        const rPx = r90SensorPx * (thetaDeg / 90.0);
        const camAzRad = Math.atan2(vE, vN) - northAngleRad;

        // Sky dome parity looking up: North = UP (-Y), East = LEFT (-X), South = DOWN (+Y), West = RIGHT (+X)
        const sensorPx = optX - rPx * Math.sin(camAzRad);
        const sensorPy = optY - rPx * Math.cos(camAzRad);

        const canvasX = offsetX + toImgX(sensorPx) * scaleX;
        const canvasY = offsetY + toImgY(sensorPy) * scaleY;
        const inDome = thetaDeg <= 93.0 && altDeg >= -1.5;
        const inViewport =
            canvasX >= offsetX - 10 &&
            canvasX <= offsetX + renderW + 10 &&
            canvasY >= offsetY - 10 &&
            canvasY <= offsetY + renderH + 10;

        return {
            x: canvasX,
            y: canvasY,
            alt: altDeg,
            az: azDeg,
            theta: thetaDeg,
            visible: inDome && inViewport,
        };
    }

    // Project Celestial J2000 (raDeg, decDeg) -> Canvas (x, y, alt, az, visible)
    // J2000 catalogue positions are precessed to the equinox of date first (~0.38 deg by 2026, ~6 px on the sensor);
    // the Sun and Moon formulas already give positions of date (ofDate = true)
    const precT = (nowUtc.getTime() / 86400000.0 + 2440587.5 - 2451545.0) / 36525.0;
    const precZeta = ((2306.2181 * precT + 0.30188 * precT * precT) / 3600.0) * (Math.PI / 180.0);
    const precZ = ((2306.2181 * precT + 1.09468 * precT * precT) / 3600.0) * (Math.PI / 180.0);
    const precTheta = ((2004.3109 * precT - 0.42665 * precT * precT) / 3600.0) * (Math.PI / 180.0);
    function projectRaDecToCanvas(raDeg0, decDeg0, ofDate = false) {
        let raDeg = raDeg0, decDeg = decDeg0;
        if (!ofDate) {
            const r = (raDeg0 * Math.PI) / 180.0, dc = (decDeg0 * Math.PI) / 180.0;
            const pa = Math.cos(dc) * Math.sin(r + precZeta);
            const pb = Math.cos(precTheta) * Math.cos(dc) * Math.cos(r + precZeta) - Math.sin(precTheta) * Math.sin(dc);
            const pc = Math.sin(precTheta) * Math.cos(dc) * Math.cos(r + precZeta) + Math.cos(precTheta) * Math.sin(dc);
            raDeg = ((((Math.atan2(pa, pb) + precZ) * 180.0) / Math.PI) % 360.0 + 360.0) % 360.0;
            decDeg = (Math.asin(Math.max(-1.0, Math.min(1.0, pc))) * 180.0) / Math.PI;
        }
        const haRad = ((((lmstDeg - raDeg) % 360.0) + 360.0) % 360.0) * (Math.PI / 180.0);
        const decRad = (decDeg * Math.PI) / 180.0;
        const sinDec = Math.sin(decRad);
        const cosDec = Math.cos(decRad);

        const sinAlt = Math.max(-1.0, Math.min(1.0, sinLat * sinDec + cosLat * cosDec * Math.cos(haRad)));
        const altRad = Math.asin(sinAlt);
        const altDeg = (altRad * 180.0) / Math.PI;

        const y = -Math.sin(haRad) * cosDec;
        const x = sinDec * cosLat - cosDec * sinLat * Math.cos(haRad);
        const azDeg = ((Math.atan2(y, x) * 180.0) / Math.PI + 360.0) % 360.0;

        return projectAltAzToCanvas(altDeg, azDeg);
    }

    // 1a. Rectilinear lens: altitude lines and azimuth ticks instead of fisheye rings
    if (showGrid && isRectilinear) {
        const polyline = (pts) => {
            let pen = false;
            ctx.beginPath();
            pts.forEach((p) => {
                if (!Number.isFinite(p.x) || p.theta > (isFisheyePointing ? 100 : 85)) { pen = false; return; }
                if (pen) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y);
                pen = true;
            });
            ctx.stroke();
        };
        ctx.save();
        for (let alt = 0; alt <= 75; alt += 15) {
            ctx.strokeStyle = alt === 0 ? "rgba(56, 189, 248, 0.6)" : "rgba(56, 189, 248, 0.25)";
            ctx.lineWidth = alt === 0 ? 1.5 : 1;
            ctx.setLineDash(alt === 0 ? [] : [4, 4]);
            const pts = [];
            for (let az = 0; az <= 360; az += 1) pts.push(projectAltAzToCanvas(alt, az));
            polyline(pts);
            const tag = projectAltAzToCanvas(alt, tiltAzDeg);
            if (alt > 0 && tag.visible) {
                ctx.fillStyle = "rgba(148, 163, 184, 0.8)";
                ctx.font = "10px monospace";
                ctx.fillText(`${alt}°`, tag.x + 4, tag.y - 3);
            }
        }
        ctx.setLineDash([]);
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 13px monospace";
        ctx.textAlign = "center";
        ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
        ctx.shadowBlur = 4;
        ["N", "NE", "E", "SE", "S", "SW", "W", "NW"].forEach((label, i) => {
            for (const alt of [2, 20, 40]) {
                const p = projectAltAzToCanvas(alt, i * 45);
                if (p.visible) { ctx.fillText(label, p.x, p.y - 6); break; }
            }
        });
        ctx.restore();
    }

    // 1. Draw Horizon, Altitude Circles & Compass Reticle
    if (showGrid && !isRectilinear) {
        // Unit direction (canvas space) from optical center toward a horizon azimuth
        const dirTo = (azDeg) => {
            const p = projectAltAzToCanvas(0.0, azDeg);
            const dx = p.x - cxCanvas;
            const dy = p.y - cyCanvas;
            const len = Math.hypot(dx, dy) || 1;
            return { x: dx / len, y: dy / len };
        };
        const dN = dirTo(0.0);
        const dE = dirTo(90.0);
        const dS = dirTo(180.0);
        const dW = dirTo(270.0);

        ctx.save();
        ctx.translate(cxCanvas, cyCanvas);

        // Outer Horizon Circle (0 deg altitude = R90)
        ctx.strokeStyle = "rgba(56, 189, 248, 0.45)";
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        ctx.arc(0, 0, r90Canvas, 0, 2 * Math.PI);
        ctx.stroke();

        // 30° and 60° altitude rings
        ctx.strokeStyle = "rgba(56, 189, 248, 0.25)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.arc(0, 0, r90Canvas * (60.0 / 90.0), 0, 2 * Math.PI);
        ctx.moveTo(r90Canvas * (30.0 / 90.0), 0);
        ctx.arc(0, 0, r90Canvas * (30.0 / 90.0), 0, 2 * Math.PI);
        ctx.stroke();

        // Cardinal N-S and E-W axes
        ctx.beginPath();
        ctx.moveTo(dN.x * r90Canvas, dN.y * r90Canvas);
        ctx.lineTo(dS.x * r90Canvas, dS.y * r90Canvas);
        ctx.moveTo(dE.x * r90Canvas, dE.y * r90Canvas);
        ctx.lineTo(dW.x * r90Canvas, dW.y * r90Canvas);
        ctx.stroke();
        ctx.setLineDash([]);

        // Altitude ring degree tags (along the North axis)
        ctx.fillStyle = "rgba(148, 163, 184, 0.75)";
        ctx.font = "10px monospace";
        ctx.textAlign = "left";
        ctx.fillText("60°", dN.x * r90Canvas * (30.0 / 90.0) + 4, dN.y * r90Canvas * (30.0 / 90.0) + 11);
        ctx.fillText("30°", dN.x * r90Canvas * (60.0 / 90.0) + 4, dN.y * r90Canvas * (60.0 / 90.0) + 11);

        // Cardinal direction labels follow PA and mirror/flip
        ctx.fillStyle = "#38bdf8";
        ctx.font = "bold 13px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
        ctx.shadowBlur = 4;
        const cardRadius = Math.min(r90Canvas - 14, Math.min(renderW, renderH) * 0.46);
        [[dN, "N"], [dS, "S"], [dE, "E"], [dW, "W"]].forEach(([d, label]) => {
            ctx.fillText(label, d.x * cardRadius, d.y * cardRadius);
        });
        ctx.restore();

        // Zenith marker (+)
        const zenPt = projectAltAzToCanvas(90.0, 0.0);
        ctx.save();
        ctx.strokeStyle = "rgba(250, 204, 21, 0.75)";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(zenPt.x - 6, zenPt.y);
        ctx.lineTo(zenPt.x + 6, zenPt.y);
        ctx.moveTo(zenPt.x, zenPt.y - 6);
        ctx.lineTo(zenPt.x, zenPt.y + 6);
        ctx.stroke();
        ctx.restore();
    }

    const catalog = window.ALLSKY_CATALOG || { constellations: [], stars: [], messier: [] };
    // The canvas sits in the CSS-zoomed stage: 1 screen px = u canvas px (keeps strokes and text screen-sized)
    const u = 1 / Math.max(1, view.zoom);
    const occupiedBoxes = [];

    // Collision test + register for a label box given by its top-left corner (canvas px)
    function placeBox(x, y, bw, bh, pad = 2 * u) {
        const box = { x1: x - pad, y1: y - pad, x2: x + bw + pad, y2: y + bh + pad };
        if (box.x1 < offsetX + 4 * u || box.x2 > offsetX + renderW - 4 * u || box.y1 < offsetY + 4 * u || box.y2 > offsetY + renderH - 4 * u) {
            return false;
        }
        for (let i = 0; i < occupiedBoxes.length; i++) {
            const b = occupiedBoxes[i];
            if (!(box.x2 < b.x1 || box.x1 > b.x2 || box.y2 < b.y1 || box.y1 > b.y2)) return false;
        }
        occupiedBoxes.push(box);
        return true;
    }
    // Reserve the top-left summary badge so labels avoid it
    occupiedBoxes.push({ x1: offsetX, y1: offsetY, x2: offsetX + 560 * u, y2: offsetY + 36 * u });

    // Equatorial coordinate grid: hour circles (right ascension, every 1 h) and parallels (declination, every 15°)
    if (showEqGrid) {
        ctx.save();
        ctx.lineJoin = "round";
        const fontPx = 10 * u;
        ctx.font = `${fontPx}px monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const eqLine = (pts, major, label) => {
            // Split into runs of visible points so the line never jumps across the horizon or the frame edge
            let run = [];
            const runs = [];
            pts.forEach((pt) => {
                if (pt && pt.visible) run.push(pt);
                else { if (run.length > 1) runs.push(run); run = []; }
            });
            if (run.length > 1) runs.push(run);
            ctx.strokeStyle = major ? "rgba(251, 191, 36, 0.55)" : "rgba(251, 191, 36, 0.30)";
            ctx.lineWidth = (major ? 1.3 : 1) * u;
            ctx.setLineDash([5 * u, 4 * u]);
            runs.forEach((r) => {
                ctx.beginPath();
                r.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
                ctx.stroke();
            });
            ctx.setLineDash([]);
            if (!label || !runs.length) return;
            const best = runs.reduce((a, b) => (b.length > a.length ? b : a));
            const mid = best[Math.floor(best.length / 2)];
            const tw = ctx.measureText(label).width;
            if (!placeBox(mid.x - tw / 2, mid.y - fontPx / 2, tw, fontPx)) return;
            ctx.fillStyle = "rgba(251, 191, 36, 0.9)";
            ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
            ctx.shadowBlur = 3 * u;
            ctx.fillText(label, mid.x, mid.y);
            ctx.shadowBlur = 0;
        };
        // Parallels of declination
        for (let dec = -75; dec <= 75; dec += 15) {
            const pts = [];
            for (let ra = 0; ra <= 360; ra += 2) pts.push(projectRaDecToCanvas(ra, dec));
            eqLine(pts, dec === 0, `${dec > 0 ? "+" : ""}${dec}°`);
        }
        // Hour circles of right ascension
        for (let h = 0; h < 24; h++) {
            const pts = [];
            for (let dec = -88; dec <= 88; dec += 2) pts.push(projectRaDecToCanvas(h * 15, dec));
            eqLine(pts, false, `${h}h`);
        }
        // Celestial poles
        [[90, "NCP"], [-90, "SCP"]].forEach(([dec, name]) => {
            const pt = projectRaDecToCanvas(0, dec);
            if (!pt || !pt.visible) return;
            ctx.strokeStyle = "rgba(251, 191, 36, 0.9)";
            ctx.lineWidth = 1.4 * u;
            ctx.beginPath();
            ctx.arc(pt.x, pt.y, 5 * u, 0, 2 * Math.PI);
            ctx.stroke();
            ctx.fillStyle = "rgba(251, 191, 36, 0.95)";
            ctx.fillText(name, pt.x, pt.y - 11 * u);
        });
        ctx.restore();
    }

    const roundedRect = (x, y, rw, rh, r) => {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + rw, y, x + rw, y + rh, r);
        ctx.arcTo(x + rw, y + rh, x, y + rh, r);
        ctx.arcTo(x, y + rh, x, y, r);
        ctx.arcTo(x, y, x + rw, y, r);
        ctx.closePath();
    };

    // Bright-star lookup so stick-figure vertices know their magnitude (line weight, reticle rim gap)
    const starVertices = new Map();
    const vertexKey = (ra, dec) => `${Math.round(ra * 10)},${Math.round(dec * 10)}`;
    (catalog.stars || []).forEach((s) => {
        const k = vertexKey(s.ra, s.dec);
        if (!starVertices.has(k)) starVertices.set(k, s);
    });
    const findVertexStar = (ra, dec) => {
        const r0 = Math.round(ra * 10), d0 = Math.round(dec * 10);
        for (let dr = -2; dr <= 2; dr++) {
            for (let dd = -2; dd <= 2; dd++) {
                const s = starVertices.get(`${r0 + dr},${d0 + dd}`);
                if (s && Math.abs(s.ra - ra) < 0.25 && Math.abs(s.dec - dec) < 0.25) return s;
            }
        }
        return null;
    };
    const starWeight = (mag) => Math.max(0.24, Math.min(1.0, (5.2 - mag) / 4.5));
    const reticleRadius = (mag) => (Math.max(1.4, Math.min(4.8, 3.8 - mag * 0.65)) + 4) * u;

    // Bi-tapered, magnitude-weighted constellation filament (after the remote plate-solving viewer):
    // anchored at each star's reticle rim, a soft bloom, a gradient body and a fine core, screen-blended
    const lw = 1.6;
    function drawFilament(v1, v2) {
        const dx = v2.x - v1.x;
        const dy = v2.y - v1.y;
        const segLen = Math.hypot(dx, dy);
        if (segLen / u < 7) return;
        const ux = dx / segLen, uy = dy / segLen;
        const nx = -uy, ny = ux;
        const b1 = starWeight(v1.mag), b2 = starWeight(v2.mag);
        const bAvg = Math.sqrt(b1 * b2);
        const g1 = Math.min(segLen * 0.26, v1.rRing + 0.15 * u);
        const g2 = Math.min(segLen * 0.26, v2.rRing + 0.15 * u);
        const x1 = v1.x + ux * g1, y1 = v1.y + uy * g1;
        const x2 = v2.x - ux * g2, y2 = v2.y - uy * g2;
        const effLen = Math.hypot(x2 - x1, y2 - y1);
        const effScreen = effLen / u;
        if (effLen <= 1e-2 || effScreen < 2.5) return;

        // Short spans stay fine, long spans stay strong but slightly softer
        const distW = effScreen < 36 ? 0.68 + (effScreen / 36) * 0.32 : effScreen > 180 ? Math.max(0.88, 1.0 - (effScreen - 180) / 1100) : 1.0;
        const distA = effScreen > 160 ? Math.max(0.84, 1.0 - (effScreen - 160) / 900) : 1.0;
        const taperLen = Math.min(effLen * 0.28, Math.max(4.5, Math.min(17.0, Math.sqrt(effScreen) * 1.12)) * u);
        const tStop = Math.max(0.04, Math.min(0.24, taperLen / effLen));
        const tx1 = x1 + ux * taperLen, ty1 = y1 + uy * taperLen;
        const tx2 = x2 - ux * taperLen, ty2 = y2 - uy * taperLen;
        const midX = (x1 + x2) * 0.5, midY = (y1 + y2) * 0.5;

        const ribbon = (tipW1, bodyW1, midW, bodyW2, tipW2) => {
            ctx.beginPath();
            ctx.moveTo(x1 + nx * tipW1, y1 + ny * tipW1);
            ctx.quadraticCurveTo(tx1 + nx * bodyW1, ty1 + ny * bodyW1, midX + nx * midW, midY + ny * midW);
            ctx.quadraticCurveTo(tx2 + nx * bodyW2, ty2 + ny * bodyW2, x2 + nx * tipW2, y2 + ny * tipW2);
            ctx.lineTo(x2 - nx * tipW2, y2 - ny * tipW2);
            ctx.quadraticCurveTo(tx2 - nx * bodyW2, ty2 - ny * bodyW2, midX - nx * midW, midY - ny * midW);
            ctx.quadraticCurveTo(tx1 - nx * bodyW1, ty1 - ny * bodyW1, x1 - nx * tipW1, y1 - ny * tipW1);
            ctx.closePath();
        };

        const wBase = distW * lw * u;
        const tipW1 = (0.14 + 0.12 * b1) * wBase, tipW2 = (0.14 + 0.12 * b2) * wBase;
        const bodyW1 = (0.30 + 0.34 * b1) * wBase, bodyW2 = (0.30 + 0.34 * b2) * wBase;
        const midW = (0.34 + 0.36 * bAvg) * wBase * (0.88 + 0.12 * distA);
        const aTip1 = Math.min(0.68, (0.20 + 0.24 * b1) * distA);
        const aShld1 = Math.min(0.76, (0.38 + 0.28 * b1) * distA);
        const aMid = Math.min(0.80, (0.42 + 0.28 * bAvg) * distA);
        const aShld2 = Math.min(0.76, (0.38 + 0.28 * b2) * distA);
        const aTip2 = Math.min(0.68, (0.20 + 0.24 * b2) * distA);

        ctx.save();
        ctx.globalCompositeOperation = "screen";
        // 1) soft bloom
        const glow = ctx.createLinearGradient(x1, y1, x2, y2);
        glow.addColorStop(0, `rgba(56, 189, 248, ${(aTip1 * 0.16).toFixed(3)})`);
        glow.addColorStop(tStop, `rgba(56, 189, 248, ${(aShld1 * 0.22).toFixed(3)})`);
        glow.addColorStop(0.5, `rgba(125, 211, 252, ${(aMid * 0.25).toFixed(3)})`);
        glow.addColorStop(1 - tStop, `rgba(56, 189, 248, ${(aShld2 * 0.22).toFixed(3)})`);
        glow.addColorStop(1, `rgba(56, 189, 248, ${(aTip2 * 0.16).toFixed(3)})`);
        ribbon(tipW1 * 2.15, bodyW1 * 2.25, midW * 2.35, bodyW2 * 2.25, tipW2 * 2.15);
        ctx.fillStyle = glow;
        ctx.fill();
        // 2) tapered body
        const body = ctx.createLinearGradient(x1, y1, x2, y2);
        body.addColorStop(0, `rgba(56, 189, 248, ${aTip1.toFixed(3)})`);
        body.addColorStop(tStop, `rgba(125, 211, 252, ${aShld1.toFixed(3)})`);
        body.addColorStop(0.5, `rgba(186, 230, 253, ${aMid.toFixed(3)})`);
        body.addColorStop(1 - tStop, `rgba(125, 211, 252, ${aShld2.toFixed(3)})`);
        body.addColorStop(1, `rgba(56, 189, 248, ${aTip2.toFixed(3)})`);
        ribbon(tipW1, bodyW1, midW, bodyW2, tipW2);
        ctx.fillStyle = body;
        ctx.fill();
        // 3) fine continuous core reaching both rims
        ctx.strokeStyle = body;
        ctx.lineWidth = (0.34 + 0.28 * bAvg) * wBase;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
        // 4) contact glint on prominent rims
        if (effScreen >= 24) {
            [[b1, x1, y1], [b2, x2, y2]].forEach(([b, gx, gy]) => {
                if (b < 0.42) return;
                ctx.fillStyle = `rgba(186, 230, 253, ${(0.28 + 0.26 * b).toFixed(3)})`;
                ctx.beginPath();
                ctx.arc(gx, gy, (0.36 + 0.24 * b) * wBase, 0, Math.PI * 2);
                ctx.fill();
            });
        }
        ctx.restore();
    }

    let visibleConstellationsCount = 0;
    let visibleStarsCount = 0;

    // 2. IAU constellation stick figures (one filament per unique edge) & visible constellation centroids
    const visibleConstellationLabels = [];
    if ((showSky || showLabels) && Array.isArray(catalog.constellations)) {
        const maxSegLen = isRectilinear ? renderW * (isFisheyePointing ? 0.35 : 0.6) : r90Canvas * 0.65;
        const drawnEdges = new Set();
        const resolveVertex = (ra, dec) => {
            const p = projectRaDecToCanvas(ra, dec);
            const star = findVertexStar(ra, dec);
            const mag = star ? Number(star.mag) : 3.8;
            p.mag = mag;
            p.rRing = star && mag <= 3.0 ? reticleRadius(mag) : 2.8 * u;
            return p;
        };

        catalog.constellations.forEach((con) => {
            let sumX = 0;
            let sumY = 0;
            let countPts = 0;
            let anySegmentVisible = false;

            if (Array.isArray(con.lines)) {
                con.lines.forEach((seg) => {
                    if (!Array.isArray(seg) || seg.length < 2) return;
                    const p1 = resolveVertex(seg[0][0], seg[0][1]);
                    const p2 = resolveVertex(seg[1][0], seg[1][1]);
                    if (p1.alt >= 0.0 && p2.alt >= 0.0 && (p1.visible || p2.visible)) {
                        const segDist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
                        if (segDist <= maxSegLen) {
                            anySegmentVisible = true;
                            const k1 = `${Math.round(p1.x * 2)},${Math.round(p1.y * 2)}`;
                            const k2 = `${Math.round(p2.x * 2)},${Math.round(p2.y * 2)}`;
                            const eKey = k1 < k2 ? `${k1}|${k2}` : `${k2}|${k1}`;
                            if (showSky && !drawnEdges.has(eKey)) {
                                drawnEdges.add(eKey);
                                drawFilament(p1, p2);
                            }
                            sumX += p1.x + p2.x;
                            sumY += p1.y + p2.y;
                            countPts += 2;
                        }
                    }
                });
            }

            const centerProj = projectRaDecToCanvas(con.ra, con.dec);
            if (anySegmentVisible || (centerProj.visible && centerProj.alt >= 8.0)) {
                visibleConstellationsCount++;
                const labelX = countPts > 0 ? sumX / countPts : centerProj.x;
                const labelY = countPts > 0 ? sumY / countPts : centerProj.y;
                if (centerProj.alt >= 5.0 || countPts >= 4) {
                    visibleConstellationLabels.push({
                        id: con.id,
                        name: con.name,
                        common: con.common || "",
                        x: labelX,
                        y: labelY,
                        alt: centerProj.alt,
                    });
                }
            }
        });
    }

    // 3. Messier deep-sky objects: screen-blended diamond with a soft halo
    const visibleMessier = [];
    if ((showSky || showLabels) && Array.isArray(catalog.messier)) {
        ctx.save();
        ctx.globalCompositeOperation = "screen";
        catalog.messier.forEach((dso) => {
            const pt = projectRaDecToCanvas(dso.ra, dso.dec);
            if (!pt.visible || pt.alt < 5.0) return;
            visibleMessier.push({ ...dso, x: pt.x, y: pt.y, alt: pt.alt });
            if (showSky) hits.push({ kind: "dso", id: dso.id, name: dso.name, mag: dso.mag, ra: dso.ra, dec: dso.dec, x: pt.x, y: pt.y, alt: pt.alt, az: pt.az });
            if (showSky) {
                const r = 4.5 * u;
                const diamond = () => {
                    ctx.beginPath();
                    ctx.moveTo(pt.x, pt.y - r);
                    ctx.lineTo(pt.x + r, pt.y);
                    ctx.lineTo(pt.x, pt.y + r);
                    ctx.lineTo(pt.x - r, pt.y);
                    ctx.closePath();
                };
                ctx.strokeStyle = "rgba(253, 224, 71, 0.14)";
                ctx.lineWidth = 2.6 * u;
                diamond();
                ctx.stroke();
                ctx.strokeStyle = "rgba(253, 224, 71, 0.62)";
                ctx.lineWidth = 0.8 * u;
                diamond();
                ctx.stroke();
            }
        });
        ctx.restore();
    }

    // 4. Bright stars: four-arc reticles (open, so the real star stays visible), gold + ticks for the brightest
    const visibleBrightStars = [];
    if ((showSky || showLabels) && Array.isArray(catalog.stars)) {
        ctx.save();
        ctx.globalCompositeOperation = "screen";
        const gapRad = 0.28;
        const arcs = (x, y, r) => {
            for (let q = 0; q < 4; q++) {
                ctx.beginPath();
                ctx.arc(x, y, r, q * (Math.PI / 2) + gapRad, (q + 1) * (Math.PI / 2) - gapRad);
                ctx.stroke();
            }
        };
        catalog.stars.concat(window.ALLSKY_PLANETS || []).forEach((star) => {
            const pt = projectRaDecToCanvas(star.ra, star.dec);
            if (!pt.visible || pt.alt < 1.0) return;
            visibleStarsCount++;

            const mag = Number(star.mag !== undefined ? star.mag : 3.0);
            const rRing = reticleRadius(mag);
            const prominent = mag <= 1.5;
            const hasTicks = mag <= 1.0;
            if ((showSky && mag <= 3.0) || (showLabels && star.name && mag <= 2.55)) {
                hits.push({ kind: star.planet ? "planet" : "star", name: star.name, bayer: star.bayer, con: star.con, mag, ra: star.ra, dec: star.dec, x: pt.x, y: pt.y, alt: pt.alt, az: pt.az });
            }

            if (showSky && mag <= 3.0) {
                const b = Math.max(0.32, starWeight(mag));
                ctx.strokeStyle = prominent ? "rgba(253, 224, 71, 0.12)" : "rgba(56, 189, 248, 0.10)";
                ctx.lineWidth = (prominent ? 1.85 : 1.45) * lw * u;
                arcs(pt.x, pt.y, rRing);
                ctx.strokeStyle = prominent
                    ? `rgba(253, 224, 71, ${(0.40 + 0.18 * b).toFixed(2)})`
                    : `rgba(125, 211, 252, ${(0.28 + 0.22 * b).toFixed(2)})`;
                ctx.lineWidth = (prominent ? 0.68 + 0.24 * b : 0.48 + 0.22 * b) * lw * u;
                arcs(pt.x, pt.y, rRing);
                if (hasTicks) {
                    const tIn = rRing + 1.2 * u, tOut = rRing + 3.6 * u;
                    ctx.beginPath();
                    ctx.moveTo(pt.x - tOut, pt.y); ctx.lineTo(pt.x - tIn, pt.y);
                    ctx.moveTo(pt.x + tIn, pt.y); ctx.lineTo(pt.x + tOut, pt.y);
                    ctx.moveTo(pt.x, pt.y - tOut); ctx.lineTo(pt.x, pt.y - tIn);
                    ctx.moveTo(pt.x, pt.y + tIn); ctx.lineTo(pt.x, pt.y + tOut);
                    ctx.stroke();
                }
            }

            if (star.name && mag <= 2.55) {
                visibleBrightStars.push({
                    name: star.name,
                    bayer: star.bayer || "",
                    mag: mag,
                    x: pt.x,
                    y: pt.y,
                    rRing: rRing,
                    prominent: prominent,
                    hasTicks: hasTicks,
                });
            }
        });
        ctx.restore();
    }

    // 5. Labels with collision avoidance (constellation pill badges, star names with leader filaments, DSOs)
    if (showLabels) {
        ctx.save();
        ctx.textBaseline = "middle";

        // 5a. Bright star names first (sorted brightest first): try 8 positions just outside the reticle
        visibleBrightStars.sort((a, b) => a.mag - b.mag);
        visibleBrightStars.forEach((s) => {
            const fs = (s.prominent ? 11.2 : 10.0) * u;
            ctx.font = `${s.prominent ? 600 : 500} ${fs}px Inter, system-ui, sans-serif`;
            const tw = ctx.measureText(s.name).width;
            const th = 14 * u;
            const rim = s.rRing + (s.hasTicks ? 4.2 : 1.0) * u;
            const dist = rim + 6.5 * u;
            const diag = rim * 0.72 + 6.0 * u;
            const cands = [
                { bx: s.x + dist, by: s.y - th / 2, tx: s.x + dist, ty: s.y, align: "left", ux: 1, uy: 0 },
                { bx: s.x + diag, by: s.y - diag - th + 2 * u, tx: s.x + diag, ty: s.y - diag - th / 2 + 2 * u, align: "left", ux: 0.707, uy: -0.707 },
                { bx: s.x + diag, by: s.y + diag - 2 * u, tx: s.x + diag, ty: s.y + diag + th / 2 - 2 * u, align: "left", ux: 0.707, uy: 0.707 },
                { bx: s.x - dist - tw, by: s.y - th / 2, tx: s.x - dist, ty: s.y, align: "right", ux: -1, uy: 0 },
                { bx: s.x - diag - tw, by: s.y - diag - th + 2 * u, tx: s.x - diag, ty: s.y - diag - th / 2 + 2 * u, align: "right", ux: -0.707, uy: -0.707 },
                { bx: s.x - diag - tw, by: s.y + diag - 2 * u, tx: s.x - diag, ty: s.y + diag + th / 2 - 2 * u, align: "right", ux: -0.707, uy: 0.707 },
                { bx: s.x - tw / 2, by: s.y - dist - th, tx: s.x, ty: s.y - dist - th / 2, align: "center", ux: 0, uy: -1 },
                { bx: s.x - tw / 2, by: s.y + dist, tx: s.x, ty: s.y + dist + th / 2, align: "center", ux: 0, uy: 1 },
            ];
            for (const c of cands) {
                if (!placeBox(c.bx, c.by, tw, th)) continue;
                // Subtle leader filament from just outside the reticle rim toward the label
                ctx.save();
                ctx.globalCompositeOperation = "screen";
                ctx.strokeStyle = s.prominent ? "rgba(253, 224, 71, 0.28)" : "rgba(125, 211, 252, 0.25)";
                ctx.lineWidth = 0.65 * u;
                ctx.beginPath();
                ctx.moveTo(s.x + c.ux * (rim + 1.0 * u), s.y + c.uy * (rim + 1.0 * u));
                ctx.lineTo(s.x + c.ux * (rim + 4.5 * u), s.y + c.uy * (rim + 4.5 * u));
                ctx.stroke();
                ctx.restore();
                ctx.fillStyle = s.prominent ? "rgba(254, 243, 199, 0.88)" : "rgba(224, 242, 254, 0.80)";
                ctx.shadowColor = "rgba(2, 6, 23, 0.72)";
                ctx.shadowBlur = 4;
                ctx.textAlign = c.align;
                ctx.fillText(s.name, c.tx, c.ty);
                ctx.shadowBlur = 0;
                break;
            }
        });

        // 5b. Constellation names as translucent pill badges (highest altitude first), nudged around the centroid
        visibleConstellationLabels.sort((a, b) => b.alt - a.alt);
        visibleConstellationLabels.forEach((con) => {
            const labelText = con.common ? `${con.name.toUpperCase()} · ${con.common}` : con.name.toUpperCase();
            const fs = 10.5 * u;
            ctx.font = `600 ${fs}px Inter, system-ui, sans-serif`;
            const tw = ctx.measureText(labelText).width;
            const padX = 7 * u;
            const bw = tw + padX * 2;
            const bh = 18 * u;
            const offs = [[0, 0], [0, -22], [0, 22], [-40, 0], [40, 0], [0, -44], [0, 44]];
            for (const [ox, oy] of offs) {
                const bx = con.x - bw / 2 + ox * u;
                const by = con.y - bh / 2 + oy * u;
                if (!placeBox(bx, by, bw, bh)) continue;
                roundedRect(bx, by, bw, bh, 5 * u);
                hits.push({ kind: "con", id: con.id, name: con.name, common: con.common, alt: con.alt, x: bx + bw / 2, y: by + bh / 2, hw: bw / 2, hh: bh / 2 });
                ctx.fillStyle = "rgba(15, 23, 42, 0.46)";
                ctx.fill();
                ctx.strokeStyle = "rgba(56, 189, 248, 0.30)";
                ctx.lineWidth = 0.75 * u;
                ctx.stroke();
                ctx.fillStyle = "rgba(186, 230, 253, 0.88)";
                ctx.textAlign = "center";
                ctx.shadowColor = "rgba(2, 6, 23, 0.75)";
                ctx.shadowBlur = 3;
                ctx.fillText(labelText, bx + bw / 2, by + bh / 2 + 0.5 * u);
                ctx.shadowBlur = 0;
                break;
            }
        });

        // 5c. Messier labels (M45, M42, M31, M44, ...)
        ctx.font = `600 ${9.5 * u}px Inter, system-ui, sans-serif`;
        ctx.textAlign = "left";
        ctx.shadowColor = "rgba(2, 6, 23, 0.72)";
        visibleMessier.forEach((dso) => {
            const labelText = `${dso.id} ${dso.name}`;
            const tw = ctx.measureText(labelText).width;
            const th = 12 * u;
            const lx = dso.x + 8 * u;
            if (placeBox(lx, dso.y - th / 2, tw, th)) {
                ctx.fillStyle = "rgba(254, 240, 138, 0.85)";
                ctx.shadowBlur = 3;
                ctx.fillText(labelText, lx, dso.y);
                ctx.shadowBlur = 0;
            }
        });

        ctx.restore();
    }

    // 5d. Sun and Moon (drawn even when the sky catalog is hidden: they are the brightest things in view)
    {
        const sm = sunMoonEquatorial(nowUtc);
        const bodies = [
            { key: "sun", label: "Sun", pos: sm.sun, ring: "rgba(250, 204, 21, 0.95)", text: "rgba(254, 240, 138, 0.95)", r: 11 },
            { key: "moon", label: `Moon ${Math.round(sm.moon.illum * 100)}%`, pos: sm.moon, ring: "rgba(226, 232, 240, 0.9)", text: "rgba(241, 245, 249, 0.95)", r: 9 },
        ];
        ctx.save();
        ctx.font = "600 11px Inter, system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.shadowColor = "rgba(2, 6, 23, 0.95)";
        ctx.shadowBlur = 4;
        bodies.forEach((b) => {
            const pt = projectRaDecToCanvas(b.pos.ra, b.pos.dec, true);
            if (!pt.visible) return;
            ctx.strokeStyle = b.ring;
            ctx.lineWidth = 1.6;
            ctx.beginPath();
            ctx.arc(pt.x, pt.y, b.r, 0, 2 * Math.PI);
            ctx.stroke();
            if (b.key === "sun") {
                for (let i = 0; i < 8; i++) {
                    const a = (i * Math.PI) / 4;
                    ctx.beginPath();
                    ctx.moveTo(pt.x + Math.cos(a) * (b.r + 3), pt.y + Math.sin(a) * (b.r + 3));
                    ctx.lineTo(pt.x + Math.cos(a) * (b.r + 8), pt.y + Math.sin(a) * (b.r + 8));
                    ctx.stroke();
                }
            }
            hits.push({ kind: b.key, name: b.key === "sun" ? "Sun" : "Moon", illum: b.key === "moon" ? sm.moon.illum : null, x: pt.x, y: pt.y, alt: pt.alt, az: pt.az });
            ctx.fillStyle = b.text;
            ctx.fillText(`${b.label} ${pt.alt.toFixed(1)}°`, pt.x + b.r + 10, pt.y + 4);
        });
        ctx.restore();
    }

    // 6. Draw Top-Left Astrometric Overlay Summary Badge (not on a phone: it would cover the picture)
    if (viewportContainer && viewportContainer.clientWidth < 600) return;
    if (chkOverlayText && !chkOverlayText.checked) return;   // Layers sheet: Overlay text off
    ctx.save();
    const badgeText = `LST ${formatLstHms(lmstDeg)} • ${visibleConstellationsCount} constellations • ${visibleStarsCount} stars • Center (${optX.toFixed(0)}, ${optY.toFixed(0)}) ${isRectilinear ? `FOV ${hfovDeg.toFixed(1)}° Az ${tiltAzDeg.toFixed(1)}°` : `R90=${r90SensorPx.toFixed(0)}px`} PA=${northPaDeg.toFixed(1)}°`;
    ctx.font = "11px monospace";
    // Below the capture info (time / exposure) rather than under it; two lines when the picture is too narrow for one
    const infoBottom = liveFrameInfo && !liveFrameInfo.hidden ? liveFrameInfo.offsetTop + liveFrameInfo.offsetHeight + 6 : 0;
    const cut = badgeText.indexOf(" • Center");
    const oneLine = ctx.measureText(badgeText).width + 16 <= renderW - 20 || cut < 0;
    const lines = oneLine ? [badgeText] : [badgeText.slice(0, cut), badgeText.slice(cut + 3)];
    const bw = Math.max(...lines.map((t) => ctx.measureText(t).width)) + 16;
    const bh = 8 + 14 * lines.length;
    const bx = offsetX + 10;
    const by = Math.max(offsetY + 10, infoBottom);
    ctx.fillStyle = "rgba(2, 6, 23, 0.72)";
    ctx.strokeStyle = "rgba(56, 189, 248, 0.35)";
    ctx.lineWidth = 1;
    ctx.fillRect(bx, by, bw, bh);
    ctx.strokeRect(bx, by, bw, bh);
    ctx.fillStyle = "#e2e8f0";
    ctx.textAlign = "left";
    lines.forEach((t, i) => ctx.fillText(t, bx + 8, by + 15 + 14 * i));
    ctx.restore();
}
