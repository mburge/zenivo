/* Zenivo dashboard (part of the former app.js): Exposure/gain controls and master dark calibration UI.
 * Classic script: top-level declarations are shared with the other src/web/app/*.js files; load order is fixed in index.html. */

// ------------------------------------------------------------------
// The #exposure-card lives on Live only, under the picture: collapsed to one row, open/closed remembered.
// Admin changes it; a guest sees the live values with every control disabled and a sign-in line (the server refuses guests anyway).
// ------------------------------------------------------------------
const expCard = document.getElementById("exposure-card");
if (expCard) {
    const EXP_OPEN_KEY = "zenivo.expOpen";
    try { expCard.open = localStorage.getItem(EXP_OPEN_KEY) === "1"; } catch (e) { /* storage blocked */ }
    expCard.addEventListener("toggle", () => {
        try { localStorage.setItem(EXP_OPEN_KEY, expCard.open ? "1" : "0"); } catch (e) { /* storage blocked */ }
    });
}
function applyExposureAccess() {
    if (!expCard) return;
    const admin = !!(window.zenivoAuth && window.zenivoAuth.admin);
    expCard.classList.toggle("exp-readonly", !admin);
    expCard.querySelectorAll('input[type="range"], .btn-lock, .chip-preset, .btn-preset, .btn-preset-gain').forEach((el) => {
        el.disabled = !admin;
        if (admin) el.removeAttribute("aria-disabled"); else el.setAttribute("aria-disabled", "true");
    });
    const link = document.getElementById("exp-signin-link");
    if (link) link.setAttribute("href", "/login?next=" + encodeURIComponent(location.pathname + location.hash));
}
document.addEventListener("zenivo-auth", applyExposureAccess);
applyExposureAccess();

// ------------------------------------------------------------------
// Range Controls Synchronization
// ------------------------------------------------------------------
rangeExposure.addEventListener("input", (e) => {
    const val = parseFloat(e.target.value);
    valExposure.textContent = val < 0.01 ? (val * 1000).toFixed(2) + " ms" : val < 0.1 ? (val * 1000).toFixed(1) + " ms" : val.toFixed(3) + " s";
});

rangeGain.addEventListener("input", (e) => {
    const val = parseFloat(e.target.value);
    valGain.textContent = val.toFixed(1) + " dB";
});

document.querySelectorAll(".btn-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
        const exp = parseFloat(btn.getAttribute("data-exp"));
        rangeExposure.value = exp;
        valExposure.textContent = exp < 0.01 ? (exp * 1000).toFixed(2) + " ms" : exp < 0.1 ? (exp * 1000).toFixed(1) + " ms" : exp.toFixed(3) + " s";
        rangeExposure.dispatchEvent(new Event("change"));
    });
});

document.querySelectorAll(".btn-preset-gain").forEach((btn) => {
    btn.addEventListener("click", () => {
        const gain = parseFloat(btn.getAttribute("data-gain"));
        rangeGain.value = gain;
        valGain.textContent = gain.toFixed(1) + " dB";
        rangeGain.dispatchEvent(new Event("change"));
    });
});

