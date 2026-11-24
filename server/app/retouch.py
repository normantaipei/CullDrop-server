"""修容（捏臉）的純邏輯：檢查資料、合併兩個人的修改、把原圖照設定變形輸出。路由和資料表在 main.py。

非破壞式：原圖不動。每張臉存的是一組「變形元件」，伺服器照著把原圖變形後另存一份。

座標：轉正後照片寬 = 1 的等比座標（x ∈ [0, 1]、y ∈ [0, 高 / 寬]），和解析度無關，
所以返圖頁用 2048 的預覽圖調、伺服器用原尺寸輸出，結果一樣。

元件（返圖頁 web/static/retouch.js 的 displace() 用同一套公式畫預覽，兩邊要一起改）：
  ["m", cx, cy, r, mx, my]            推：圓心附近的內容移動 (mx, my)，離圓心越遠動越少，到 r 為 0
  ["s", cx, cy, ux, uy, rx, ry, k]    縮放：以圓心放大（k > 0）或縮小；橢圓，長軸方向 (ux, uy)
輸出的點 x 取樣自原圖的 x + D(x)，D 是所有元件的和，權重 w(t) = (1 − t²)²。

一張臉：{"box": [x0, y0, x1, y1], "params": {滑桿名: -100～100}, "prims": [元件…], "by": 誰改的, "at": 時間}
狀態：{臉的 id: 臉}。params 只給介面顯示與比較用，輸出只看 prims。
"""

import math
import os
import re
from pathlib import Path

from PIL import Image, ImageOps, JpegImagePlugin

MAX_FACES = 64
MAX_PRIMS = 96
MAX_PARAMS = 24
MAX_MOVE = 0.5        # 推的距離最多是半徑的一半：再多內容會摺疊
MAX_SCALE = 0.5
MAX_RADIUS = 0.5
MAX_CELLS = 60000     # 一塊最多切幾格
FACE_ID_RE = re.compile(r"^[0-9a-z]{6,16}$")
PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,23}$")


class Invalid(ValueError):
    pass


