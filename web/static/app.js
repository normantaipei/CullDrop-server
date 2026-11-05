// Culldrop 返圖頁：只給客戶看圖、下載。依星等分段可收合、無間隙 justified 排版、相機篩選。
(() => {
  const key = location.pathname.split("/").filter(Boolean).pop();
  const api = `/api/share/${key}`;
  const main = document.getElementById("main");
  const chipsEl = document.getElementById("chips");
  const metaEl = document.getElementById("meta");
  const dlAll = document.getElementById("dl-all");
  const rowHeight = () => (window.innerWidth < 640 ? 110 : 240);
  const camColors = ["#5B6CFF", "#F08A3C", "#4CC38A", "#A66CFF", "#FF6CB5", "#3CC7C0", "#F7B500", "#FF5C5C"];

  let album = null, cameraFilter = null, open = { 5: true, 4: true, 3: false, 2: false, 1: false }, visible = [];

  // 無間隙 justified：每格 flex-grow = 長寬比；整列等高、左右滿版；最後一列照片少時靠左不拉伸
  function justify(ratios, width, target) {
    const rows = []; let cur = [], sum = 0;
    const flush = (stretch) => {
      if (!cur.length) return;
      const h = stretch ? width / sum : target;
      rows.push({ h, cells: cur.map(i => ({ i, w: ratios[i] * h })) , stretch });
      cur = []; sum = 0;
    };
    ratios.forEach((r, i) => { cur.push(i); sum += r; if (sum * target >= width) flush(true); });
    flush(sum * target / width >= 0.6);
    return rows;
  }

  function ratio(p) { return p.w && p.h ? p.w / p.h : 1.5; }
  function camName(k) { return (album.cameras && album.cameras[k]) || (k ? k.slice(-6) : ""); }

  function render() {
    const photos = album.photos.filter(p => !cameraFilter || p.camera === cameraFilter);
    visible = [];
    metaEl.textContent = `${album.title}${album.shoot_date ? " · " + album.shoot_date.replace(/-/g, "_") : ""} · ${photoCount(photos.length)}`;
    document.title = `${album.title} · ${document.title.split(" · ").pop()}`;
    main.innerHTML = "";
    if (!photos.length) { main.innerHTML = `<div class="empty">${t("還沒有照片")}</div>`; return; }
    const width = main.clientWidth || window.innerWidth;
    for (let s = 5; s >= 1; s--) {
      const list = photos.filter(p => Math.min(5, Math.max(1, p.stars || 1)) === s)
        .sort((a, b) => (a.taken_at || "").localeCompare(b.taken_at || "") || a.name.localeCompare(b.name));
      if (!list.length) continue;
      const head = document.createElement("div");
      head.className = "section-head" + (open[s] ? "" : " closed");
      head.innerHTML = `<span class="stars">${"★".repeat(s)}</span><span class="count">${photoCount(list.length)}</span><span class="arrow">⌃</span>`;
      head.onclick = () => { open[s] = !open[s]; render(); };
      main.appendChild(head);
      if (!open[s]) continue;
      const grid = document.createElement("div"); grid.className = "grid";
      const rows = justify(list.map(ratio), width, rowHeight());
      for (const row of rows) {
        const rowEl = document.createElement("div"); rowEl.className = "row" + (row.stretch ? "" : " last");
        rowEl.style.height = row.h + "px";
        for (const c of row.cells) {
          const p = list[c.i]; const idx = visible.push(p) - 1;
          const a = document.createElement("a");
          a.className = "tile"; a.href = `/media/${key}/p/${p.id}`; a.style.width = c.w + "px"; a.style.height = row.h + "px";
          const img = document.createElement("img"); img.loading = "lazy"; img.decoding = "async";
          img.src = `/media/${key}/${rowHeight() > 200 ? "t" : "s"}/${p.id}`; img.alt = "";
          img.onload = () => img.classList.add("ok");
          a.appendChild(img);
          a.onclick = (e) => { e.preventDefault(); openLightbox(idx); };
          rowEl.appendChild(a);
        }
        grid.appendChild(rowEl);
      }
      main.appendChild(grid);
    }
  }

  function renderChips() {
    const cams = [];
    for (const p of album.photos) if (p.camera && !cams.includes(p.camera)) cams.push(p.camera);
    chipsEl.innerHTML = "";
    if (cams.length < 2) return;
    const mk = (label, k, color) => {
      const b = document.createElement("button"); b.className = "chip" + (cameraFilter === k ? " on" : "");
      b.innerHTML = (color ? `<span class="dot" style="background:${color}"></span>` : "") + label;
      b.onclick = () => { cameraFilter = k; renderChips(); render(); };
      chipsEl.appendChild(b);
    };
    mk(t("全部"), null, null);
    cams.forEach((k, i) => mk(camName(k), k, camColors[i % camColors.length]));
  }

  // 存到 iOS「照片」：<a download> 只會進「檔案」App，要用分享面板的「儲存影像」。
  // 只有 HTTPS 有 navigator.share；其他情況退回一般下載。
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const canSaveToPhotos = (() => {
    if (!isIOS || !navigator.share || !navigator.canShare) return false;
    try { return navigator.canShare({ files: [new File([new Uint8Array(1)], "x.jpg", { type: "image/jpeg" })] }); }
    catch { return false; }
  })();
  const jpgName = (n) => /\.jpe?g$/i.test(n || "") ? n : `${(n || "photo").replace(/\.[^.]+$/, "")}.jpg`;

  async function fetchFiles(photos, onProgress) {
    const files = [];
    for (const p of photos) {
      const r = await fetch(`/media/${key}/o/${p.id}`);
      if (!r.ok) throw new Error(r.status);
      files.push(new File([await r.blob()], jpgName(p.name), { type: "image/jpeg" }));
      onProgress && onProgress(files.length);
    }
    return files;
  }

  /** ok / cancel / needTap：下載太久瀏覽器不認這是使用者按的，要再按一次 */
  async function shareFiles(files) {
    try { await navigator.share({ files }); return "ok"; }
    catch (e) {
      if (e.name === "AbortError") return "cancel";
      if (e.name === "NotAllowedError") return "needTap";
      throw e;
    }
  }

  /** 分享面板一次塞太多會失敗：每批最多 20 張、200MB */
  function batches(photos, maxCount = 20, maxBytes = 2e8) {
    const out = []; let cur = [], size = 0;
    for (const p of photos) {
      if (cur.length && (cur.length >= maxCount || size + (p.bytes || 0) > maxBytes)) { out.push(cur); cur = []; size = 0; }
      cur.push(p); size += p.bytes || 0;
    }
    if (cur.length) out.push(cur);
    return out;
  }

  // 燈箱
  const lb = document.getElementById("lightbox"), lbImg = document.getElementById("lb-img");
  const lbDl = document.getElementById("lb-dl");
  let cur = -1, lbFiles = null, lbBusy = false;
  lbDl.addEventListener("click", async (e) => {
    if (!canSaveToPhotos) return;
    e.preventDefault();
    if (lbBusy) return;
    const photo = visible[cur];
    try {
      if (!lbFiles) {
        lbBusy = true; lbDl.textContent = t("下載中…");
        lbFiles = await fetchFiles([photo]);
        lbBusy = false;
        if (visible[cur] !== photo) { lbFiles = null; return; }   // 下載時翻到別張了
      }
      const r = await shareFiles(lbFiles);
      if (r === "needTap") { lbDl.textContent = t("點一下存到照片"); return; }
      lbFiles = null; lbDl.textContent = t("存到照片");
    } catch {
      lbBusy = false; lbFiles = null; lbDl.textContent = t("下載失敗，再試一次");
    }
  });
  function openLightbox(i) {
    cur = i; const p = visible[i];
    lbImg.src = `/media/${key}/p/${p.id}`;
    document.getElementById("lb-name").textContent = p.name;
    const info = [p.taken_at ? p.taken_at.slice(11, 16) : "", p.camera ? camName(p.camera) : "", p.stars ? "★".repeat(p.stars) : ""].filter(Boolean).join(" · ");
    document.getElementById("lb-info").textContent = info;
    lbDl.href = `/media/${key}/o/${p.id}`;
    lbFiles = null; lbDl.textContent = canSaveToPhotos ? t("存到照片") : t("下載 JPG");
    const raw = document.getElementById("lb-raw"); raw.hidden = !p.raw_name; raw.href = `/media/${key}/raw/${p.id}`;
    lb.hidden = false;
  }
  document.getElementById("lb-close").onclick = () => { lb.hidden = true; };
  document.getElementById("lb-prev").onclick = () => openLightbox((cur - 1 + visible.length) % visible.length);
  document.getElementById("lb-next").onclick = () => openLightbox((cur + 1) % visible.length);
  lb.onclick = (e) => { if (e.target === lb) lb.hidden = true; };
  document.addEventListener("keydown", (e) => {
    if (lb.hidden) return;
    if (e.key === "Escape") lb.hidden = true;
    if (e.key === "ArrowLeft") document.getElementById("lb-prev").click();
    if (e.key === "ArrowRight") document.getElementById("lb-next").click();
  });

  // 右上角的下載選單：全部 JPG / 全部 RAW / 全部，都是一個 ZIP
  const dlMenu = document.getElementById("dl-menu");
  const size = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`;
  function closeMenu() { dlMenu.hidden = true; dlAll.setAttribute("aria-expanded", "false"); }
  function renderMenu() {
    const raws = album.photos.filter(p => p.raw_name);
    const jpgBytes = album.photos.reduce((s, p) => s + (p.bytes || 0), 0), rawBytes = raws.reduce((s, p) => s + (p.raw_bytes || 0), 0);
    const item = (label, fmt, n, bytes) => {
      const a = document.createElement("a");
      a.href = `/api/share/${key}/zip${fmt === "jpg" ? "" : `?fmt=${fmt}`}`;
      a.setAttribute("download", ""); a.dataset.fmt = fmt;
      a.innerHTML = `${label}<span>${fileCount(n)} · ${size(bytes)}</span>`;
      a.onclick = closeMenu;
      return a;
    };
    dlMenu.innerHTML = "";
    if (canSaveToPhotos) {
      const b = document.createElement("button");
      b.id = "dl-photos";
      b.innerHTML = `${t("存到照片")}<span>${t("{n} 張 JPG", { n: album.photos.length })}</span>`;
      b.onclick = () => { closeMenu(); startJob(); };
      dlMenu.append(b);
    }
    dlMenu.append(item(t("全部 JPG"), "jpg", album.photos.length, jpgBytes));
    if (raws.length) {
      dlMenu.append(item(t("全部 RAW"), "raw", raws.length, rawBytes));
      dlMenu.append(item(t("全部"), "all", album.photos.length + raws.length, jpgBytes + rawBytes));
    }
    if (isIOS) { const p = document.createElement("p"); p.textContent = t("ZIP 會存到「檔案」App"); dlMenu.append(p); }
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".dl-wrap")) closeMenu(); });

  // 整本存到照片：下載好一批就等使用者按一次（分享面板一定要使用者自己按）
  const dlCancel = document.getElementById("dl-cancel");
  let job = null;
  const allLabel = () => t("下載 ▾");
  function endJob(text) { job = null; dlCancel.hidden = true; dlAll.textContent = text || allLabel(); }
  async function prepareBatch(j) {
    const list = j.list[j.i], mine = j;
    j.files = null;
    dlAll.textContent = t("下載第 {i}/{n} 批 {k}/{m}…", { i: j.i + 1, n: j.list.length, k: 0, m: list.length });
    try {
      const files = await fetchFiles(list, (k) => { if (job === mine) dlAll.textContent = t("下載第 {i}/{n} 批 {k}/{m}…", { i: j.i + 1, n: j.list.length, k, m: list.length }); });
      if (job !== mine) return;
      j.files = files;
      dlAll.textContent = t("存第 {i}/{n} 批（{m} 張）", { i: j.i + 1, n: j.list.length, m: list.length });
    } catch { if (job === mine) endJob(t("下載失敗，再試一次")); }
  }
  function startJob() {
    const photos = album.photos.filter(p => !cameraFilter || p.camera === cameraFilter);
    if (!photos.length) return;
    job = { list: batches(photos), i: 0, files: null, total: photos.length };
    dlCancel.hidden = false;
    prepareBatch(job);
  }
  dlAll.addEventListener("click", async () => {
    if (!job) {
      dlMenu.hidden = !dlMenu.hidden;
      dlAll.setAttribute("aria-expanded", String(!dlMenu.hidden));
      return;
    }
    if (!job.files) return;     // 還在下載
    const j = job;
    try {
      const r = await shareFiles(j.files);
      if (r !== "ok") return;   // 取消或要再按一次：這批留著
      j.i += 1;
      if (j.i >= j.list.length) endJob(t("已處理 {n} 張", { n: j.total }));
      else prepareBatch(j);
    } catch { endJob(t("下載失敗，再試一次")); }
  });
  dlCancel.addEventListener("click", () => endJob());
  let resizeTimer; window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => album && render(), 150); });

  fetch(api).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(a => { album = a; renderChips(); render(); renderMenu(); })
    .catch(() => { main.innerHTML = `<div class="empty">${t("找不到這本相簿")}</div>`; });
})();