// Exposure scenario chips: Moonless / Twilight / Full moon set shutter + gain and lock both; Custom leaves the sliders alone
const presetChips = document.querySelectorAll(".chip-preset");
const markPreset = (name) => presetChips.forEach((c) => c.classList.toggle("active", c.dataset.preset === name));
presetChips.forEach((chip) => {
    chip.addEventListener("click", () => {
        markPreset(chip.dataset.preset);
        if (chip.dataset.preset === "custom") return;
        const clamp = (v, el) => Math.min(parseFloat(el.max), Math.max(parseFloat(el.min), v));
        rangeExposure.value = clamp(parseFloat(chip.dataset.exp), rangeExposure);
        rangeGain.value = clamp(parseFloat(chip.dataset.gain), rangeGain);
        rangeExposure.dispatchEvent(new Event("input"));
        rangeGain.dispatchEvent(new Event("input"));
        setAeLock("both");
        markPreset(chip.dataset.preset);
    });
});
[rangeExposure, rangeGain].forEach((el) => el.addEventListener("pointerdown", () => markPreset("custom")));
// Exposure / gain locks replace the Auto/Manual switch: nothing locked = Auto computes both,
// one locked = Auto adjusts only the other, both locked = fixed (manual) exposure and gain
const isLocked = (knob) => state.aeLock === knob || state.aeLock === "both";
const lockName = (exp, gain) => (exp && gain ? "both" : exp ? "exposure" : gain ? "gain" : "none");
// The click paints the new state at once (the server may answer only after the running exposure);
// the button pulses until the server confirms, and falls back to the old state when the request fails
const lockBusy = (on) => [btnLockExposure, btnLockGain, btnLockTarget].forEach((b) => {
    b.classList.toggle("pending", on);
    if (on) b.setAttribute("aria-busy", "true"); else b.removeAttribute("aria-busy");
});
async function setAeLock(lock) {
    const exposure_s = parseFloat(rangeExposure.value);
    const gain_db = parseFloat(rangeGain.value);
    const before = state.aeLock;
    const seq = (state.lockSeq = (state.lockSeq || 0) + 1);
    state.lockPending = true;
    state.aeLock = lock === "none" ? null : lock;
    paintLocks();
    lockBusy(true);
    try {
        const res = await fetch("/api/camera/ae-lock", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ lock, value: lock === "gain" ? gain_db : exposure_s, exposure_s, gain_db }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        if (seq !== state.lockSeq) return;   // a newer click is on its way: its answer wins
        state.lockPending = false;
        state.mode = data.mode;
        syncAeLock(data.ae_lock);
    } catch (err) {
        if (seq !== state.lockSeq) return;
        state.lockPending = false;
        state.aeLock = before;
        paintLocks();
        showAlert(controlAlert, `Could not change the lock: ${err.message}`, "error");
    } finally {
        if (seq === state.lockSeq) lockBusy(false);
    }
}
btnLockExposure.addEventListener("click", () => setAeLock(lockName(!isLocked("exposure"), isLocked("gain"))));
btnLockGain.addEventListener("click", () => setAeLock(lockName(isLocked("exposure"), !isLocked("gain"))));
// Grabbing a slider locks it at once (telemetry stops moving it); releasing it, or picking a preset,
// sends the lock with the chosen value
const paintLocks = () => syncModeButtons(state.aeLock === "both" ? "manual" : "auto");
// While a slider is held, the lock poll must not undo its local lock before the value is sent
[rangeExposure, rangeGain, rangeTarget].forEach((el) => {
    el.addEventListener("pointerdown", () => (state.dragging = true));
    ["change", "pointerup", "pointercancel"].forEach((ev) => el.addEventListener(ev, () => (state.dragging = false)));
});
rangeExposure.addEventListener("input", () => {
    if (!isLocked("exposure")) { state.aeLock = lockName(true, isLocked("gain")); paintLocks(); }
});
rangeGain.addEventListener("input", () => {
    if (!isLocked("gain")) { state.aeLock = lockName(isLocked("exposure"), true); paintLocks(); }
});
rangeExposure.addEventListener("change", () => setAeLock(lockName(true, isLocked("gain"))));
rangeGain.addEventListener("change", () => setAeLock(lockName(isLocked("exposure"), true)));

