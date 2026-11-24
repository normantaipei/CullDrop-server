"""修容（捏臉）的純邏輯：檢查資料、合併兩個人的修改、把原圖照設定變形輸出。路由和資料表在 main.py。

非破壞式：原圖不動。每張臉存的是一組「變形元件」，伺服器照著把原圖變形後另存一份。

座標：轉正後照片寬 = 1 的等比座標（x ∈ [0, 1]、y ∈ [0, 高 / 寬]），和解析度無關，
所以返圖頁用 2048 的預覽圖調、伺服器用原尺寸輸出，結果一樣。

元件（返圖頁 web/static/retouch.js 的 displace() 用同一套公式畫預覽，兩邊要一起改）：
  ["m", cx, cy, r, mx, my]            推：圓心附近的內容移動 (mx, my)，離圓心越遠動越少，到 r 為 0
  ["s", cx, cy, ux, uy, rx, ry, k]    縮放：以圓心放大（k > 0）或縮小；橢圓，長軸方向 (ux, uy)
輸出的點 x 取樣自原圖的 x + D(x)，D 是所有元件的和，權重 w(t) = (1 − t²)²。

皮膚與膚色（磨皮、去瑕疵、美白…）是另一種東西：不搬動內容，只改顏色。做法一樣是返圖頁把滑桿換成幾種通用的「圖層」，
伺服器只認圖層，不管滑桿的意思（返圖頁 retouch.js 的 paintCrop() 是同一套公式）。圖層先做，變形後做，所以遮罩用的是原圖上的位置。
  shapes：{名字: [x0, y0, x1, y1, …]}，臉上各部位的多邊形（輪廓、眼、眉、唇…），圖層用名字指定範圍。
          身體的皮膚沒辦法用多邊形描，用點陣遮罩：{名字: {"box": [x0, y0, x1, y1], "png": 灰階 PNG 的 base64}}，
          是返圖頁用人像分割模型算出來的，最大 256 × 256，貼到 box 的位置。只有 color 圖層能用
  每個圖層的前三項都是 [要的形狀], [挖掉的形狀], 邊緣羽化；接著：
  ["band", …, s1, s2, gain, lo, hi]                 去掉 s1～s2 之間粗細的明暗不均（磨皮、去瑕疵）。和周圍的亮度差在 lo 以下的全去，
                                                     到 hi 以上的不動：差很多的是五官的輪廓，不是瑕疵
  ["color", …, gamma, [r, g, b 倍率], [r, g, b 加], sat, lo, hi]   調色；lo < hi 時只作用在亮度 lo 以上的地方（牙齒）
  ["even", …, sigma, k]                              把大範圍的色偏拉回整張臉的平均（均勻膚色），不動明暗
  ["shine", …, sigma, k, thr]                        壓掉比周圍亮的反光（去油光）
  長度（羽化、s1、s2、sigma）都是等比座標；亮度是 0～1。

一張臉：{"box": [x0, y0, x1, y1], "params": {滑桿名: -100～100}, "prims": [元件…], "shapes": {…}, "layers": [圖層…], "by": 誰改的, "at": 時間}
狀態：{臉的 id: 臉}。params 只給介面顯示與比較用，輸出只看 prims、shapes、layers。
"""

import base64
import binascii
import io
import math
import os
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageMath, ImageOps, ImageStat, JpegImagePlugin

MAX_FACES = 64
MAX_PRIMS = 96
MAX_PARAMS = 48
MAX_SHAPES = 24
MAX_POINTS = 64     # 一個形狀最多幾個點
MAX_LAYERS = 24
MAX_SIGMA = 0.1
MAX_RASTER = 256        # 點陣遮罩的邊長上限
MAX_RASTER_TEXT = 120000
MAX_MOVE = 0.5        # 推的距離最多是半徑的一半：再多內容會摺疊
MAX_SCALE = 0.5
MAX_RADIUS = 0.5
MAX_CELLS = 60000     # 一塊最多切幾格
FACE_ID_RE = re.compile(r"^[0-9a-z]{6,16}$")
PARAM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,23}$")
LUMA = (0.299, 0.587, 0.114)      # 和 Pillow 轉灰階用的一樣


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
        # 元件要在這張臉附近：改臉的人不能順便動到照片的其他地方。往下多留一個臉高，脖子在那裡
        cx, cy = _num(vx, x0 - bw * 0.5, x1 + bw * 0.5), _num(vy, y0 - bh * 0.5, y1 + bh)
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
    raw_shapes = raw.get("shapes") or {}
    if not isinstance(raw_shapes, dict) or len(raw_shapes) > MAX_SHAPES:
        raise Invalid("shapes")
    shapes = {}
    for name in sorted(raw_shapes):
        pts = raw_shapes[name]
        if not isinstance(name, str) or not PARAM_RE.match(name):
            raise Invalid("shape")
        if isinstance(pts, dict):
            shapes[name] = _clean_raster(pts, (x0, y0, x1, y1), aspect)
            continue
        if not isinstance(pts, list) or len(pts) % 2 or not 6 <= len(pts) <= MAX_POINTS * 2:
            raise Invalid("shape")
        flat = []
        for i in range(0, len(pts), 2):
            flat += center(pts[i], pts[i + 1])
        shapes[name] = flat

    raw_layers = raw.get("layers") or []
    if not isinstance(raw_layers, list) or len(raw_layers) > MAX_LAYERS:
        raise Invalid("layers")
    layers = []
    for layer in raw_layers:
        clean = _clean_layer(layer, shapes)
        if clean:
            layers.append(clean)
    used = {n for layer in layers for n in layer[1] + layer[2]}
    shapes = {n: pts for n, pts in shapes.items() if n in used}
    if not prims and not layers:
        return None
    face = {"box": [x0, y0, x1, y1], "params": params, "prims": prims}
    if layers:
        face["shapes"], face["layers"] = shapes, layers
    return face


