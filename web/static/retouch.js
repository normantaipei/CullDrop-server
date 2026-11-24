// Culldrop 返圖頁的修容（捏臉）：給 cos 團照接力用。每個人挑自己的臉微調，存一次是一筆紀錄，下一個人接著改。
// 非破壞式：這裡只算「每張臉的變形元件」送給伺服器，原圖不動；伺服器用同一套公式輸出原尺寸。
// 找臉與特徵點用 MediaPipe（在瀏覽器裡跑，照片不會送到別的地方），模型由自己的伺服器提供。
// 座標：照片寬 = 1 的等比座標（x ∈ [0, 1]、y ∈ [0, 高 / 寬]）。元件格式與公式見 server/app/retouch.py，兩邊要一起改。
(() => {
  // [滑桿名, 標籤]；正的方向就是標籤說的方向
  const SLIDERS = [
    ["slim", "瘦臉"], ["cheek", "收顴骨"], ["jaw", "收下顎"], ["chin", "下巴長度"],
    ["eye", "眼睛大小"], ["eyeGap", "眼距"], ["nose", "瘦鼻翼"], ["mouth", "嘴巴大小"], ["smile", "嘴角上揚"],
  ];
  const GRID = 40;            // 每張臉的預覽格子數
  const POLL_MS = 8000;       // 多久看一次別人有沒有存新的
  const NAME_KEY = "culldrop.retouch.name";

  // ---------- 向量 ----------
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1];
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const unit = (a) => { const n = Math.hypot(a[0], a[1]) || 1; return [a[0] / n, a[1] / n]; };
  const r6 = (v) => Math.round(v * 1e6) / 1e6;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // ---------- 滑桿 → 變形元件 ----------
  // lm 是 MediaPipe 的 478 個特徵點（等比座標）。左右是照片上的左右。
  function buildPrims(lm, params) {
    const P = (i) => lm[i], out = [];
    const amount = (k) => clamp(params[k] || 0, -100, 100) / 100;
    const eyeA = mid(P(33), P(133)), eyeB = mid(P(362), P(263)), eyeMid = mid(eyeA, eyeB);
    const u = unit(sub(eyeB, eyeA));                       // 沿兩眼連線往照片右邊
    let v = [-u[1], u[0]];                                 // 垂直兩眼連線、往下巴
    if (dot(v, sub(P(152), eyeMid)) < 0) v = [-v[0], -v[1]];
    const s = dist(P(234), P(454));                        // 臉寬
    const inward = (p) => (dot(sub(p, eyeMid), u) > 0 ? [-u[0], -u[1]] : u);
    const outward = (p) => { const d = inward(p); return [-d[0], -d[1]]; };
    const move = (c, r, d, len) => { if (len) out.push(["m", r6(c[0]), r6(c[1]), r6(r), r6(d[0] * len), r6(d[1] * len)]); };
    const scale = (c, rx, ry, k) => { if (k) out.push(["s", r6(c[0]), r6(c[1]), r6(u[0]), r6(u[1]), r6(rx), r6(ry), r6(k)]); };

    // 臉的輪廓：從耳朵高度往下到下巴，左右各一排點往中線推
    for (const i of [93, 132, 58, 172, 323, 361, 288, 397]) move(P(i), 0.30 * s, inward(P(i)), amount("slim") * 0.028 * s);
    for (const i of [234, 93, 454, 323]) move(P(i), 0.24 * s, inward(P(i)), amount("cheek") * 0.030 * s);
    for (const i of [172, 136, 150, 397, 365, 379]) move(P(i), 0.22 * s, inward(P(i)), amount("jaw") * 0.030 * s);
    move(P(152), 0.30 * s, v, amount("chin") * 0.07 * s);
    // 眼睛：[瞳孔, 外眼角, 內眼角]
    for (const [iris, outer, inner] of [[468, 33, 133], [473, 263, 362]]) {
      const c = lm[iris] || mid(P(outer), P(inner)), w = dist(P(outer), P(inner));
      scale(c, 1.0 * w, 0.8 * w, amount("eye") * 0.22);
      move(c, 1.1 * w, outward(c), amount("eyeGap") * 0.10 * w);
    }
    // 鼻翼
    const noseW = dist(P(64), P(294));
    for (const i of [64, 294]) move(P(i), 0.6 * noseW, inward(P(i)), amount("nose") * 0.14 * noseW);
    // 嘴
    const mouthW = dist(P(61), P(291));
    scale(mid(P(61), P(291)), 0.85 * mouthW, 0.6 * mouthW, amount("mouth") * 0.20);
    for (const i of [61, 291]) {
      const o = outward(P(i));
      move(P(i), 0.42 * mouthW, unit([o[0] * 0.35 - v[0] * 0.9, o[1] * 0.35 - v[1] * 0.9]), amount("smile") * 0.10 * mouthW);
    }
    return out;
  }

  // ---------- 變形（和 server/app/retouch.py 的 _one / displace 相同）----------
  const radius = (p) => (p[0] === "m" ? p[3] : Math.max(p[5], p[6]));
  function displace(prims, x, y, out) {
    let dx = 0, dy = 0;
    for (const p of prims) {
      const ex = x - p[1], ey = y - p[2];
      if (p[0] === "m") {
        const t2 = (ex * ex + ey * ey) / (p[3] * p[3]);
        if (t2 < 1) { const w = (1 - t2) * (1 - t2); dx -= p[4] * w; dy -= p[5] * w; }
      } else {
        const a = (ex * p[3] + ey * p[4]) / p[5], b = (ey * p[3] - ex * p[4]) / p[6], t2 = a * a + b * b;
        if (t2 < 1) { const w = p[7] * (1 - t2) * (1 - t2); dx -= w * ex; dy -= w * ey; }
      }
    }
    out[0] = dx; out[1] = dy;
  }
  function extent(prims) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of prims) { const r = radius(p); x0 = Math.min(x0, p[1] - r); y0 = Math.min(y0, p[2] - r); x1 = Math.max(x1, p[1] + r); y1 = Math.max(y1, p[2] + r); }
    return [x0, y0, x1, y1];
  }

  /** 畫面要畫的三角形：[x, y, 取樣 x, 取樣 y]。前 6 個點是整張原圖，後面每張臉一塊格子蓋上去。 */
  function buildMesh(groups, asp) {
    groups = groups.filter((g) => g.length);
    const all = groups.flat(), data = new Float32Array(24 + groups.length * GRID * GRID * 24), d = [0, 0];
    data.set([0, 0, 0, 0, 1, 0, 1, 0, 0, asp, 0, asp, 1, 0, 1, 0, 1, asp, 1, asp, 0, asp, 0, asp]);
    let o = 24;
    const n = GRID + 1, sx = new Float32Array(n * n), sy = new Float32Array(n * n);
    for (const prims of groups) {
      let [x0, y0, x1, y1] = extent(prims);
      x0 = clamp(x0, 0, 1); x1 = clamp(x1, 0, 1); y0 = clamp(y0, 0, asp); y1 = clamp(y1, 0, asp);
      // 別張臉的元件伸進這一塊的也要算：重疊的地方兩塊畫出來才會一樣
      const near = all.filter((p) => { const r = radius(p); return p[1] + r > x0 && p[1] - r < x1 && p[2] + r > y0 && p[2] - r < y1; });
      for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
        const x = x0 + (x1 - x0) * i / GRID, y = y0 + (y1 - y0) * j / GRID;
        displace(near, x, y, d);
        sx[j * n + i] = clamp(x + d[0], 0, 1); sy[j * n + i] = clamp(y + d[1], 0, asp);
      }
      const put = (i, j) => { data[o++] = x0 + (x1 - x0) * i / GRID; data[o++] = y0 + (y1 - y0) * j / GRID; data[o++] = sx[j * n + i]; data[o++] = sy[j * n + i]; };
      for (let j = 0; j < GRID; j++) for (let i = 0; i < GRID; i++) { put(i, j); put(i + 1, j); put(i, j + 1); put(i + 1, j); put(i + 1, j + 1); put(i, j + 1); }
    }
    return data;
  }

  // ---------- WebGL ----------
  function createRenderer(canvas, img, asp) {
    const opts = { alpha: false, antialias: true };
    const gl2 = canvas.getContext("webgl2", opts), gl = gl2 || canvas.getContext("webgl", opts);
    if (!gl) return null;
    const shader = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
    const prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER,
      "attribute vec2 a_pos; attribute vec2 a_src; uniform vec4 u_view; uniform float u_asp; varying vec2 v_uv;" +
      "void main() { v_uv = vec2(a_src.x, a_src.y / u_asp); vec2 p = a_pos * u_view.xy + u_view.zw; gl_Position = vec4(p.x, -p.y, 0.0, 1.0); }"));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER,
      "precision mediump float; uniform sampler2D u_tex; varying vec2 v_uv; void main() { gl_FragColor = texture2D(u_tex, v_uv); }"));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
    gl.useProgram(prog);
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    if (gl2) { gl.generateMipmap(gl.TEXTURE_2D); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR); }
    else gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);     // WebGL1 的非 2 次方貼圖不能用 mipmap
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    const aPos = gl.getAttribLocation(prog, "a_pos"), aSrc = gl.getAttribLocation(prog, "a_src");
    gl.enableVertexAttribArray(aPos); gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 16, 0);
    gl.enableVertexAttribArray(aSrc); gl.vertexAttribPointer(aSrc, 2, gl.FLOAT, false, 16, 8);
    gl.uniform1f(gl.getUniformLocation(prog, "u_asp"), asp);
    const uView = gl.getUniformLocation(prog, "u_view");
    let count = 6;
    return {
      setMesh(data) { gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW); count = data.length / 4; },
      /** view：畫面座標（CSS px）= 等比座標 × s + (x, y) */
      draw(view, cssW, cssH, original) {
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.clearColor(0.047, 0.047, 0.055, 1); gl.clear(gl.COLOR_BUFFER_BIT);
        gl.uniform4f(uView, 2 * view.s / cssW, 2 * view.s / cssH, 2 * view.x / cssW - 1, 2 * view.y / cssH - 1);
        gl.drawArrays(gl.TRIANGLES, 0, original ? 6 : count);
      },
      destroy() { const ext = gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext(); },
    };
  }

  // ---------- 找臉 ----------
  let visionPromise = null;
  function loadVision(vendor) {
    visionPromise = visionPromise || (async () => {
      const mp = await import(`${vendor}/vision_bundle.mjs`);
      const files = { wasmLoaderPath: `${vendor}/vision_wasm_internal.js`, wasmBinaryPath: `${vendor}/vision_wasm_internal.wasm` };
      const detector = await mp.FaceDetector.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${vendor}/blaze_face_short_range.tflite`, delegate: "CPU" }, runningMode: "IMAGE", minDetectionConfidence: 0.5 });
      const landmarker = (confidence) => mp.FaceLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: `${vendor}/face_landmarker.task`, delegate: "CPU" }, runningMode: "IMAGE", numFaces: 3,
        minFaceDetectionConfidence: confidence, minFacePresenceConfidence: confidence });
      // 自動找臉用嚴格的（衣服上的圓形配件、背景圖案常被當成臉）；使用者自己指的位置用寬鬆的（濃妝、假髮、面具）
      return { detector, strict: await landmarker(0.65), loose: await landmarker(0.3) };
    })();
    visionPromise.catch(() => { visionPromise = null; });
    return visionPromise;
  }

  const boxOf = (lm) => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of lm) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); }
    return [r6(x0), r6(y0), r6(x1), r6(y1)];
  };
  function iou(a, b) {
    const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]), h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
    if (w <= 0 || h <= 0) return 0;
    return w * h / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - w * h);
  }
  const inBox = (b, x, y) => x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3];
  const tick = () => new Promise((r) => setTimeout(r, 0));

  /** 把圖的一塊正方形畫到畫布上（超出圖的部分留黑），回傳畫布 */
  const scratch = document.createElement("canvas");
  function cropTo(img, x, y, side, size) {
    scratch.width = scratch.height = size;
    const ctx = scratch.getContext("2d");
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, size, size);
    const sx0 = Math.max(0, x), sy0 = Math.max(0, y), sx1 = Math.min(img.naturalWidth, x + side), sy1 = Math.min(img.naturalHeight, y + side);
    if (sx1 > sx0 && sy1 > sy0) {
      const k = size / side;
      ctx.drawImage(img, sx0, sy0, sx1 - sx0, sy1 - sy0, (sx0 - x) * k, (sy0 - y) * k, (sx1 - sx0) * k, (sy1 - sy0) * k);
    }
    return scratch;
  }

  /** 在 (cx, cy) 附近 side 大小的範圍找特徵點（px）；回傳等比座標的 478 個點，找不到是 null */
  function landmarksAt(img, landmarker, cx, cy, side) {
    const x = cx - side / 2, y = cy - side / 2, size = Math.round(clamp(side, 192, 512));
    const res = landmarker.detect(cropTo(img, x, y, side, size));
    let best = null, bestD = Infinity;
    for (const pts of res.faceLandmarks || []) {
      const lm = pts.map((p) => [(x + p.x * side) / img.naturalWidth, (y + p.y * side) / img.naturalWidth]);
      const b = boxOf(lm), d = Math.hypot((b[0] + b[2]) / 2 * img.naturalWidth - cx, (b[1] + b[3]) / 2 * img.naturalWidth - cy);
      if (d < bestD) { best = lm; bestD = d; }
    }
    return best && bestD < side * 0.35 ? best : null;
  }

  /** 整張圖找臉。偵測模型的輸入只有 128px，團照裡的臉太小會漏，所以切成大中小三種格子各找一次，再合併。 */
  async function detectFaces(img, vision, onProgress, alive) {
    const iw = img.naturalWidth, ih = img.naturalHeight, short = Math.min(iw, ih), tiles = [];
    for (const f of [1, 0.5, 0.25]) {
      const T = Math.round(short * f), step = T / 2;
      const nx = Math.max(1, Math.ceil((iw - T) / step) + 1), ny = Math.max(1, Math.ceil((ih - T) / step) + 1);
      for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) tiles.push([Math.round(Math.min(i * step, iw - T)), Math.round(Math.min(j * step, ih - T)), T]);
    }
    const found = [];
    for (let n = 0; n < tiles.length; n++) {
      const [x, y, T] = tiles[n], size = Math.min(T, 256), k = T / size, edge = T * 0.02;
      for (const det of vision.detector.detect(cropTo(img, x, y, T, size)).detections || []) {
        const b = det.boundingBox;
        if (!b) continue;
        const bx = x + b.originX * k, by = y + b.originY * k, bw = b.width * k, bh = b.height * k;
        // 被格子邊緣切到的臉不準（圖本身的邊緣除外），隔壁格會有完整的
        if ((bx - x < edge && x > 0) || (by - y < edge && y > 0) || (x + T - bx - bw < edge && x + T < iw) || (y + T - by - bh < edge && y + T < ih)) continue;
        found.push({ box: [bx, by, bx + bw, by + bh], score: (det.categories[0] || {}).score || 0 });
      }
      if (n % 6 === 5) { onProgress(n + 1, tiles.length); await tick(); if (!alive()) return []; }
    }
    found.sort((a, b) => b.score - a.score);
    const keep = [];
    for (const c of found) {
      const cx = (c.box[0] + c.box[2]) / 2, cy = (c.box[1] + c.box[3]) / 2;
      if (!keep.some((k) => iou(k.box, c.box) > 0.3 || inBox(k.box, cx, cy))) keep.push(c);
    }
    const faces = [];
    for (const c of keep) {
      const side = Math.max(c.box[2] - c.box[0], c.box[3] - c.box[1]) * 2.4;
      const lm = landmarksAt(img, vision.strict, (c.box[0] + c.box[2]) / 2, (c.box[1] + c.box[3]) / 2, side);
      if (lm && !faces.some((f) => iou(boxOf(f), boxOf(lm)) > 0.4)) faces.push(lm);
      await tick();
      if (!alive()) return [];
    }
    return faces;
  }

  // ---------- 介面 ----------
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(10)), (b) => "0123456789abcdefghijklmnopqrstuvwxyz"[b % 36]).join("");
  const sameParams = (a, b) => SLIDERS.every(([k]) => ((a || {})[k] || 0) === ((b || {})[k] || 0));
  const signed = (v) => (v > 0 ? `+${v}` : `${v}`);
  function timeAgo(at) {
    const sec = Date.now() / 1000 - at;
    if (sec < 60) return t("剛剛");
    if (sec < 3600) return t("{n} 分鐘前", { n: Math.floor(sec / 60) });
    if (sec < 86400) return t("{n} 小時前", { n: Math.floor(sec / 3600) });
    return new Date(at * 1000).toLocaleString(document.documentElement.lang, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  let current = null;     // 一次只開一個

  function open({ key, photo, vendor, onChange }) {
    if (current) return;
    const api = `/api/share/${key}/retouch/${photo.id}`;
    const S = {
      alive: true, img: null, asp: 1, renderer: null, view: { s: 1, x: 0, y: 0 }, fitScale: 1,
      faces: [],            // {id, lm, box, params, prims, saved, by}；saved 是伺服器上的那一份（沒修過是 null）
      sel: null, adding: false, original: false, busy: false, vision: null,
      head: null, parent: null, commits: [], preview: null,
    };
    current = S;

    // DOM
    const root = el("div", "rt");
    const top = el("header", "rt-top");
    const back = el("button", "rt-btn", t("‹ 返回"));
    const title = el("span", "rt-title", photo.name);
    const status = el("span", "rt-status");
    const compare = el("button", "rt-btn", t("按住看原圖"));
    const save = el("button", "rt-btn rt-primary", t("儲存"));
    top.append(back, title, status, compare, save);
    const body = el("div", "rt-body");
    const stage = el("div", "rt-stage");
    const canvas = el("canvas");
    const overlay = el("div", "rt-faces");
    const banner = el("div", "rt-banner"); banner.hidden = true;
    const fitBtn = el("button", "rt-btn rt-fit", t("看全圖"));
    stage.append(canvas, overlay, banner, fitBtn);
    const panel = el("aside", "rt-panel");
    const tabs = el("div", "rt-tabs");
    const tabAdjust = el("button", "on", t("調整")), tabLog = el("button", "", t("紀錄"));
    tabs.append(tabAdjust, tabLog);
    const adjust = el("div", "rt-pane");
    const faceBar = el("div", "rt-facebar");
    const hint = el("p", "rt-hint");
    const sliders = el("div", "rt-sliders");
    const reset = el("button", "rt-btn", t("重設這張臉"));
    const who = el("div", "rt-who");
    const nameIn = el("input"); nameIn.maxLength = 24; nameIn.placeholder = t("你的名字（會記在紀錄上）");
    const msgIn = el("input"); msgIn.maxLength = 120; msgIn.placeholder = t("這次改了什麼（可不填）");
    try { nameIn.value = localStorage.getItem(NAME_KEY) || ""; } catch {}
    who.append(nameIn, msgIn);
    adjust.append(faceBar, hint, sliders, reset, who);
    const log = el("div", "rt-pane rt-log"); log.hidden = true;
    panel.append(tabs, adjust, log);
    body.append(stage, panel);
    const dialog = el("div", "rt-dialog"); dialog.hidden = true;
    root.append(top, body, dialog);
    document.body.append(root);
    document.body.classList.add("rt-open");

    const sliderEls = {};
    for (const [k, label] of SLIDERS) {
      const row = el("label", "rt-slider");
      const name = el("span", "rt-slider-name", t(label)), val = el("span", "rt-slider-val", "0");
      const input = el("input"); input.type = "range"; input.min = -100; input.max = 100; input.step = 1; input.value = 0;
      input.addEventListener("input", () => setParam(k, Number(input.value)));
      val.title = t("點一下歸零");
      val.addEventListener("click", (e) => { e.preventDefault(); setParam(k, 0); });
      row.append(name, input, val);
      sliders.append(row);
      sliderEls[k] = { input, val };
    }

    // ----- 狀態 -----
    const selected = () => S.faces.find((f) => f.id === S.sel) || null;
    const isDirty = (f) => (f.saved ? !sameParams(f.params, f.saved.params) : f.prims.length > 0);
    const dirtyFaces = () => S.faces.filter(isDirty);
    const ordered = () => [...S.faces].sort((a, b) => (a.box[0] + a.box[2]) - (b.box[0] + b.box[2]));
    const faceLabel = (id) => { const i = ordered().findIndex((f) => f.id === id); return i < 0 ? t("臉 ?") : t("臉 {n}", { n: i + 1 }); };
    const setStatus = (text) => { status.textContent = text || ""; };
    let toastTimer;
    function toast(text) { setStatus(text); clearTimeout(toastTimer); toastTimer = setTimeout(() => { if (status.textContent === text) setStatus(""); }, 4000); }

    function setParam(k, v) {
      const f = selected();
      if (!f || !f.lm || S.preview) return;
      if (v) f.params[k] = v; else delete f.params[k];
      // 調回和存檔時一樣：用存檔那份元件（可能是別人算的），畫面才會和伺服器輸出的一樣
      f.prims = f.saved && sameParams(f.params, f.saved.params) ? f.saved.prims : buildPrims(f.lm, f.params);
      remesh(); refresh();
    }

    function remesh() {
      if (!S.renderer) return;
      const groups = S.preview ? Object.values(S.preview.state).map((f) => f.prims) : S.faces.map((f) => f.prims);
      S.renderer.setMesh(buildMesh(groups, S.asp));
      draw();
    }

    let drawQueued = false;
    function draw() {
      if (drawQueued) return;
      drawQueued = true;
      requestAnimationFrame(() => {
        drawQueued = false;
        if (!S.alive || !S.renderer) return;
        const w = stage.clientWidth, h = stage.clientHeight, dpr = Math.min(2, window.devicePixelRatio || 1);
        if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
        S.renderer.draw(S.view, w, h, S.original);
        for (const node of overlay.children) {
          const f = S.faces.find((x) => x.id === node.dataset.id);
          if (!f) continue;
          const b = f.box, pad = (b[2] - b[0]) * 0.12;
          node.style.left = `${(b[0] - pad) * S.view.s + S.view.x}px`; node.style.top = `${(b[1] - pad * 1.6) * S.view.s + S.view.y}px`;
          node.style.width = `${(b[2] - b[0] + pad * 2) * S.view.s}px`; node.style.height = `${(b[3] - b[1] + pad * 2.4) * S.view.s}px`;
        }
      });
    }

    /** 把 rect（等比座標）放進畫面；沒給就是整張圖 */
    function fit(rect) {
      const w = stage.clientWidth, h = stage.clientHeight;
      if (!w || !h) return;
      S.fitScale = Math.min(w / 1, h / S.asp) * 0.96;
      const r = rect || [0, 0, 1, S.asp];
      S.view.s = rect ? clamp(Math.min(w / (r[2] - r[0]), h / (r[3] - r[1])), S.fitScale, S.fitScale * 14) : S.fitScale;
      S.view.x = w / 2 - (r[0] + r[2]) / 2 * S.view.s; S.view.y = h / 2 - (r[1] + r[3]) / 2 * S.view.s;
      draw();
    }
    function focusFace(f) {
      const b = f.box, w = b[2] - b[0], h = b[3] - b[1];
      fit([b[0] - w * 0.9, b[1] - h * 0.9, b[2] + w * 0.9, b[3] + h * 0.7]);
    }
    function zoomAt(px, py, factor) {
      const s = clamp(S.view.s * factor, S.fitScale * 0.7, S.fitScale * 20), k = s / S.view.s;
      S.view.x = px - (px - S.view.x) * k; S.view.y = py - (py - S.view.y) * k; S.view.s = s;
      draw();
    }

    /** 畫面上所有會變的東西都在這裡更新 */
    function refresh() {
      const f = selected(), dirty = dirtyFaces(), locked = !!S.preview;
      save.disabled = S.busy || locked || !dirty.length;
      save.textContent = dirty.length ? t("儲存（{n} 張臉）", { n: dirty.length }) : t("儲存");
      tabLog.textContent = S.commits.length ? t("紀錄（{n}）", { n: S.commits.length }) : t("紀錄");
      // 臉的按鈕
      faceBar.textContent = "";
      for (const face of ordered()) {
        const b = el("button", "rt-chip" + (face.id === S.sel ? " on" : "") + (isDirty(face) ? " dirty" : face.prims.length ? " done" : ""), faceLabel(face.id));
        if (face.by && face.prims.length) b.title = t("{name} 修過", { name: face.by });
        b.onclick = () => select(face.id, true);
        faceBar.append(b);
      }
      const add = el("button", "rt-chip rt-add" + (S.adding ? " on" : ""), S.adding ? t("點照片上的臉…") : t("＋ 手動加臉"));
      add.disabled = !S.vision || locked;
      add.onclick = () => { S.adding = !S.adding; refresh(); };
      faceBar.append(add);
      // 照片上的框
      overlay.textContent = "";
      for (const face of ordered()) {
        const node = el("div", "rt-face" + (face.id === S.sel ? " on" : "") + (isDirty(face) ? " dirty" : face.prims.length ? " done" : ""));
        node.dataset.id = face.id;
        node.append(el("span", "", faceLabel(face.id) + (face.by && face.prims.length && !isDirty(face) ? ` · ${face.by}` : "")));
        overlay.append(node);
      }
      overlay.hidden = S.original;
      stage.classList.toggle("adding", S.adding);
      // 滑桿
      const usable = !!f && !!f.lm && !locked;
      for (const [k] of SLIDERS) {
        const v = f ? f.params[k] || 0 : 0;
        sliderEls[k].input.value = v; sliderEls[k].input.disabled = !usable;
        sliderEls[k].val.textContent = signed(v); sliderEls[k].val.classList.toggle("set", v !== 0);
      }
      reset.disabled = locked || !f || (!f.prims.length && !Object.keys(f.params).length);
      hint.textContent = locked ? t("正在看舊的版本，不能調整")
        : !S.faces.length ? (S.vision ? t("找不到臉。按「＋ 手動加臉」，再點照片上的臉") : "")
        : !f ? t("點照片上的臉，或上面的按鈕，選一張來調")
        : !f.lm ? t("這張臉抓不到特徵點，只能重設")
        : f.saved && f.by ? t("{name} 修過這張臉，你可以接著調", { name: f.by }) : "";
      hint.hidden = !hint.textContent;
      // 舊版本的提示
      banner.hidden = !locked;
      if (locked) {
        banner.textContent = "";
        const c = S.commits.find((x) => x.id === S.preview.id);
        banner.append(el("span", "", S.preview.id ? t("正在看 {who} 的版本 {id}", { who: c ? c.author : "", id: S.preview.id.slice(0, 7) }) : t("正在看原圖")));
        const use = el("button", "rt-btn rt-primary", t("還原到這版")), leave = el("button", "rt-btn", t("回到最新"));
        use.disabled = S.busy || S.preview.id === S.head;
        use.onclick = () => restore(S.preview.id);
        leave.onclick = () => { S.preview = null; remesh(); refresh(); renderLog(); };
        banner.append(use, leave);
      }
      draw();
    }

    function select(id, focus) {
      S.sel = id; S.adding = false;
      const f = selected();
      if (f && focus) focusFace(f);
      if (f) showTab(false);
      refresh();
    }

    function showTab(isLog) {
      tabAdjust.classList.toggle("on", !isLog); tabLog.classList.toggle("on", isLog);
      adjust.hidden = isLog; log.hidden = !isLog;
      if (isLog) renderLog();
    }
    tabAdjust.onclick = () => showTab(false);
    tabLog.onclick = () => showTab(true);

    // ----- 紀錄 -----
    function describe(c) {
      const parent = S.commits.find((x) => x.id === c.parent), before = parent ? parent.params : {}, lines = [];
      for (const fid of c.changed) {
        const now = c.params[fid], old = before[fid] || {};
        if (!now) { lines.push(t("{face}：{what}", { face: faceLabel(fid), what: t("重設") })); continue; }
        const parts = SLIDERS.filter(([k]) => (now[k] || 0) !== (old[k] || 0)).map(([k, label]) => `${t(label)} ${signed(now[k] || 0)}`);
        lines.push(t("{face}：{what}", { face: faceLabel(fid), what: parts.join(t("、")) || t("微調") }));
      }
      return lines;
    }
    function renderLog() {
      log.textContent = "";
      if (!S.commits.length) { log.append(el("p", "rt-hint", t("還沒有人修過這張。存一次就會多一筆紀錄，隨時可以還原到任何一筆。"))); return; }
      const row = (c) => {
        const item = el("div", "rt-commit" + (c && c.id === S.head ? " head" : "") + (S.preview && S.preview.id === (c ? c.id : null) ? " viewing" : ""));
        const headLine = el("div", "rt-commit-head");
        if (c) {
          headLine.append(el("b", "", c.author), el("span", "", timeAgo(c.at)), el("code", "", c.id.slice(0, 7)));
          if (c.id === S.head) headLine.append(el("em", "", t("目前")));
          item.append(headLine);
          if (c.kind === "restore") item.append(el("p", "", c.ref ? t("還原到 {id}", { id: c.ref.slice(0, 7) }) : t("還原成原圖")));
          if (c.message) item.append(el("p", "rt-commit-msg", c.message));
          for (const line of describe(c)) item.append(el("p", "rt-commit-diff", line));
        } else {
          headLine.append(el("b", "", t("原圖")));
          item.append(headLine);
        }
        const id = c ? c.id : null, actions = el("div", "rt-commit-actions");
        const look = el("button", "rt-btn", t("看這版")), use = el("button", "rt-btn", c ? t("還原到這版") : t("還原成原圖"));
        look.onclick = () => preview(id);
        use.disabled = S.busy || id === S.head || (!c && !S.faces.some((f) => f.saved));
        use.onclick = () => restore(id);
        if (id !== S.head) actions.append(look);
        actions.append(use);
        if (id !== S.head) item.append(actions);
        return item;
      };
      for (const c of S.commits) log.append(row(c));
      log.append(row(null));
    }

    async function preview(id) {
      try {
        const state = id ? (await getJSON(`${api}/commits/${id}`)).state : {};
        if (!S.alive) return;
        S.preview = { id, state }; S.sel = null;
        fit(); remesh(); refresh(); renderLog();
      } catch { toast(t("讀不到這一版")); }
    }

    // ----- 伺服器 -----
    async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(r.status); return r.json(); }
    async function post(url, body) {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      let data = {};
      try { data = await r.json(); } catch {}
      return { status: r.status, data };
    }

    /** 套用伺服器上的最新狀態。keepMine：我正在改、還沒存的臉不要蓋掉（存的時候伺服器會合併，撞到再問） */
    function applyRemote(data, keepMine) {
      const mine = keepMine ? dirtyFaces() : [];
      S.head = data.head; S.commits = data.commits || [];
      const remote = { ...data.state };
      for (const f of S.faces) {
        // 別人第一次修這張臉時取的 id 和我的不一樣：位置重疊就是同一張，改用伺服器上的 id
        const id = f.id in remote ? f.id : Object.keys(remote).find((k) => !S.faces.some((x) => x.id === k) && iou(remote[k].box, f.box) > 0.4);
        if (mine.includes(f)) { delete remote[id]; continue; }
        const r = id ? remote[id] : null;
        if (r) {
          if (S.sel === f.id) S.sel = id;
          f.id = id; f.saved = r; f.params = { ...r.params }; f.prims = r.prims; f.by = r.by || "";
          delete remote[id];
        } else { f.saved = null; f.params = {}; f.prims = []; f.by = ""; }
      }
      for (const [id, r] of Object.entries(remote)) {     // 我這邊沒找到、別人手動加的臉
        S.faces.push({ id, lm: null, box: r.box, params: { ...r.params }, prims: r.prims, saved: r, by: r.by || "" });
        locate(id);
      }
      if (!mine.length) S.parent = S.head;
      onChange(Object.keys(data.state || {}).length ? S.head : null);
      remesh(); refresh();
      if (!log.hidden) renderLog();
    }

    /** 幫沒有特徵點的臉（別人手動加的）補找一次 */
    async function locate(id) {
      const vision = S.vision, f = S.faces.find((x) => x.id === id);
      if (!vision || !f || f.lm) return;
      const iw = S.img.naturalWidth, b = f.box;
      const lm = landmarksAt(S.img, vision.loose, (b[0] + b[2]) / 2 * iw, (b[1] + b[3]) / 2 * iw, Math.max(b[2] - b[0], b[3] - b[1]) * iw * 2.2);
      if (lm && iou(boxOf(lm), b) > 0.3) { f.lm = lm; refresh(); }
    }

    function author() {
      const name = nameIn.value.trim().replace(/\s+/g, " ");
      if (!name) { showTab(false); nameIn.focus(); nameIn.classList.add("need"); toast(t("先填你的名字")); return null; }
      nameIn.classList.remove("need");
      try { localStorage.setItem(NAME_KEY, name); } catch {}
      return name;
    }

    async function commit(force) {
      const name = author(), dirty = dirtyFaces();
      if (!name || !dirty.length || S.busy) return;
      const faces = {};
      for (const f of dirty) faces[f.id] = f.prims.length ? { box: f.box, params: f.params, prims: f.prims } : null;
      S.busy = true; setStatus(t("儲存中…")); refresh();
      try {
        const { status: code, data } = await post(api, { parent: S.parent, author: name, message: msgIn.value.trim(), faces, force: !!force });
        if (!S.alive) return;
        S.busy = false;
        if (code === 200) { msgIn.value = ""; applyRemote(data, false); toast(t("已儲存")); }
        else if (code === 409 && data.conflicts) conflict(data);
        else { toast(data.detail || t("儲存失敗，再試一次")); refresh(); }
      } catch { S.busy = false; toast(t("儲存失敗，再試一次")); refresh(); }
    }

    /** 我改的臉在這段時間也被別人改了：讓使用者選用誰的 */
    function conflict(data) {
      const names = data.conflicts.map((id) => {
        const f = S.faces.find((x) => x.id === id || (data.state[id] && iou(data.state[id].box, x.box) > 0.4));
        const by = (data.state[id] || {}).by;
        const face = f ? faceLabel(f.id) : t("臉 ?");
        return by ? t("{face}（{name}）", { face, name: by }) : face;
      });
      dialog.textContent = "";
      const box = el("div", "rt-dialog-box");
      box.append(el("h3", "", t("有人先改了同一張臉")), el("p", "", t("{faces} 在你調的時候被別人存了新的版本。要用誰的？", { faces: names.join(t("、")) })));
      const mineBtn = el("button", "rt-btn rt-primary", t("用我的")), theirs = el("button", "rt-btn", t("用對方的"));
      mineBtn.onclick = () => { dialog.hidden = true; commit(true); };
      theirs.onclick = () => {
        dialog.hidden = true;
        // 撞到的臉放棄我的修改，其他臉的修改留著
        for (const id of data.conflicts) {
          const f = S.faces.find((x) => x.id === id || (data.state[id] && iou(data.state[id].box, x.box) > 0.4));
          if (f) { f.params = { ...((f.saved || {}).params || {}) }; f.prims = f.saved ? f.saved.prims : []; }
        }
        applyRemote(data, true);
        if (dirtyFaces().length) commit(false); else toast(t("已改用對方的版本"));
      };
      const actions = el("div", "rt-dialog-actions"); actions.append(theirs, mineBtn);
      box.append(actions); dialog.append(box); dialog.hidden = false;
      setStatus(""); refresh();
    }

    async function restore(id) {
      const name = author();
      if (!name || S.busy) return;
      if (dirtyFaces().length && !confirm(t("還沒儲存的修改會不見，確定要還原？"))) return;
      S.busy = true; setStatus(t("還原中…")); refresh();
      try {
        const { status: code, data } = await post(`${api}/restore`, { commit: id, author: name });
        if (!S.alive) return;
        S.busy = false;
        if (code === 200) { S.preview = null; applyRemote(data, false); toast(t("已還原")); }
        else { toast(data.detail || t("還原失敗，再試一次")); refresh(); }
      } catch { S.busy = false; toast(t("還原失敗，再試一次")); refresh(); }
      renderLog();
    }

    async function poll() {
      if (!S.alive || S.busy || document.hidden || !dialog.hidden) return;
      try {
        const data = await getJSON(`${api}?head=${S.head || "none"}`);
        if (!S.alive || S.busy || data.unchanged || data.head === S.head) return;
        const latest = (data.commits || [])[0];
        applyRemote(data, true);
        if (latest) toast(t("{name} 剛存了新的一筆", { name: latest.author }));
      } catch {}
    }
    const pollTimer = setInterval(poll, POLL_MS);

    // ----- 操作 -----
    const toUnit = (px, py) => [(px - S.view.x) / S.view.s, (py - S.view.y) / S.view.s];
    function tap(px, py) {
      if (S.preview) return;
      const [x, y] = toUnit(px, py);
      if (S.adding) { addAt(x, y); return; }
      const hit = S.faces.filter((f) => { const b = f.box, w = b[2] - b[0], h = b[3] - b[1]; return inBox([b[0] - w * 0.12, b[1] - h * 0.25, b[2] + w * 0.12, b[3] + h * 0.1], x, y); })
        .sort((a, b) => (a.box[2] - a.box[0]) - (b.box[2] - b.box[0]))[0];
      if (hit) select(hit.id, hit.id !== S.sel);
      else if (S.sel) { S.sel = null; refresh(); }
    }
    function addAt(x, y) {
      const iw = S.img.naturalWidth, short = Math.min(iw, S.img.naturalHeight);
      const existing = S.faces.find((f) => inBox(f.box, x, y));
      if (existing) { select(existing.id, true); return; }
      for (const f of [0.1, 0.2, 0.4, 0.8]) {
        const lm = landmarksAt(S.img, S.vision.loose, x * iw, y * iw, short * f);
        if (lm && inBox(boxOf(lm), x, y)) {
          const face = { id: newId(), lm, box: boxOf(lm), params: {}, prims: [], saved: null, by: "" };
          S.faces.push(face);
          select(face.id, true);
          return;
        }
      }
      toast(t("這裡找不到臉。放大一點，點在鼻子附近再試"));
    }

    const pointers = new Map();
    let gesture = null;
    stage.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button")) return;
      stage.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      const pts = [...pointers.values()];
      gesture = pts.length === 1 ? { kind: "pan", start: pts[0], last: pts[0], moved: false }
        : { kind: "pinch", d: dist(pts[0], pts[1]), c: mid(pts[0], pts[1]) };
    });
    stage.addEventListener("pointermove", (e) => {
      if (!pointers.has(e.pointerId) || !gesture) return;
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      const pts = [...pointers.values()], rect = stage.getBoundingClientRect();
      if (gesture.kind === "pan" && pts.length === 1) {
        if (!gesture.moved && dist(pts[0], gesture.start) < 5) return;
        gesture.moved = true;
        S.view.x += pts[0][0] - gesture.last[0]; S.view.y += pts[0][1] - gesture.last[1]; gesture.last = pts[0];
        draw();
      } else if (gesture.kind === "pinch" && pts.length >= 2) {
        const d = dist(pts[0], pts[1]), c = mid(pts[0], pts[1]);
        S.view.x += c[0] - gesture.c[0]; S.view.y += c[1] - gesture.c[1];
        zoomAt(c[0] - rect.left, c[1] - rect.top, d / (gesture.d || d));
        gesture.d = d; gesture.c = c;
      }
    });
    const release = (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      const g = gesture, rect = stage.getBoundingClientRect();
      gesture = pointers.size === 1 ? { kind: "pan", start: [...pointers.values()][0], last: [...pointers.values()][0], moved: true } : null;
      if (e.type === "pointerup" && g && g.kind === "pan" && !g.moved) tap(e.clientX - rect.left, e.clientY - rect.top);
    };
    stage.addEventListener("pointerup", release);
    stage.addEventListener("pointercancel", release);
    stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = stage.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)));
    }, { passive: false });

    const showOriginal = (on) => { if (S.original !== on) { S.original = on; refresh(); } };
    compare.addEventListener("pointerdown", (e) => { e.preventDefault(); showOriginal(true); });
    for (const type of ["pointerup", "pointerleave", "pointercancel"]) compare.addEventListener(type, () => showOriginal(false));
    fitBtn.onclick = () => fit();
    save.onclick = () => commit(false);
    reset.onclick = () => { const f = selected(); if (!f) return; f.params = {}; f.prims = []; remesh(); refresh(); };
    nameIn.addEventListener("input", () => nameIn.classList.remove("need"));

    function close() {
      if (dirtyFaces().length && !confirm(t("還沒儲存的修改會不見，確定離開？"))) return;
      S.alive = false; current = null;
      clearInterval(pollTimer);
      observer.disconnect();
      document.removeEventListener("keydown", onKey, true);
      if (S.renderer) S.renderer.destroy();
      root.remove();
      document.body.classList.remove("rt-open");
    }
    back.onclick = close;
    const onKey = (e) => {
      e.stopPropagation();          // 編輯器開著時，底下燈箱的方向鍵、Esc 不要動
      if (e.key === "Escape" && !e.target.closest("input")) { if (!dialog.hidden) return; if (S.preview) { S.preview = null; remesh(); refresh(); renderLog(); } else close(); }
    };
    document.addEventListener("keydown", onKey, true);
    const observer = new ResizeObserver(() => { const f = selected(); if (f) focusFace(f); else fit(); });

    // ----- 開始 -----
    (async () => {
      setStatus(t("讀取照片…"));
      const img = new Image();
      img.src = `/media/${key}/p/${photo.id}`;        // 永遠從原圖開始變形
      const dataP = getJSON(api);
      const visionP = loadVision(vendor);
      visionP.catch(() => {});
      try { await img.decode(); } catch { setStatus(t("照片讀不到")); return; }
      if (!S.alive) return;
      S.img = img; S.asp = img.naturalHeight / img.naturalWidth;
      S.renderer = createRenderer(canvas, img, S.asp);
      if (!S.renderer) { setStatus(t("這個瀏覽器不支援修容（需要 WebGL）")); return; }
      observer.observe(stage);
      fit(); remesh();
      let data;
      try { data = await dataP; } catch { setStatus(t("讀不到修容紀錄")); return; }
      if (!S.alive) return;
      applyRemote(data, false);      // 先把別人修好的畫出來
      setStatus(t("載入找臉的模型…（第一次比較久）"));
      let vision;
      try { vision = await visionP; } catch { setStatus(t("找臉的模型載入失敗，重新整理再試")); return; }
      if (!S.alive) return;
      S.vision = vision;
      const found = await detectFaces(img, vision, (i, n) => setStatus(t("找臉中… {p}%", { p: Math.round(i / n * 100) })), () => S.alive);
      if (!S.alive) return;
      for (const lm of found) {
        const box = boxOf(lm), known = S.faces.find((f) => iou(f.box, box) > 0.4);
        if (known) { if (!known.lm) { known.lm = lm; } }
        else S.faces.push({ id: newId(), lm, box, params: {}, prims: [], saved: null, by: "" });
      }
      for (const f of S.faces) if (!f.lm) await locate(f.id);
      toast(S.faces.length ? t("找到 {n} 張臉", { n: S.faces.length }) : "");
      refresh();
    })();
    refresh();
  }

  window.CulldropRetouch = { open };
})();