def _num(v, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise Invalid("number")
    return round(float(v), 6)


def clean_face(raw, aspect: float):
    """檢查一張臉的資料並整理成固定格式；沒有任何變形（等於重設）回傳 None。aspect = 高 / 寬。"""
    if not isinstance(raw, dict):
        raise Invalid("face")
    box = raw.get("box")
    if not isinstance(box, list) or len(box) != 4:
        raise Invalid("box")
    x0, x1 = _num(box[0], -0.5, 1.5), _num(box[2], -0.5, 1.5)
    y0, y1 = _num(box[1], -0.5, aspect + 0.5), _num(box[3], -0.5, aspect + 0.5)
    bw, bh = x1 - x0, y1 - y0
    if bw < 0.002 or bh < 0.002:
        raise Invalid("box")
    reach = max(bw, bh)

    def center(vx, vy):
        # 元件要在這張臉附近：改臉的人不能順便動到照片的其他地方
        cx, cy = _num(vx, x0 - bw * 0.5, x1 + bw * 0.5), _num(vy, y0 - bh * 0.5, y1 + bh * 0.5)
        return cx, cy

    raw_params = raw.get("params") or {}
    if not isinstance(raw_params, dict) or len(raw_params) > MAX_PARAMS:
        raise Invalid("params")
    params = {}
    for k in sorted(raw_params):
        if not isinstance(k, str) or not PARAM_RE.match(k):
            raise Invalid("params")
        v = _num(raw_params[k], -100, 100)
        if v:
            params[k] = v

    raw_prims = raw.get("prims") or []
    if not isinstance(raw_prims, list) or len(raw_prims) > MAX_PRIMS:
        raise Invalid("prims")
    prims = []
    for p in raw_prims:
        if not isinstance(p, list) or not p:
            raise Invalid("prim")
        if p[0] == "m" and len(p) == 6:
            cx, cy = center(p[1], p[2])
            r = _num(p[3], 1e-4, min(MAX_RADIUS, reach))
            mx, my = _num(p[4], -r, r), _num(p[5], -r, r)
            if math.hypot(mx, my) > MAX_MOVE * r + 1e-6:
                raise Invalid("move")
            if mx or my:
                prims.append(["m", cx, cy, r, mx, my])
        elif p[0] == "s" and len(p) == 8:
            cx, cy = center(p[1], p[2])
            ux, uy = _num(p[3], -1, 1), _num(p[4], -1, 1)
            n = math.hypot(ux, uy)
            if n < 1e-3:
                raise Invalid("axis")
            rx, ry = _num(p[5], 1e-4, min(MAX_RADIUS, reach)), _num(p[6], 1e-4, min(MAX_RADIUS, reach))
            k = _num(p[7], -MAX_SCALE, MAX_SCALE)
            if k:
                prims.append(["s", cx, cy, round(ux / n, 6), round(uy / n, 6), rx, ry, k])
        else:
            raise Invalid("prim")
    if not prims:
        return None
    return {"box": [x0, y0, x1, y1], "params": params, "prims": prims}


def same(a, b) -> bool:
    """兩張臉的修容內容一樣（不看是誰、什麼時候改的）。"""
    if a is None or b is None:
        return a is None and b is None
    return a["prims"] == b["prims"] and a["params"] == b["params"]


def iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def merge(base: dict, head: dict, changes: dict, force: bool = False):
    """把一個人的修改接到最新一版上。base 是他開始改時看到的狀態，head 是現在最新的，
    changes 是 {臉 id: 臉 或 None（重設）}。回傳 (新狀態, 動到的臉, 撞到的臉)。

    每個人修自己的臉，所以以臉為單位合併：他沒動的臉照最新的，他動的臉用他的。
    他動的臉在這段時間也被別人改過就算撞到；撞到時整筆不收（除非 force），讓他決定用誰的。"""
    new = dict(head)
    changed, conflicts = [], []
    for fid, face in changes.items():
        target = fid
        if face is not None and fid not in head:
            # 兩個人同時第一次修同一張臉，各自取了 id：位置重疊就當同一張
            target = next((hid for hid, hf in head.items() if iou(hf["box"], face["box"]) > 0.4), fid)
        theirs = head.get(target)
        if same(theirs, face):
            continue
        if not same(theirs, base.get(target)) and not force:
            conflicts.append(target)
            continue
        if face is None:
            new.pop(target, None)
        else:
            new[target] = face
        changed.append(target)
    if conflicts:
        return dict(head), [], conflicts
    return new, changed, []


# ---------- 變形 ----------

def _radius(p) -> float:
    return p[3] if p[0] == "m" else max(p[5], p[6])


def _extent(p):
    r = _radius(p)
    return p[1] - r, p[2] - r, p[1] + r, p[2] + r


def _one(p, x: float, y: float):
    """一個元件在 (x, y) 的位移。"""
    ex, ey = x - p[1], y - p[2]
    if p[0] == "m":
        t2 = (ex * ex + ey * ey) / (p[3] * p[3])
        if t2 >= 1:
            return 0.0, 0.0
        w = (1 - t2) * (1 - t2)
        return -p[4] * w, -p[5] * w
    a = (ex * p[3] + ey * p[4]) / p[5]
    b = (ey * p[3] - ex * p[4]) / p[6]
    t2 = a * a + b * b
    if t2 >= 1:
        return 0.0, 0.0
    w = p[7] * (1 - t2) * (1 - t2)
    return -w * ex, -w * ey


def displace(prims, x: float, y: float):
    """輸出的點 (x, y) 要從原圖的哪裡取樣：回傳位移 D，取樣點是 (x + dx, y + dy)。"""
    dx = dy = 0.0
    for p in prims:
        a, b = _one(p, x, y)
        dx += a
        dy += b
    return dx, dy


def clusters(state: dict, width: int, height: int):
    """每張臉會動到的範圍（像素）；重疊的併成一塊，回傳 [((x0, y0, x1, y1), 元件)]。
    併起來之後每一塊的邊界上位移都是 0，貼回去才不會有接縫。"""
    items = []
    for face in state.values():
        prims = face.get("prims") or []
        if not prims:
            continue
        ext = [_extent(p) for p in prims]
        x0 = max(0, math.floor(min(e[0] for e in ext) * width))
        y0 = max(0, math.floor(min(e[1] for e in ext) * width))
        x1 = min(width, math.ceil(max(e[2] for e in ext) * width))
        y1 = min(height, math.ceil(max(e[3] for e in ext) * width))
        if x1 - x0 >= 2 and y1 - y0 >= 2:
            items.append([[x0, y0, x1, y1], list(prims)])
    merged = True
    while merged:
        merged = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i][0], items[j][0]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    items[i] = [[min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])], items[i][1] + items[j][1]]
                    del items[j]
                    merged = True
                    break
            if merged:
                break
    return [(tuple(box), prims) for box, prims in items]