def _clean_raster(raw: dict, face_box, aspect: float) -> dict:
    """點陣遮罩（身體的皮膚）。範圍可以比臉大很多，但還是要在這個人附近：左右各四個臉寬、往下十個臉高。"""
    x0, y0, x1, y1 = face_box
    bw, bh = x1 - x0, y1 - y0
    box, png = raw.get("box"), raw.get("png")
    if not isinstance(box, list) or len(box) != 4 or not isinstance(png, str) or not 0 < len(png) <= MAX_RASTER_TEXT:
        raise Invalid("raster")
    rx0, rx1 = _num(box[0], max(-0.5, x0 - 4 * bw), min(1.5, x1 + 4 * bw)), _num(box[2], max(-0.5, x0 - 4 * bw), min(1.5, x1 + 4 * bw))
    ry0, ry1 = _num(box[1], max(-0.5, y0 - 1.5 * bh), min(aspect + 0.5, y1 + 10 * bh)), _num(box[3], max(-0.5, y0 - 1.5 * bh), min(aspect + 0.5, y1 + 10 * bh))
    if rx1 - rx0 < 0.002 or ry1 - ry0 < 0.002:
        raise Invalid("raster")
    try:
        with Image.open(io.BytesIO(base64.b64decode(png, validate=True))) as im:
            if im.format != "PNG" or max(im.size) > MAX_RASTER:      # 先看檔頭的尺寸，太大的不解開
                raise Invalid("raster")
            im.convert("L")
    except (binascii.Error, OSError, ValueError, SyntaxError) as e:
        raise Invalid("raster") from e
    return {"box": [rx0, ry0, rx1, ry1], "png": png}


def _names(value, shapes: dict, required: bool):
    if not isinstance(value, list) or len(value) > 8 or (required and not value) or any(not isinstance(n, str) or n not in shapes for n in value):
        raise Invalid("layer shapes")
    return list(value)


def _triple(value, lo: float, hi: float):
    if not isinstance(value, list) or len(value) != 3:
        raise Invalid("triple")
    return [_num(v, lo, hi) for v in value]


def _clean_layer(layer, shapes: dict):
    """檢查一個圖層；沒有效果的回傳 None。"""
    sizes = {"band": 9, "color": 10, "even": 6, "shine": 7}
    if not isinstance(layer, list) or not layer or layer[0] not in sizes or len(layer) != sizes[layer[0]]:
        raise Invalid("layer")
    kind = layer[0]
    head = [kind, _names(layer[1], shapes, True), _names(layer[2], shapes, False), _num(layer[3], 0, MAX_SIGMA)]
    if any(isinstance(shapes[n], dict) for n in head[2]) or (kind != "color" and any(isinstance(shapes[n], dict) for n in head[1])):
        raise Invalid("raster layer")       # 點陣遮罩的範圍大，只給便宜的調色用，也不能拿來挖洞
    if kind == "band":
        s1, s2 = _num(layer[4], 0, MAX_SIGMA), _num(layer[5], 1e-5, MAX_SIGMA)
        gain, lo, hi = _num(layer[6], 0, 1), _num(layer[7], 0, 1), _num(layer[8], 0.004, 1)
        if s1 >= s2 or lo >= hi:
            raise Invalid("band")
        return head + [s1, s2, gain, lo, hi] if gain else None
    if kind == "color":
        gamma, mul, add = _num(layer[4], 0.4, 2.5), _triple(layer[5], 0.5, 1.6), _triple(layer[6], -0.3, 0.3)
        sat, lo, hi = _num(layer[7], 0, 2.5), _num(layer[8], 0, 1), _num(layer[9], 0, 1)
        if gamma == 1 and mul == [1, 1, 1] and add == [0, 0, 0] and sat == 1:
            return None
        return head + [gamma, mul, add, sat, lo, hi]
    if kind == "even":
        sigma, k = _num(layer[4], 1e-5, MAX_SIGMA), _num(layer[5], 0, 1)
        return head + [sigma, k] if k else None
    sigma, k, thr = _num(layer[4], 1e-5, MAX_SIGMA), _num(layer[5], 0, 1), _num(layer[6], 0, 0.99)
    return head + [sigma, k, thr] if k else None