// Target brightness: unlocked it follows the measured brightness; locked, Auto aims for it
const paintTarget = () => {
    valTarget.textContent = rangeTarget.value;
    btnLockTarget.classList.toggle("locked", !!state.targetLocked);
    btnLockTarget.textContent = state.targetLocked ? "Locked" : "Auto";
    btnLockTarget.setAttribute("aria-pressed", state.targetLocked ? "true" : "false");
};
async function setTargetBrightness(value) {
    const before = state.targetLocked;
    const seq = (state.lockSeq = (state.lockSeq || 0) + 1);
    state.lockPending = true;
    state.targetLocked = value != null;
    paintTarget();
    lockBusy(true);
    try {
        const res = await fetch("/api/camera/target-brightness", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        if (seq !== state.lockSeq) return;
        state.lockPending = false;
        syncAeLock(data.ae_lock);
    } catch (err) {
        if (seq !== state.lockSeq) return;
        state.lockPending = false;
        state.targetLocked = before;
        paintTarget();
        showAlert(controlAlert, `Could not set the target brightness: ${err.message}`, "error");
    } finally {
        if (seq === state.lockSeq) lockBusy(false);
    }
}
rangeTarget.addEventListener("input", () => {
    state.targetLocked = true;
    paintTarget();
});
rangeTarget.addEventListener("change", () => setTargetBrightness(parseFloat(rangeTarget.value)));
btnLockTarget.addEventListener("click", () =>
    setTargetBrightness(state.targetLocked ? null : parseFloat(rangeTarget.value))
);

function buildAsc1CommandPacket(cmdId, modeVal, exposureS, gainDb) {
    const buf = new ArrayBuffer(16);
    const dv = new DataView(buf);
    // Magic "ASC1"
    dv.setUint8(0, 0x41);
    dv.setUint8(1, 0x53);
    dv.setUint8(2, 0x43);
    dv.setUint8(3, 0x31);
    dv.setUint8(4, cmdId & 0xff);
    dv.setUint8(5, modeVal & 0xff);
    dv.setUint16(6, 0, true);
    dv.setFloat32(8, Number(exposureS || 0), true);
    dv.setFloat32(12, Number(gainDb || 0), true);
    return buf;
}

function showAlert(el, msg, type, persist = false) {
    if (!el) return;
    el.textContent = msg;
    el.className = `alert-box alert-${type} mt-3`;
    el.classList.remove("hidden");
    if (el._hideTimer) {
        clearTimeout(el._hideTimer);
        el._hideTimer = null;
    }
    if (!persist) {
        el._hideTimer = setTimeout(() => {
            el.classList.add("hidden");
        }, 4000);
    }
}

// ------------------------------------------------------------------
// Calibration Master Dark & Live Progress
// ------------------------------------------------------------------
function updateCalibrationUI(cal) {
    if (!cal) return;
    const st = (cal.state || "idle").toLowerCase();
    const hasProgress = typeof cal.progress_pct === "number";
    // Ignore partial ASB1 binary WebSocket packets during active calibration so progress bar never jumps to 0%
    if (st === "running" && !hasProgress) {
        return;
    }
    const pct = hasProgress ? cal.progress_pct : (st === "completed" ? 100 : 0);
    const readyCnt = cal.ready_profiles_count !== undefined ? cal.ready_profiles_count : 0;
    const totalCnt = cal.total_profiles_count !== undefined ? cal.total_profiles_count : 6;

    if (darkStatusBadge) {
        if (st === "running") {
            darkStatusBadge.textContent = `AUTO-CAPTURING ${cal.current_frame || 0}/${cal.total_frames || 0}`;
            darkStatusBadge.className = "badge badge-warning";
        } else if (readyCnt >= totalCnt && totalCnt > 0) {
            darkStatusBadge.textContent = `ALL ${readyCnt}/${totalCnt} READY`;
            darkStatusBadge.className = "badge badge-success";
        } else if (st === "completed") {
            darkStatusBadge.textContent = `${readyCnt}/${totalCnt} READY`;
            darkStatusBadge.className = "badge badge-success";
        } else if (st === "error") {
            darkStatusBadge.textContent = "ERROR";
            darkStatusBadge.className = "badge badge-danger";
        } else {
            darkStatusBadge.textContent = `${readyCnt}/${totalCnt} READY`;
            darkStatusBadge.className = "badge badge-accent";
        }
    }

    if (darkStatusText && cal.message) {
        darkStatusText.textContent = cal.message;
    }
    if (darkProgressPct) {
        darkProgressPct.textContent = `${Math.round(pct)}%`;
    }
    if (darkProgressBar) {
        darkProgressBar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    }

    const isRunning = st === "running";
    if (btnTriggerDark) {
        btnTriggerDark.disabled = isRunning;
        if (isRunning) {
            btnTriggerDark.textContent = `Capturing Profile ${cal.current_profile || 1}/${cal.total_profiles || 7} — Frame ${cal.current_frame || 0}/${cal.total_frames || "?"} (${Math.round(pct)}%)...`;
        } else {
            const totalProfs = Array.isArray(cal.profiles) ? cal.profiles.length : 7;
            const readyProfs = Array.isArray(cal.profiles) ? cal.profiles.filter((p) => p.ready).length : 0;
            if (readyProfs > 0 && readyProfs < totalProfs) {
                btnTriggerDark.textContent = `Capture Missing Master Dark Profiles (${totalProfs - readyProfs}/${totalProfs} Remaining)`;
            } else {
                btnTriggerDark.textContent = `Capture & Build All Master Dark Profiles (${totalProfs} Profiles: 0.5s – 15.0s)`;
            }
        }
    }

    if (cal.profiles && darkProfilesMatrixBody) {
        darkProfilesMatrixBody.innerHTML = cal.profiles
            .map((p) => {
                const isCurrent =
                    isRunning &&
                    Math.abs((cal.exposure_s || 0) - p.exposure_s) < 0.05 &&
                    Math.abs((cal.gain_db || 0) - p.gain_db) < 0.5;
                let badgeHtml = `<span class="badge">NOT CAPTURED</span>`;
                if (isCurrent) {
                    badgeHtml = `<span class="badge badge-warning">⏳ CAPTURING (${cal.current_frame || 0}/${cal.total_frames || p.num_frames})</span>`;
                } else if (p.ready) {
                    badgeHtml = `<span class="badge badge-success">✅ READY (${p.size_kb} KB)</span>`;
                }
                const updatedStr = p.modified_at
                    ? `${p.raw_filename || p.filename} • ${fmtLocalIso(p.modified_at)}`
                    : `${p.raw_filename || p.filename}`;
                return `
                    <tr>
                        <td><strong>${p.label}</strong></td>
                        <td><span class="text-accent">${p.exposure_s.toFixed(1)}s</span> @ <strong>${p.gain_db.toFixed(0)} dB</strong></td>
                        <td>${p.num_frames} frames</td>
                        <td>${badgeHtml}</td>
                        <td class="text-muted">${updatedStr}</td>
                    </tr>`;
            })
            .join("");
    }

    if (cal.library && darkLibraryBody) {
        if (cal.library.length === 0) {
            darkLibraryBody.innerHTML = `<tr><td colspan="4" class="text-muted" style="text-align:center;">No Master Dark frames generated yet.</td></tr>`;
        } else {
            darkLibraryBody.innerHTML = cal.library
                .map(
                    (f) => `
                    <tr>
                        <td><strong class="text-accent">${f.filename}</strong></td>
                        <td>${f.size_kb} KB</td>
                        <td>${f.modified_at ? fmtLocalIso(f.modified_at) : "--"}</td>
                        <td><button class="btn btn-sm" onclick="window.showDarkPreview('${f.filename}')">View</button></td>
                    </tr>`
                )
                .join("");
        }
    }
}

window.showDarkPreview = async function (fn) {
    let box = document.getElementById("dark-preview-box");
    if (!box) {
        box = document.createElement("div");
        box.id = "dark-preview-box";
        box.style.cssText = "margin-top:12px;padding:12px;border:1px solid #444;border-radius:8px;";
        document.getElementById("dark-library-body").closest(".events-table-wrapper").after(box);
    }
    box.textContent = "Loading...";
    try {
        const q = encodeURIComponent(fn);
        const st = await (await fetch(`/api/calibration/dark-preview?file=${q}&stats=1`)).json();
        const warn = (st.warnings || []).map((w) => `<div style="color:#ff6b6b">⚠ ${w}</div>`).join("") || `<div style="color:#51cf66">OK</div>`;
        const info = (st.info || []).map((w) => `<div style="color:#74c0fc">ℹ ${w}</div>`).join("");
        box.innerHTML = `<strong>${st.filename}</strong> ${st.shape[1]}x${st.shape[0]} ${st.dtype}<br>` +
            `min ${st.min} · max ${st.max} · mean ${st.mean} · median ${st.median} · std ${st.std} · zero ${st.zero_pct}% · sat ${st.saturated_pct}% · stretch ${st.stretch[0]}–${st.stretch[1]} DN${warn}${info}` +
            `<img src="/api/calibration/dark-preview?file=${q}" style="max-width:100%;margin-top:8px;display:block">`;
    } catch (e) {
        box.textContent = "Preview failed: " + e;
    }
};

async function pollCalibrationStatus() {
    try {
        const res = await fetch("/api/calibration/status");
        if (res.ok) {
            const cal = await res.json();
            updateCalibrationUI(cal);
            return cal;
        }
    } catch (e) {}
    return null;
}

async function runAutoDarkLibrary(onlyMissing = false) {
    if (btnTriggerDark) {
        btnTriggerDark.disabled = true;
        btnTriggerDark.textContent = "Capturing Master Dark Profile (0%)...";
    }
    showAlert(
        darkAlert,
        "⏳ Capturing and median-stacking sensor-native Master Dark Profiles (0.5s–15.0s, within camera gain limits)...",
        "info",
        true
    );

    let calPollBusy = false; // don't stack requests when the server answers slower than 500 ms
    const calPollTimer = setInterval(async () => {
        if (calPollBusy) return;
        calPollBusy = true;
        try {
            await pollCalibrationStatus();
        } finally {
            calPollBusy = false;
        }
    }, 500);
    try {
        const res = await fetch(`/api/calibration/auto-library?only_missing=${onlyMissing ? "true" : "false"}`, {
            method: "POST",
        });
        const rawText = await res.text();
        let data = {};
        try {
            data = JSON.parse(rawText);
        } catch (_) {
            data = { detail: rawText };
        }
        const cal = await pollCalibrationStatus();
        if (res.ok && data.success) {
            showAlert(darkAlert, `✅ ${data.message}`, "success", true);
        } else if (cal && cal.state === "running") {
            // If reverse proxy timed out while backend is still running, keep polling until completion
            while (true) {
                await new Promise((r) => setTimeout(r, 1000));
                const st = await pollCalibrationStatus();
                if (!st || st.state !== "running") {
                    if (!st) {
                        showAlert(darkAlert, "⚠️ Lost contact with the station while waiting for calibration. It may still be running; reload to check.", "error", true);
                    } else if (st.state === "completed") {
                        showAlert(darkAlert, `✅ ${st.message}`, "success", true);
                    } else if (st.state === "error") {
                        showAlert(darkAlert, `❌ ${st.message}`, "error", true);
                    }
                    break;
                }
            }
        } else {
            const errMsg = (cal && cal.state === "error" && cal.message) || data.detail || "Error";
            showAlert(darkAlert, `❌ Master Dark calibration failed: ${errMsg}`, "error", true);
        }
    } catch (err) {
        showAlert(darkAlert, `❌ Network error: ${err.message}`, "error", true);
    } finally {
        clearInterval(calPollTimer);
        if (btnTriggerDark) {
            btnTriggerDark.disabled = false;
            btnTriggerDark.textContent = "Capture & Build All Master Dark Profiles (7 Profiles)";
        }
    }
}

if (btnTriggerDark) {
    btnTriggerDark.addEventListener("click", () => runAutoDarkLibrary(false));
}

// ------------------------------------------------------------------