def _edges(start: int, length: int, n: int):
    return [start + round(length * i / n) for i in range(n + 1)]


def mesh(box, prims, width: int, height: int):
    """把一塊切成格子，算出每格要從原圖的哪個四邊形取樣（Pillow MESH 的格式）。
    沒有位移的格子整排併成一格。"""
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    cell = min(48.0, max(4.0, min(_radius(p) for p in prims) * width / 8))
    nx, ny = max(1, math.ceil(cw / cell)), max(1, math.ceil(ch / cell))
    while nx * ny > MAX_CELLS:
        nx, ny = max(1, math.ceil(nx / 1.25)), max(1, math.ceil(ny / 1.25))
    xs, ys = _edges(x0, cw, nx), _edges(y0, ch, ny)
    dx = [[0.0] * (nx + 1) for _ in range(ny + 1)]
    dy = [[0.0] * (nx + 1) for _ in range(ny + 1)]
    for p in prims:
        ex0, ey0, ex1, ey1 = (v * width for v in _extent(p))
        i0 = max(0, math.floor((ex0 - x0) / cw * nx) - 1)
        i1 = min(nx, math.ceil((ex1 - x0) / cw * nx) + 1)
        j0 = max(0, math.floor((ey0 - y0) / ch * ny) - 1)
        j1 = min(ny, math.ceil((ey1 - y0) / ch * ny) + 1)
        for j in range(j0, j1 + 1):
            y = ys[j] / width
            for i in range(i0, i1 + 1):
                a, b = _one(p, xs[i] / width, y)
                dx[j][i] += a * width
                dy[j][i] += b * width

    def src(i, j):
        return min(float(width), max(0.0, xs[i] + dx[j][i])), min(float(height), max(0.0, ys[j] + dy[j][i]))

    def still(i, j):
        return not (dx[j][i] or dy[j][i] or dx[j + 1][i] or dy[j + 1][i])

    data = []
    for j in range(ny):
        if ys[j + 1] <= ys[j]:
            continue
        i = 0
        while i < nx:
            k = i + 1
            if still(i, j) and still(i + 1, j):
                while k < nx and still(k + 1, j):
                    k += 1
            if xs[k] > xs[i]:
                nw, sw, se, ne = src(i, j), src(i, j + 1), src(k, j + 1), src(k, j)
                data.append(((xs[i] - x0, ys[j] - y0, xs[k] - x0, ys[j + 1] - y0), (*nw, *sw, *se, *ne)))
            i = k
    return data


def warp(im: Image.Image, state: dict) -> Image.Image:
    """照設定把圖變形（直接改 im）。im 要是轉正過的。"""
    width, height = im.size
    patches = []
    for box, prims in clusters(state, width, height):
        size = (box[2] - box[0], box[3] - box[1])
        patches.append((box, im.transform(size, Image.Transform.MESH, mesh(box, prims, width, height), Image.Resampling.BICUBIC)))
    for box, patch in patches:      # 全部算完才貼：每一塊都要從沒動過的原圖取樣
        im.paste(patch, (box[0], box[1]))
    return im


def _save(im: Image.Image, dst: Path, fmt: str, **options):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    try:
        im.save(tmp, fmt, **options)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def render(src: Path, state: dict, full: Path, derived=()):
    """原圖 + 設定 → 修容後的原尺寸 JPG（保留 EXIF 與色彩描述檔），外加縮圖。
    derived 是 [(路徑, 長邊, "JPEG" 或 "WEBP")]，和 main.make_derivatives 的規格一致。"""
    with Image.open(src) as im:
        sampling = JpegImagePlugin.get_sampling(im) if im.format == "JPEG" else -1
        icc = im.info.get("icc_profile")
        exif = im.getexif()
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.load()
    exif.pop(0x0112, None)      # 已經轉正
    warp(im, state)
    options = {"quality": 95, "exif": exif}
    if sampling in (0, 1, 2) and im.mode == "RGB":
        options["subsampling"] = sampling
    if icc:
        options["icc_profile"] = icc
    for dst, edge, fmt in derived:
        scale = min(1.0, edge / max(im.size))
        small = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS, reducing_gap=3.0) if scale < 1 else im
        if fmt == "JPEG":
            _save(small, dst, "JPEG", quality=88, optimize=True, progressive=True)
        else:
            _save(small, dst, "WEBP", quality=80, method=4)
    _save(im, full, "JPEG", **options)     # 最後才寫：這個檔在 = 整組都好了