def same(a, b) -> bool:
    """兩張臉的修容內容一樣（不看是誰、什麼時候改的）。"""
    if a is None or b is None:
        return a is None and b is None
    return (a["prims"] == b["prims"] and a["params"] == b["params"]
            and (a.get("layers") or []) == (b.get("layers") or []) and (a.get("shapes") or {}) == (b.get("shapes") or {}))


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


# ---------- 圖層（皮膚、膚色）----------

def _blur(im: Image.Image, sigma_px: float) -> Image.Image:
    return im.filter(ImageFilter.GaussianBlur(sigma_px)) if sigma_px >= 0.3 else im


def _math(expr, **images) -> Image.Image:
    return ImageMath.lambda_eval(expr, **images)


def _to_l(a, value):
    """浮點 → 8 位元（四捨五入；超出範圍的會被截掉）"""
    return a["convert"](value + 0.5, "L")


def _mask(size, origin, shapes: dict, include, exclude, feather_px: float, width: int) -> Image.Image:
    """多邊形 → 遮罩（要的填白、挖掉的填黑，再把邊緣糊開）"""
    mask = Image.new("L", size, 0)
    for names, fill in ((include, 255), (exclude, 0)):
        for name in names:
            pts = shapes[name]
            if isinstance(pts, dict):       # 點陣遮罩：放大到 box 的大小，和已經畫的取聯集
                bx0, by0, bx1, by1 = (round(v * width) for v in pts["box"])
                tile = Image.open(io.BytesIO(base64.b64decode(pts["png"]))).convert("L").resize((max(1, bx1 - bx0), max(1, by1 - by0)), Image.Resampling.BILINEAR)
                layer = Image.new("L", size, 0)
                layer.paste(tile, (bx0 - origin[0], by0 - origin[1]))
                mask = ImageChops.lighter(mask, layer)
            else:
                ImageDraw.Draw(mask).polygon([(pts[i] * width - origin[0], pts[i + 1] * width - origin[1]) for i in range(0, len(pts), 2)], fill=fill)
    return mask, _blur(mask, feather_px)


def _smoothstep(v: float, lo: float, hi: float) -> float:
    t = min(1.0, max(0.0, (v - lo) / (hi - lo)))
    return t * t * (3 - 2 * t)


def _band(crop, mask, s1, s2, gain, lo, hi):
    g1, g2 = _blur(crop, s1), _blur(crop, s2)
    # 每個點要去掉多少：和周圍差一點點的（毛孔、斑、痘）全去，差很多的（鼻孔、雙眼皮、髮絲）不動
    amount = ImageChops.difference(g1.convert("L"), g2.convert("L")).point([round(gain * (1 - _smoothstep(v / 255, lo, hi)) * 255) for v in range(256)])
    bands = [_math(lambda a: _to_l(a, a["float"](a["c"]) - (a["float"](a["g1"]) - a["float"](a["g2"])) * a["float"](a["k"]) / 255), c=c, g1=b1, g2=b2, k=amount)
             for c, b1, b2 in zip(crop.split(), g1.split(), g2.split())]
    return Image.composite(Image.merge("RGB", bands), crop, mask)


def _color(crop, mask, gamma, mul, add, sat, lo, hi):
    lut = []
    for m, d in zip(mul, add):
        lut += [min(255, max(0, round((((v / 255) ** gamma) * m + d) * 255))) for v in range(256)]
    out = crop.point(lut)
    if sat != 1:
        out = ImageEnhance.Color(out).enhance(sat)
    if lo < hi:         # 只作用在夠亮的地方
        mask = ImageChops.multiply(mask, crop.convert("L").point([round(_smoothstep(v / 255, lo, hi) * 255) for v in range(256)]))
    return Image.composite(out, crop, mask)


