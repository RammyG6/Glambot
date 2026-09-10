/* Advanced editing controls (shared - templates/_advanced_editor.html):
 *  - live value labels + CSS-approx preview for exposure / contrast / saturation / WB
 *  - a draggable speed-ramp curve editor that serialises into
 *    input[name=speed_ramp_json]
 *  - drives an optional "Render preview" button
 *
 * initAdvancedEditor(root, opts) runs one instance scoped to `root`
 * (a [data-adv-root] element), so any number can coexist on a page
 * (project settings form + every review card). Plain, no build step.
 *
 *   opts.preview (optional): { btn, video, status, form, url }
 *     - form: the <form> whose FormData is POSTed for the exact render
 *     - url:  string, or a function returning the string, to POST to
 */
(function () {
  "use strict";

  function initAdvancedEditor(root, opts) {
    if (!root || root._advInit) return;
    root._advInit = true;
    opts = opts || {};
    var preview = opts.preview || null;

    var q = function (sel) { return root.querySelector(sel); };

    // ---- Speed-ramp curve editor ----
    var enabled = q(".adv-ramp-enable");
    var wrap = q(".adv-ramp-editor");
    var canvas = q(".adv-ramp-canvas");
    var hidden = q(".adv-ramp-json");
    var interpSel = q("select[name=speed_ramp_interpolation]");
    var smoothCb = q("input[name=speed_ramp_smooth_frames]");
    var durationEl = q(".adv-ramp-duration");
    var maxSel = q("select[name=speed_ramp_max_speed]");
    var maxCustom = q("input[name=speed_ramp_max_speed_custom]");
    var maxCustomWrap = q(".adv-ramp-maxspeed-custom");

    var DEFAULT_MAX_SPEED = 40;
    var MAX_PRESETS = ["2", "4", "8", "40"];
    var customMode = false;
    var DEFAULT_CURVE = [{ t: 0, speed: 2 }, { t: 0.35, speed: 0.4 },
                         { t: 0.65, speed: 0.4 }, { t: 1, speed: 2 }];
    function cloneCurve(src) {
      return src.map(function (p) {
        var o = { t: +p.t, speed: +p.speed };
        if (p.hl) o.hl = [p.hl[0], p.hl[1]];
        if (p.hr) o.hr = [p.hr[0], p.hr[1]];
        return o;
      });
    }

    function formatTimecode(sec) {
      if (sec < 60) return sec.toFixed(sec < 10 ? 2 : 1) + "s";
      var m = Math.floor(sec / 60), s = Math.round(sec % 60);
      return m + ":" + (s < 10 ? "0" : "") + s;
    }

    var points = cloneCurve(DEFAULT_CURVE);
    var interpolation = "smooth";
    var smoothFrames = false;
    var maxSpeed = DEFAULT_MAX_SPEED;

    if (hidden && hidden.value) {
      try {
        var saved = JSON.parse(hidden.value);
        if (saved && Array.isArray(saved.points) && saved.points.length >= 2) {
          points = saved.points.map(function (p) {
            var o = { t: +p.t, speed: +p.speed };
            if (Array.isArray(p.hl) && p.hl.length === 2) o.hl = [+p.hl[0], +p.hl[1]];
            if (Array.isArray(p.hr) && p.hr.length === 2) o.hr = [+p.hr[0], +p.hr[1]];
            return o;
          }).sort(function (a, b) { return a.t - b.t; });
          points[0].t = 0; points[points.length - 1].t = 1;
        }
        if (saved && saved.interpolation) interpolation = saved.interpolation;
        smoothFrames = !!(saved && saved.smooth_frames);
        if (saved && +saved.max_speed >= 2) maxSpeed = +saved.max_speed;
        customMode = MAX_PRESETS.indexOf(String(maxSpeed)) === -1;
      } catch (e) { /* keep defaults */ }
    }
    if (interpSel) interpSel.value = interpolation;
    if (smoothCb) smoothCb.checked = smoothFrames;

    var SPEED_MIN = 0.1, VIEW_LO = 0.25;
    var VIEW_HI = maxSpeed;
    var VIEW_OFFSET = Math.log(VIEW_LO) / Math.LN2;                 // log2(0.25)
    var VIEW_SPAN = Math.log(VIEW_HI) / Math.LN2 - VIEW_OFFSET;
    var PAD = 26;

    // Horizontal gridline speeds: the "nice" values that fit under the ceiling,
    // always including the ceiling itself.
    function gridSpeeds() {
      var out = [0.25, 0.5, 1, 2, 5, 10, 20, 40, 100, 200, 500].filter(function (s) {
        return s <= VIEW_HI + 1e-6;
      });
      if (out[out.length - 1] < VIEW_HI - 1e-6) out.push(VIEW_HI);
      return out;
    }

    function syncMaxSpeedControls() {
      if (!maxSel) return;
      maxSel.value = customMode ? "custom" : String(maxSpeed);
      if (maxCustomWrap) maxCustomWrap.classList.toggle("hidden", !customMode);
    }
    if (customMode && maxCustom) maxCustom.value = maxSpeed;
    syncMaxSpeedControls();

    function setMaxSpeed(v) {
      v = Math.max(2, Math.min(1000, +v || DEFAULT_MAX_SPEED));
      maxSpeed = v;
      VIEW_HI = v;
      VIEW_SPAN = Math.log(VIEW_HI) / Math.LN2 - VIEW_OFFSET;
      points.forEach(function (p) { if (p.speed > VIEW_HI) p.speed = VIEW_HI; });
      syncMaxSpeedControls();
      draw(); serialize();
    }
    function applyMaxSpeed(v) {
      customMode = MAX_PRESETS.indexOf(String(+v)) === -1;
      setMaxSpeed(v);
    }

    function speedToV(s) {
      s = Math.max(VIEW_LO, Math.min(VIEW_HI, s));
      return (Math.log(s) / Math.LN2 - VIEW_OFFSET) / VIEW_SPAN;
    }
    function vToSpeed(v) {
      v = Math.max(0, Math.min(1, v));
      return Math.max(SPEED_MIN, Math.min(VIEW_HI, Math.pow(2, v * VIEW_SPAN + VIEW_OFFSET)));
    }
    function bez1(p0, p1, p2, p3, u) {
      var m = 1 - u;
      return m*m*m*p0 + 3*m*m*u*p1 + 3*m*u*u*p2 + u*u*u*p3;
    }
    function autoHandle(pt, other, sign) { return [sign * Math.abs(other.t - pt.t) / 3, 0]; }
    function handleOf(pt, key, other, sign) {
      var h = pt[key];
      if (Array.isArray(h) && h.length === 2) return h;
      return autoHandle(pt, other, sign);
    }

    function sampleSpeed(u) {
      if (u <= points[0].t) return points[0].speed;
      var last = points[points.length - 1];
      if (u >= last.t) return last.speed;
      for (var i = 1; i < points.length; i++) {
        var a = points[i - 1], b = points[i];
        if (u > b.t) continue;
        var va = speedToV(a.speed), vb = speedToV(b.speed);
        if (b.t - a.t <= 0) return b.speed;
        if (interpolation === "linear") {
          return vToSpeed(va + (vb - va) * ((u - a.t) / (b.t - a.t)));
        }
        var hr = handleOf(a, "hr", b, 1), hl = handleOf(b, "hl", a, -1);
        var c1t = Math.min(b.t, Math.max(a.t, a.t + hr[0]));
        var c2t = Math.min(b.t, Math.max(a.t, b.t + hl[0]));
        if (c2t < c1t) { c1t = c2t = (c1t + c2t) / 2; }
        var c1v = va + hr[1], c2v = vb + hl[1];
        var lo = 0, hi = 1;
        for (var k = 0; k < 28; k++) {
          var mid = (lo + hi) / 2;
          if (bez1(a.t, c1t, c2t, b.t, mid) < u) lo = mid; else hi = mid;
        }
        var p = (lo + hi) / 2;
        return vToSpeed(bez1(va, c1v, c2v, vb, p));
      }
      return last.speed;
    }

    function px(t) { return PAD + t * (canvas.width - 2 * PAD); }
    function pyV(v) { return PAD + (1 - v) * (canvas.height - 2 * PAD); }
    function py(s) { return pyV(speedToV(s)); }
    function invT(x) { return Math.max(0, Math.min(1, (x - PAD) / (canvas.width - 2 * PAD))); }
    function invV(y) { return Math.max(0, Math.min(1, 1 - (y - PAD) / (canvas.height - 2 * PAD))); }
    function invS(y) { return vToSpeed(invV(y)); }

    function draw() {
      if (!canvas) return;
      var ctx = canvas.getContext("2d");
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = "rgba(255,255,255,0.12)";
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.font = "10px sans-serif";
      gridSpeeds().forEach(function (s) {
        var y = py(s);
        ctx.beginPath(); ctx.moveTo(PAD, y); ctx.lineTo(canvas.width - PAD, y); ctx.stroke();
        ctx.fillText((s < 1 ? s : Math.round(s)) + "x", 2, y + 3);
      });

      // X-axis: vertical gridlines labelled as % of the (trimmed) clip, with a
      // seconds readout in parens only once a real clip length is known.
      var durRaw = durationEl ? parseFloat(durationEl.value) : NaN;
      var dur = durRaw > 0 ? durRaw : null;
      var ticks = 8;
      ctx.strokeStyle = "rgba(255,255,255,0.08)";
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.textAlign = "center";
      for (var ti = 0; ti <= ticks; ti++) {
        var tu = ti / ticks, x = px(tu);
        ctx.beginPath(); ctx.moveTo(x, PAD); ctx.lineTo(x, canvas.height - PAD); ctx.stroke();
        var lbl = Math.round(tu * 100) + "%";
        if (dur) lbl += " (" + formatTimecode(tu * dur) + ")";
        ctx.fillText(lbl, x, canvas.height - PAD + 12);
      }
      ctx.textAlign = "left";

      ctx.strokeStyle = "#6ea8fe";
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (var i = 0; i <= 120; i++) {
        var u = i / 120;
        var xx = px(u), yy = py(sampleSpeed(u));
        if (i === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
      }
      ctx.stroke();

      if (interpolation !== "linear") {
        points.forEach(function (p, idx) {
          var vp = speedToV(p.speed);
          [["hl", idx > 0 ? points[idx - 1] : null, -1],
           ["hr", idx < points.length - 1 ? points[idx + 1] : null, 1]].forEach(function (spec) {
            if (!spec[1]) return;
            var h = handleOf(p, spec[0], spec[1], spec[2]);
            var hx = px(p.t + h[0]), hy = pyV(vp + h[1]);
            ctx.strokeStyle = "rgba(110,168,254,0.55)"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(px(p.t), pyV(vp)); ctx.lineTo(hx, hy); ctx.stroke();
            ctx.beginPath(); ctx.arc(hx, hy, 4, 0, Math.PI * 2);
            ctx.strokeStyle = "#6ea8fe"; ctx.lineWidth = 1.5; ctx.stroke();
          });
        });
      }
      points.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(px(p.t), py(p.speed), 6, 0, Math.PI * 2);
        ctx.fillStyle = "#6ea8fe";
        ctx.fill();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke();
      });
    }

    function serialize() {
      if (!hidden) return;
      hidden.value = JSON.stringify({
        points: points.map(function (p) {
          var o = { t: +p.t.toFixed(4), speed: +p.speed.toFixed(3) };
          if (p.hl) o.hl = [+p.hl[0].toFixed(4), +p.hl[1].toFixed(4)];
          if (p.hr) o.hr = [+p.hr[0].toFixed(4), +p.hr[1].toFixed(4)];
          return o;
        }),
        interpolation: interpolation,
        smooth_frames: smoothFrames,
        max_speed: maxSpeed,
      });
    }

    var drag = null;
    function localXY(ev) {
      var r = canvas.getBoundingClientRect();
      var sx = canvas.width / r.width, sy = canvas.height / r.height;
      return { x: (ev.clientX - r.left) * sx, y: (ev.clientY - r.top) * sy };
    }
    function hitPoint(pt) {
      for (var i = 0; i < points.length; i++) {
        if (Math.hypot(px(points[i].t) - pt.x, py(points[i].speed) - pt.y) < 12) return i;
      }
      return -1;
    }
    function hitHandle(pt) {
      if (interpolation === "linear") return null;
      for (var i = 0; i < points.length; i++) {
        var p = points[i], vp = speedToV(p.speed);
        var sides = [];
        if (i > 0) sides.push(["hl", points[i - 1], -1]);
        if (i < points.length - 1) sides.push(["hr", points[i + 1], 1]);
        for (var s = 0; s < sides.length; s++) {
          var h = handleOf(p, sides[s][0], sides[s][1], sides[s][2]);
          if (Math.hypot(px(p.t + h[0]) - pt.x, pyV(vp + h[1]) - pt.y) < 10) {
            return { idx: i, side: sides[s][0], nb: sides[s][1], sign: sides[s][2] };
          }
        }
      }
      return null;
    }

    if (canvas) {
      canvas.addEventListener("pointerdown", function (ev) {
        var pt = localXY(ev);
        var h = hitHandle(pt);
        if (h) {
          drag = h;
          var p = points[h.idx];
          if (!p[h.side]) p[h.side] = handleOf(p, h.side, h.nb, h.sign).slice();
        } else {
          var idx = hitPoint(pt);
          drag = idx === -1 ? null : { idx: idx };
        }
        if (drag) canvas.setPointerCapture(ev.pointerId);
      });
      canvas.addEventListener("pointermove", function (ev) {
        if (!drag) return;
        var pt = localXY(ev);
        var p = points[drag.idx];
        if (drag.side) {
          var vp = speedToV(p.speed);
          var dt = invT(pt.x) - p.t;
          var dv = invV(pt.y) - vp;
          if (drag.side === "hl") dt = Math.min(0, Math.max(-1, dt));
          else dt = Math.max(0, Math.min(1, dt));
          p[drag.side] = [dt, Math.max(-1, Math.min(1, dv))];
        } else {
          p.speed = invS(pt.y);
          if (drag.idx !== 0 && drag.idx !== points.length - 1) {
            var lo = points[drag.idx - 1].t + 0.01;
            var hi = points[drag.idx + 1].t - 0.01;
            p.t = Math.max(lo, Math.min(hi, invT(pt.x)));
          }
        }
        draw(); serialize();
      });
      canvas.addEventListener("pointerup", function () { drag = null; });
      canvas.addEventListener("dblclick", function (ev) {
        var pt = localXY(ev);
        var idx = hitPoint(pt);
        if (idx > 0 && idx < points.length - 1) {
          points.splice(idx, 1);
        } else if (idx === -1) {
          var t = invT(pt.x);
          if (t > 0.02 && t < 0.98) {
            points.push({ t: t, speed: invS(pt.y) });
            points.sort(function (a, b) { return a.t - b.t; });
          }
        }
        draw(); serialize();
      });
    }

    if (interpSel) interpSel.addEventListener("change", function () {
      interpolation = interpSel.value; draw(); serialize();
    });
    if (smoothCb) smoothCb.addEventListener("change", function () {
      smoothFrames = smoothCb.checked; serialize();
    });
    if (durationEl) durationEl.addEventListener("input", draw);
    if (maxSel) maxSel.addEventListener("change", function () {
      if (maxSel.value === "custom") {
        customMode = true;
        if (maxCustomWrap) maxCustomWrap.classList.remove("hidden");
        if (maxCustom) {
          if (!maxCustom.value) maxCustom.value = maxSpeed;  // seed; keeps the scale valid
          maxCustom.focus();
          var seed = parseFloat(maxCustom.value);
          if (seed >= 2) setMaxSpeed(seed);
        }
      } else {
        customMode = false;
        setMaxSpeed(maxSel.value);
      }
    });
    if (maxCustom) maxCustom.addEventListener("input", function () {
      if (!customMode) return;
      var v = parseFloat(maxCustom.value);
      if (v >= 2) setMaxSpeed(v);  // below 2 or blank: keep the last good scale
    });

    function syncEnabled() {
      if (!wrap || !enabled) return;
      wrap.classList.toggle("hidden", !enabled.checked);
      if (enabled.checked) { draw(); serialize(); }
      else if (hidden) { hidden.value = ""; }
    }
    if (enabled) enabled.addEventListener("change", syncEnabled);
    syncEnabled();

    // ---- Live CSS colour approximation on the preview video ----
    function makeGradeApprox(videoEl) {
      if (!videoEl) return null;
      var w = document.createElement("div");
      w.style.cssText = "position:relative;display:inline-block;max-width:100%;line-height:0";
      videoEl.parentNode.insertBefore(w, videoEl);
      w.appendChild(videoEl);
      var tint = document.createElement("div");
      tint.style.cssText = "position:absolute;inset:0;pointer-events:none;mix-blend-mode:overlay;opacity:0;border-radius:inherit";
      w.appendChild(tint);
      var baked = false;
      return {
        apply: function (expo, contrast, sat, wb) {
          if (baked) return;
          videoEl.style.filter = "brightness(" + Math.pow(2, expo).toFixed(3) +
            ") contrast(" + (+contrast).toFixed(3) +
            ") saturate(" + (+sat).toFixed(3) + ")";
          tint.style.background = wb < 0 ? "rgb(255,170,80)" : "rgb(120,170,255)";
          tint.style.opacity = wb === 0 ? 0 : Math.min(0.4, Math.abs(wb) / 250);
        },
        setBaked: function (b) { baked = b; if (b) { videoEl.style.filter = ""; tint.style.opacity = 0; } },
      };
    }

    // ---- Exposure / contrast / saturation / white balance sliders ----
    var sliders = Array.prototype.slice.call(root.querySelectorAll(".adv-grade input[type=range]"));
    var gradeApprox = preview ? makeGradeApprox(preview.video) : null;

    function currentGrade() {
      function val(name, dflt) {
        var el = root.querySelector("input[name=" + name + "]");
        return el ? (parseFloat(el.value) || dflt) : dflt;
      }
      return [val("exposure", 0), val("contrast", 1), val("saturation", 1),
              val("white_balance", 0)];
    }
    function refreshApprox(fromSlider) {
      if (!gradeApprox) return;
      gradeApprox.setBaked(false);
      var g = currentGrade();
      gradeApprox.apply(g[0], g[1], g[2], g[3]);
      if (fromSlider && preview && preview.status && preview.video &&
          !preview.video.classList.contains("hidden")) {
        preview.status.textContent = "Approximate colour — Render preview to confirm.";
      }
    }
    sliders.forEach(function (r) {
      var out = r.parentElement.querySelector(".adv-val");
      if (out) out.textContent = r.value;
      r.addEventListener("input", function () {
        if (out) out.textContent = r.value;
        refreshApprox(true);
      });
    });

    // ---- Reset controls ----
    function setControlValue(name, value) {
      var el = root.querySelector("[name=" + name + "]");
      if (!el) return;
      if (el.type === "checkbox") el.checked = !!value;
      else el.value = value;
      var out = el.parentElement && el.parentElement.querySelector(".adv-val");
      if (out) out.textContent = el.value;
    }
    function resetField(name) {
      if (name === "curve") {
        points = cloneCurve(DEFAULT_CURVE);
        interpolation = "smooth"; smoothFrames = false;
        if (interpSel) interpSel.value = "smooth";
        if (smoothCb) smoothCb.checked = false;
        draw(); serialize();
        return;
      }
      if (name === "speed_ramp_max_speed") { applyMaxSpeed(DEFAULT_MAX_SPEED); return; }
      var el = root.querySelector("[name=" + name + "]");
      var dflt = el ? el.getAttribute("data-default") : null;
      setControlValue(name, el && el.type === "checkbox" ? false : (dflt || 0));
      if (name === "speed_ramp_interpolation") { interpolation = "smooth"; draw(); serialize(); }
      else refreshApprox(true);
    }
    function applyState(state) {
      setControlValue("exposure", state && state.exposure != null ? state.exposure : 0);
      setControlValue("contrast", state && state.contrast != null ? state.contrast : 1);
      setControlValue("saturation", state && state.saturation != null ? state.saturation : 1);
      setControlValue("white_balance", state && state.white_balance != null ? state.white_balance : 0);
      refreshApprox(true);
      var ramp = state && state.speed_ramp;
      if (enabled) { enabled.checked = !!ramp; }
      if (ramp && Array.isArray(ramp.points) && ramp.points.length >= 2) {
        points = ramp.points.map(function (p) {
          var o = { t: +p.t, speed: +p.speed };
          if (Array.isArray(p.hl) && p.hl.length === 2) o.hl = [+p.hl[0], +p.hl[1]];
          if (Array.isArray(p.hr) && p.hr.length === 2) o.hr = [+p.hr[0], +p.hr[1]];
          return o;
        }).sort(function (a, b) { return a.t - b.t; });
        points[0].t = 0; points[points.length - 1].t = 1;
        interpolation = ramp.interpolation === "linear" ? "linear" : "smooth";
        smoothFrames = !!ramp.smooth_frames;
        if (interpSel) interpSel.value = interpolation;
        if (smoothCb) smoothCb.checked = smoothFrames;
        applyMaxSpeed(+ramp.max_speed >= 2 ? +ramp.max_speed : DEFAULT_MAX_SPEED);
        if (customMode && maxCustom) maxCustom.value = maxSpeed;
      } else {
        points = cloneCurve(DEFAULT_CURVE);
        interpolation = "smooth"; smoothFrames = false;
        if (interpSel) interpSel.value = "smooth";
        if (smoothCb) smoothCb.checked = false;
        applyMaxSpeed(DEFAULT_MAX_SPEED);
      }
      syncEnabled();
      draw(); serialize();
    }
    function resetAll(kind) {
      if (kind === "project") {
        var proj = {};
        try { proj = JSON.parse(root.dataset.advProject || "{}") || {}; } catch (e) { proj = {}; }
        applyState(proj);
      } else {
        applyState(null);
      }
    }
    Array.prototype.forEach.call(root.querySelectorAll(".adv-reset"), function (btn) {
      btn.addEventListener("click", function () { resetField(btn.getAttribute("data-reset")); });
    });
    Array.prototype.forEach.call(root.querySelectorAll("[data-reset-all]"), function (btn) {
      btn.addEventListener("click", function () { resetAll(btn.getAttribute("data-reset-all")); });
    });

    // ---- "Render preview" ----
    if (preview && preview.btn && preview.form && preview.url) {
      preview.btn.addEventListener("click", function () {
        var url = typeof preview.url === "function" ? preview.url() : preview.url;
        var fd = new FormData(preview.form);
        preview.btn.disabled = true;
        if (preview.status) preview.status.textContent = "Rendering preview…";
        fetch(url, { method: "POST", body: fd })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            preview.btn.disabled = false;
            if (data.ok) {
              if (gradeApprox) gradeApprox.setBaked(true);
              if (preview.status) preview.status.textContent = "Exact render.";
              if (preview.video) {
                preview.video.src = data.url;
                preview.video.classList.remove("hidden");
                preview.video.play().catch(function () {});
              }
              if (durationEl && data.duration) { durationEl.value = data.duration.toFixed(1); draw(); }
            } else if (preview.status) {
              preview.status.textContent = data.error || "Preview failed.";
            }
          })
          .catch(function () {
            preview.btn.disabled = false;
            if (preview.status) preview.status.textContent = "Preview failed.";
          });
      });
    }
  }

  window.GlambotAdvancedEditor = initAdvancedEditor;

  // ---- Auto-bootstrap: the project settings form ----
  function bootProjectForm() {
    var form = document.getElementById("projectForm");
    if (!form) return;
    var root = form.querySelector("[data-adv-root]");
    if (!root) return;
    var btn = document.getElementById("renderPreviewBtn");
    var nameEl = document.getElementById("project_name");
    initAdvancedEditor(root, {
      preview: (btn && nameEl) ? {
        btn: btn,
        video: document.getElementById("previewVideo"),
        status: document.getElementById("previewStatus"),
        form: form,
        url: function () { return "/projects/" + encodeURIComponent(nameEl.value) + "/preview"; },
      } : null,
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootProjectForm);
  } else {
    bootProjectForm();
  }
})();