def _even(crop, mask, hard, sigma, k):
    low = _blur(crop, sigma)
    stat = ImageStat.Stat(crop, hard)
    if not stat.count[0]:
        return crop
    mean = stat.mean
    mean_l = sum(m * w for m, w in zip(mean, LUMA))
    low_l = low.convert("L")
    bands = [_math(lambda a: _to_l(a, a["float"](a["c"]) - ((a["float"](a["g"]) - a["float"](a["gl"])) - a["off"]) * k),
                   c=c, g=g, gl=low_l, off=m - mean_l) for c, g, m in zip(crop.split(), low.split(), mean)]
    return Image.composite(Image.merge("RGB", bands), crop, mask)


def _shine(crop, mask, sigma, k, thr):
    lum = crop.convert("L")
    drop = _math(lambda a: a["max"](a["float"](a["l"]) - a["float"](a["gl"]), 0) * a["float"](a["w"]) * (k / 255),
                 l=lum, gl=_blur(crop, sigma).convert("L"), w=lum.point([round(_smoothstep(v / 255, thr, 1) * 255) for v in range(256)]))
    bands = [_math(lambda a: _to_l(a, a["float"](a["c"]) - a["d"]), c=c, d=drop) for c in crop.split()]
    return Image.composite(Image.merge("RGB", bands), crop, mask)


def paint_region(face: dict, width: int, height: int):
    """這張臉的圖層會動到的範圍（像素）：用到的形狀的外框，加上模糊需要的邊。沒有圖層回傳 None。"""
    layers, shapes = face.get("layers") or [], face.get("shapes") or {}
    if not layers:
        return None
    xs, ys, pad = [], [], 0.0
    for layer in layers:
        for name in layer[1]:
            if isinstance(shapes[name], dict):
                box = shapes[name]["box"]
                xs += [box[0], box[2]]
                ys += [box[1], box[3]]
            else:
                xs += shapes[name][0::2]
                ys += shapes[name][1::2]
        reach = max(layer[5] if layer[0] == "band" else layer[4] if layer[0] in ("even", "shine") else 0, layer[3])
        pad = max(pad, reach)
    pad = pad * 3 * width + 2
    x0, y0 = max(0, math.floor(min(xs) * width - pad)), max(0, math.floor(min(ys) * width - pad))
    x1, y1 = min(width, math.ceil(max(xs) * width + pad)), min(height, math.ceil(max(ys) * width + pad))
    return (x0, y0, x1, y1) if x1 - x0 >= 2 and y1 - y0 >= 2 else None


def paint_crop(crop: Image.Image, face: dict, origin, width: int):
    """對一張臉的範圍做完所有圖層。回傳 (結果, 動到的地方)；width 是整張圖的寬（座標換算用）。"""
    shapes, masks = face["shapes"], {}
    out, touched = crop, Image.new("L", crop.size, 0)
    for layer in face["layers"]:
        key = (tuple(layer[1]), tuple(layer[2]), layer[3])
        if key not in masks:
            masks[key] = _mask(crop.size, origin, shapes, layer[1], layer[2], layer[3] * width, width)
        hard, mask = masks[key]
        if layer[0] == "band":
            out = _band(out, mask, layer[4] * width, layer[5] * width, layer[6], layer[7], layer[8])
        elif layer[0] == "color":
            out = _color(out, mask, *layer[4:])
        elif layer[0] == "even":
            out = _even(out, mask, hard, layer[4] * width, layer[5])
        else:
            out = _shine(out, mask, layer[4] * width, layer[5], layer[6])
        touched = ImageChops.lighter(touched, mask)
    return out, touched.point(lambda v: 255 if v else 0)


def paint(im: Image.Image, state: dict) -> Image.Image:
    """做所有臉的圖層（直接改 im）。每一份都從沒動過的原圖取，再只把動到的地方貼回去。
    身體的圖層（用點陣遮罩的）範圍大，和臉的分開算；先貼所有人的身體，再貼所有人的臉（返圖頁的預覽也是這個順序）。"""
    width, height = im.size
    jobs = []
    for body in (True, False):
        for fid in sorted(state):
            face, shapes = state[fid], state[fid].get("shapes") or {}
            layers = [layer for layer in face.get("layers") or [] if any(isinstance(shapes[n], dict) for n in layer[1]) == body]
            part = dict(face, layers=layers)
            region = paint_region(part, width, height)
            if region:
                jobs.append((region, im.crop(region), part))
    for region, crop, face in jobs:
        gray = crop.mode != "RGB"
        out, touched = paint_crop(crop.convert("RGB") if gray else crop, face, region[:2], width)
        im.paste(out.convert(im.mode) if gray else out, region[:2], touched)
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
    paint(im, state)            # 先調顏色（遮罩是原圖上的位置），再變形
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
