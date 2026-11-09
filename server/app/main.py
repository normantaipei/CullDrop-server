"""
Culldrop 伺服器：一台伺服器、多本相簿。

- 手機（Culldrop app）是相簿的管理系統：上傳、建相簿、刪照片、改星，每個操作都用裝置 token。
- 返圖頁 /a/<分享碼> 與 /api/share、/media 不用 token，給客戶 / coser 看圖、下載。
- 上傳協定與 Culldrop app 既有的實作一致（HEAD / PUT /api/uploads/{id}、Content-Range、X-SnapPipe-* header；
  也接受 X-Culldrop-* 同名 header）。

環境變數：
  CULLDROP_DATA            資料目錄（預設 ./data）：originals/ derived/ tmp/ culldrop.db
  CULLDROP_DEVICE_TOKENS   "iphone:token1,ipad:token2"（必填；逗號、空白或換行分隔）
  CULLDROP_PUBLIC_URL      對外網址（組分享連結用，例 https://photos.example.tw）
  CULLDROP_WEB             返圖頁靜態檔目錄（預設 ../../web）
  CULLDROP_SITE_NAME       返圖頁標題後綴（預設依語言：返圖 / Photos / 写真）
  CULLDROP_LANG            沒帶語言的請求用什麼語言（zh-Hant / en / ja，預設 zh-Hant）
  CULLDROP_MIN_FREE_GB     磁碟剩不到這麼多就拒收（507，預設 2）
  CULLDROP_MAX_INFLIGHT    每台裝置同時進行中的上傳數上限（預設 4）
  CULLDROP_DEVICE_NETS     限制裝置來源網段（逗號分隔 CIDR；不設 = 不限，只靠 token）
  CULLDROP_TRUSTED_PROXIES 反向代理的 IP 或 CIDR（逗號分隔）；只有從這裡來的連線才信 X-Forwarded-For，不設 = 都不信
  CULLDROP_NAS_DIR         NAS 掛載點；設了就把收到的檔案備份到 <NAS>/<相簿>/，手機等備份完才刪檔。
                           目錄裡要有 .culldrop-nas 標記檔才算掛上（沒掛上時掛載點是空資料夾，不能寫進去當備份）
  CULLDROP_NAS_POLL        沒工作時多久看一次（秒，預設 5）
"""

import fcntl
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import i18n

# ---------- 設定 ----------

DATA_DIR = Path(os.environ.get("CULLDROP_DATA", "./data")).expanduser().resolve()
PUBLIC_URL = os.environ.get("CULLDROP_PUBLIC_URL", "").rstrip("/")
WEB_DIR = Path(os.environ.get("CULLDROP_WEB", Path(__file__).resolve().parents[2] / "web")).expanduser().resolve()
SITE_NAME = os.environ.get("CULLDROP_SITE_NAME", "").strip()   # 空的話依語言
MIN_FREE = int(os.environ.get("CULLDROP_MIN_FREE_GB", "2")) * (1 << 30)
MAX_INFLIGHT = int(os.environ.get("CULLDROP_MAX_INFLIGHT", "4"))
AUTH_FAILS, AUTH_BAN_SEC = 10, 600
NAS_DIR = Path(os.environ["CULLDROP_NAS_DIR"]).expanduser() if os.environ.get("CULLDROP_NAS_DIR", "").strip() else None
NAS_MARKER = ".culldrop-nas"
NAS_POLL_SEC = float(os.environ.get("CULLDROP_NAS_POLL", "5"))
NAS_MAX_BACKOFF = 3600

THUMB_EDGE, SMALL_EDGE, PREVIEW_EDGE = 800, 400, 2048
OG_SIZE = (1200, 630)         # 通訊軟體連結預覽圖
OG_MIN_STARS = 4              # 預覽圖貼出去就收不回來：只用挑過的照片
PAIR_TOLERANCE = 2.0          # JPG / RAW 拍攝時間相差幾秒內算同一張
UPLOAD_STALE_SEC = 6 * 3600   # 沒動靜的半成品上傳多久後清掉

# 手機端 API 的版本。加了 app 要靠它才能用的功能就加一；只能往上加東西，舊版 app 用到的不能改、不能拿掉。
# app 從 /api/manage/me 的 "api" 讀（舊伺服器沒有這一欄 = 1），比 app 認得的舊就提醒使用者更新伺服器。
#   1 = 上傳、相簿管理、人物分享連結
#   2 = 人物同步（/api/manage/albums/{slug}/face-tags；只有名字和照片清單，舊版 app 用）
#   3 = 人臉分群完整同步（/api/manage/faces；人物、每張臉、人臉向量）
SERVER_API = 3

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = re.compile(r"^[\w.\-]{1,100}$")
NAME_RE = re.compile(r"^[\w.\-]{1,40}$")
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{20,64}$")
PERSON_RE = re.compile(r"^[\w\-]{1,64}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
RAW_EXT = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".dng", ".pef", ".srw"}
JPG_EXT = {".jpg", ".jpeg", ".jpe"}

for sub in ("originals", "derived", "tmp/uploads"):
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "culldrop.db"


def _nets(env: str):
    v = os.environ.get(env, "").strip()
    return [ipaddress.ip_network(n.strip(), strict=False) for n in v.split(",") if n.strip()] if v else None


DEVICE_NETS = _nets("CULLDROP_DEVICE_NETS")
TRUSTED_PROXIES = _nets("CULLDROP_TRUSTED_PROXIES") or []


def _device_tokens() -> dict:
    out = {}
    for item in re.split(r"[,\s]+", os.environ.get("CULLDROP_DEVICE_TOKENS", "").strip()):
        if ":" in item:
            name, tok = item.split(":", 1)
            if NAME_RE.match(name) and tok:
                out[tok] = name
    return out


DEVICE_TOKENS = _device_tokens()

# ---------- DB ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    shoot_date TEXT,
    notes TEXT,
    share_key TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id),
    photo_id TEXT NOT NULL,          -- 原始 JPG 的 SHA-256
    filename TEXT NOT NULL,
    stars INTEGER,
    aesthetic REAL,
    tags TEXT NOT NULL DEFAULT '[]',
    taken_at TEXT,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    raw_sha TEXT,
    raw_name TEXT,
    raw_bytes INTEGER,
    camera TEXT,
    creator TEXT,
    source TEXT NOT NULL DEFAULT 'culldrop',
    device TEXT,
    uploaded_at REAL NOT NULL,
    updated_at REAL,
    UNIQUE (album_id, photo_id)
);
CREATE INDEX IF NOT EXISTS photos_album ON photos(album_id);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    album TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,              -- jpeg / raw
    total INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    camera TEXT,
    taken_at TEXT,
    stars INTEGER,
    tags TEXT,
    creator TEXT,
    source TEXT,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    photo_id TEXT
);
CREATE TABLE IF NOT EXISTS raw_pending (   -- RAW 比 JPG 先到：先收著，JPG 入庫時再配對
    id INTEGER PRIMARY KEY,
    device TEXT NOT NULL, album TEXT NOT NULL, stem TEXT NOT NULL,
    sha256 TEXT NOT NULL, filename TEXT NOT NULL, bytes INTEGER NOT NULL,
    taken_at TEXT, camera TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS album_people (  -- 相簿裡一個人的分享連結：只看得到這個人的照片
    id INTEGER PRIMARY KEY,
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL,         -- 手機端的 id；人臉資料不上傳，這裡只有「這張照片有誰」
    name TEXT NOT NULL,
    share_key TEXT UNIQUE NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (album_id, person_id)
);
CREATE TABLE IF NOT EXISTS album_people_photos (
    person_pk INTEGER NOT NULL REFERENCES album_people(id) ON DELETE CASCADE,
    photo_id TEXT NOT NULL,
    PRIMARY KEY (person_pk, photo_id)
);
CREATE TABLE IF NOT EXISTS album_face_tags (   -- 人物同步：哪張照片有誰。只有名字，沒有人臉資料；連同一台伺服器的其他手機拿來對齊名字和分類
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    photo_id TEXT NOT NULL,
    PRIMARY KEY (album_id, name, photo_id)
);
CREATE TABLE IF NOT EXISTS album_face_revs (   -- 每寫一次加一：手機帶著上次讀到的版本來寫，中間被別支改過就要重讀
    album_id INTEGER PRIMARY KEY REFERENCES albums(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,
    epoch TEXT NOT NULL,             -- 清空後重新開始會換一個：手機靠它知道手上記的「上次同步」已經不能用
    device TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS face_sync (     -- 人臉分群完整同步：一筆一筆的紀錄，手機用 seq 拉「上次之後變了什麼」
    kind TEXT NOT NULL,              -- person（人物）/ face（一張臉）/ photo（這張照片分群過了，沒有臉也算）
    id TEXT NOT NULL,                -- person、face 是手機產生的 UUID；photo 是照片的 SHA-256
    photo_id TEXT,                   -- face 所在的照片：刪照片時一起清
    data TEXT NOT NULL DEFAULT '{}', -- person: name、created；face: person、vector（base64）、box
    deleted INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL,            -- 每寫一筆加一，全伺服器共用
    source TEXT,                     -- 最後寫的那支手機（安裝時產生的代號）：拉的時候不用把自己寫的再下載一次
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS face_sync_seq ON face_sync(seq);
CREATE INDEX IF NOT EXISTS face_sync_photo ON face_sync(photo_id);
CREATE TABLE IF NOT EXISTS face_sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nas_files (     -- 每個檔案一列（只有 RAW 的照片不在 photos 表，也要備份）
    sha TEXT NOT NULL,
    album TEXT NOT NULL,
    kind TEXT NOT NULL,              -- jpeg / raw
    filename TEXT NOT NULL,
    path TEXT,                       -- 相對 NAS_DIR；決定後不變，重試時才會蓋同一個檔
    done_at REAL,
    meta_dirty INTEGER NOT NULL DEFAULT 1,   -- 星等 / 標籤要重寫
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (sha, album, kind)
);
CREATE TABLE IF NOT EXISTS cameras (       -- 手機端的相機顯示名稱（返圖頁 chip 用）
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    creator TEXT,
    updated_at REAL NOT NULL
);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


with db() as _con:
    _con.executescript(SCHEMA)


def new_key() -> str:
    return secrets.token_urlsafe(9)


# ---------- 路徑 ----------

def original_path(sha: str) -> Path:
    return DATA_DIR / "originals" / sha[:2] / f"{sha}.jpg"


def raw_path(sha: str, name: str) -> Path:
    return DATA_DIR / "originals" / sha[:2] / f"{sha}{Path(name).suffix.lower()}"


def derived_path(photo_id: str, kind: str) -> Path:
    ext = "jpg" if kind == "p" else "webp"
    return DATA_DIR / "derived" / photo_id[:2] / f"{photo_id}_{kind}.{ext}"


def og_path(photo_id: str) -> Path:
    return DATA_DIR / "derived" / photo_id[:2] / f"{photo_id}_og.jpg"


def upload_tmp(upload_id: str) -> Path:
    return DATA_DIR / "tmp" / "uploads" / f"{upload_id}.part"


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def store(tmp: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        tmp.unlink(missing_ok=True)
    else:
        shutil.move(str(tmp), str(dest))


def _unlink(p: Path):
    try:
        p.unlink()
    except FileNotFoundError:
        pass


# ---------- 影像 ----------

def make_derivatives(src: Path, photo_id: str):
    """預覽 2048 / 縮圖 800 / 小縮圖 400（轉正）。回傳轉正後的寬高。已有的不重做。"""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        for kind, edge in (("p", PREVIEW_EDGE), ("t", THUMB_EDGE), ("s", SMALL_EDGE)):
            dst = derived_path(photo_id, kind)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy = im.copy()
            copy.thumbnail((edge, edge), Image.LANCZOS)
            if kind == "p":
                copy.save(dst, "JPEG", quality=88, optimize=True, progressive=True)
            else:
                copy.save(dst, "WEBP", quality=80, method=4)
    return w, h


def make_og(photo_id: str) -> Path:
    """1200×630 預覽圖：照片完整放中間，兩旁用自己模糊放大填滿，直式照片的臉才不會被切掉。"""
    dst = og_path(photo_id)
    if dst.exists():
        return dst
    src = derived_path(photo_id, "p")
    if not src.exists():
        make_derivatives(original_path(photo_id), photo_id)
    with Image.open(src) as im:
        im = im.convert("RGB")
        canvas = ImageOps.fit(im, OG_SIZE).filter(ImageFilter.GaussianBlur(28))
        canvas = ImageEnhance.Brightness(canvas).enhance(0.55)
        fg = ImageOps.contain(im, OG_SIZE, Image.LANCZOS)
        canvas.paste(fg, ((OG_SIZE[0] - fg.width) // 2, (OG_SIZE[1] - fg.height) // 2))
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    canvas.save(tmp, "JPEG", quality=85, optimize=True, progressive=True)
    os.replace(tmp, dst)
    return dst


def exif_info(path: Path) -> dict:
    """拍攝時間與機身序號（有就讀，沒有就算了）。"""
    out = {}
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            ifd = ex.get_ifd(0x8769)
            dt = ifd.get(36867) or ex.get(36867) or ex.get(306)
            if dt and len(str(dt)) >= 19:
                s = str(dt)
                out["taken_at"] = f"{s[0:4]}-{s[5:7]}-{s[8:10]}T{s[11:19]}"
            serial = ifd.get(42033)
            if serial:
                out["camera"] = str(serial).strip()[:60]
    except Exception:
        pass
    return out


def check_raw(path: Path, name: str):
    """看檔頭：TIFF（ARW / NEF / DNG / CR2 / PEF / ORF / RW2）、CR3（ISO BMFF）、RAF。"""
    ext = Path(name).suffix.lower()
    with open(path, "rb") as f:
        head = f.read(16)
    ok = (head[:4] in (b"II*\x00", b"MM\x00*", b"IIRO", b"IIRS", b"IIU\x00")
          or (ext == ".cr3" and head[4:12] == b"ftypcrx ")
          or (ext == ".raf" and head.startswith(b"FUJIFILM")))
    if not ok:
        raise ValueError(f"{ext} 檔頭不符")


# ---------- 身分 ----------

_auth_fails: dict = {}


def _ip(text: str):
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def client_ip(request: Request) -> str:
    """真正的來源。經過反向代理時連線對象都是代理，要看它加的 X-Forwarded-For。
    只取最右邊那個：那是代理自己加的，左邊的都可能是對方偽造的。"""
    peer = request.client.host if request.client else ""
    ip = _ip(peer)
    if ip is None or not any(ip in n for n in TRUSTED_PROXIES):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1]
    real = _ip(forwarded)
    return str(real) if real else peer


def _bearer(authorization: Optional[str]) -> str:
    return (authorization or "").removeprefix("Bearer ").strip()


def require_device(request: Request, authorization: Optional[str]) -> str:
    """裝置 token → 裝置名稱。所有手機端操作都要過這裡。"""
    if DEVICE_NETS is not None:
        ip = _ip(client_ip(request))
        if ip is None:
            raise HTTPException(403, "無法辨識來源 IP")
        if not any(ip in n for n in DEVICE_NETS):
            raise HTTPException(403, f"來源 {ip} 不在允許的網段")
    if not DEVICE_TOKENS:
        raise HTTPException(500, "伺服器未設定 CULLDROP_DEVICE_TOKENS")
    ip = client_ip(request)
    # 先比 token：正確的永遠放行。先看封鎖的話，別人連錯幾次就能把手機一起鎖在外面
    given = _bearer(authorization).encode()
    for tok, name in DEVICE_TOKENS.items():
        if hmac.compare_digest(given, tok.encode()):
            _auth_fails.pop(ip, None)
            return name
    rec = _auth_fails.get(ip)
    if rec and rec[0] >= AUTH_FAILS and time.time() - rec[1] < AUTH_BAN_SEC:
        raise HTTPException(429, "token 錯太多次，暫時封鎖", headers={"Retry-After": str(AUTH_BAN_SEC)})
    rec = _auth_fails.setdefault(ip, [0, 0.0])
    if time.time() - rec[1] > AUTH_BAN_SEC:
        rec[0] = 0
    rec[0] += 1
    rec[1] = time.time()
    if len(_auth_fails) > 10000:
        _auth_fails.clear()
    raise HTTPException(401, "裝置 token 錯誤")


# ---------- 共用查詢 / JSON ----------

def share_url(key: str) -> str:
    return f"{PUBLIC_URL}/a/{key}" if PUBLIC_URL else f"/a/{key}"


def album_by_slug(con, slug: str):
    row = con.execute("SELECT * FROM albums WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "沒有這本相簿")
    return row


def share_view(con, key: str):
    """分享碼 → (相簿, 看得到的 photo_id, 標題)。個人連結只看得到自己的照片；整本的 photo_ids 是 None。"""
    a = con.execute("SELECT * FROM albums WHERE share_key=?", (key,)).fetchone()
    if a:
        return a, None, a["title"]
    p = con.execute("SELECT * FROM album_people WHERE share_key=?", (key,)).fetchone()
    if not p:
        raise HTTPException(404, "沒有這本相簿")
    a = con.execute("SELECT * FROM albums WHERE id=?", (p["album_id"],)).fetchone()
    ids = {r["photo_id"] for r in con.execute("SELECT photo_id FROM album_people_photos WHERE person_pk=?", (p["id"],))}
    return a, ids, f"{a['title']} · {p['name']}"


def cover_photo(con, album_id: int, visible) -> Optional[str]:
    """這個連結的預覽圖用哪張：看得到的照片裡星等最高的；沒有 ★4 以上就不放。"""
    rows = con.execute("SELECT photo_id FROM photos WHERE album_id=? AND stars>=? ORDER BY stars DESC, taken_at IS NULL, taken_at, filename",
                       (album_id, OG_MIN_STARS)).fetchall()
    return next((r["photo_id"] for r in rows if visible is None or r["photo_id"] in visible), None)


def og_tags(key: str, title: str, description: str, cover: Optional[str], lang: str) -> str:
    """通訊軟體抓預覽用；網址要完整，沒設 CULLDROP_PUBLIC_URL 就只有文字。"""
    if not PUBLIC_URL:
        return ""
    tags = [("og:type", "website"), ("og:site_name", site_name(lang)), ("og:url", f"{PUBLIC_URL}/a/{key}")]
    if cover:
        tags += [("og:image", f"{PUBLIC_URL}/og/{key}/{cover[:20]}.jpg"), ("og:image:type", "image/jpeg"),
                 ("og:image:width", str(OG_SIZE[0])), ("og:image:height", str(OG_SIZE[1]))]
    out = [f'<meta property="{k}" content="{html.escape(v)}">' for k, v in tags]
    if cover:
        out.append('<meta name="twitter:card" content="summary_large_image">')
    return "\n".join(out)


def ensure_album(con, slug: str, title: Optional[str] = None, shoot_date: Optional[str] = None):
    row = con.execute("SELECT * FROM albums WHERE slug=?", (slug,)).fetchone()
    now = time.time()
    if not row:
        con.execute("INSERT INTO albums (slug, title, shoot_date, share_key, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (slug, title or slug, shoot_date, new_key(), now, now))
    else:
        # 標題明確給了才改；拍攝日只在相簿還沒有時補上（上傳時從 EXIF 猜的日期不該蓋掉使用者設的）
        if (title and title != row["title"]) or (shoot_date and not row["shoot_date"]):
            con.execute("UPDATE albums SET title=COALESCE(?, title), shoot_date=COALESCE(shoot_date, ?) WHERE id=?",
                        (title, shoot_date, row["id"]))
    return con.execute("SELECT * FROM albums WHERE slug=?", (slug,)).fetchone()


ORDER = "ORDER BY stars IS NULL, stars DESC, taken_at IS NULL, taken_at, filename"


def star_counts(con, album_id: int) -> dict:
    out = {str(s): 0 for s in range(1, 6)}
    out["unrated"] = 0
    for r in con.execute("SELECT stars, COUNT(*) AS n FROM photos WHERE album_id=? GROUP BY stars", (album_id,)):
        key = str(r["stars"]) if r["stars"] in (1, 2, 3, 4, 5) else "unrated"
        out[key] += r["n"]
    return out


def album_json(con, a) -> dict:
    n = con.execute("SELECT COUNT(*) FROM photos WHERE album_id=?", (a["id"],)).fetchone()[0]
    return {"slug": a["slug"], "title": a["title"], "shoot_date": a["shoot_date"], "notes": a["notes"],
            "share_key": a["share_key"], "share_url": share_url(a["share_key"]),
            "created_at": a["created_at"], "updated_at": a["updated_at"], "photos": n, "stars": star_counts(con, a["id"])}


def photo_json(r) -> dict:
    return {"id": r["photo_id"], "sha": r["photo_id"], "name": r["filename"], "stars": r["stars"],
            "aesthetic": r["aesthetic"], "tags": json.loads(r["tags"] or "[]"), "taken_at": r["taken_at"],
            "w": r["width"], "h": r["height"], "bytes": r["bytes"],
            "raw_name": r["raw_name"] if r["raw_sha"] else None, "raw_bytes": r["raw_bytes"] if r["raw_sha"] else None,
            "camera": r["camera"], "creator": r["creator"], "source": r["source"], "status": "done"}


def camera_names(con) -> dict:
    return {r["key"]: {"name": r["name"], "creator": r["creator"]} for r in con.execute("SELECT * FROM cameras")}


# ---------- NAS 備份 ----------
# 伺服器的檔案用 SHA 命名，NAS 上要看得懂：<NAS>/<相簿>/<原檔名>。JPG 寫進星等標籤，RAW 用 .xmp 附檔。
# 刪照片不動 NAS（NAS 是備份，誤刪救得回來）。

def nas_ready() -> bool:
    try:
        return NAS_DIR is not None and (NAS_DIR / NAS_MARKER).is_file()
    except OSError:
        return False


def nas_enqueue(con, sha: str, album: str, kind: str, filename: str):
    if NAS_DIR is None:
        return
    con.execute("""INSERT INTO nas_files (sha, album, kind, filename) VALUES (?,?,?,?)
                   ON CONFLICT(sha, album, kind) DO UPDATE SET meta_dirty=meta_dirty+1, next_at=0""",
                (sha, album, kind, filename))


def nas_touch(con, *shas):
    """星等 / 標籤 / 配對變了：重寫 NAS 上的 metadata。"""
    if NAS_DIR is None:
        return
    for sha in filter(None, shas):
        con.execute("UPDATE nas_files SET meta_dirty=meta_dirty+1, next_at=0 WHERE sha=?", (sha,))


def backup_state(con, sha: str) -> str:
    """none = 沒設 NAS，手機傳完就能刪；pending = 要等；done = NAS 已有。"""
    if NAS_DIR is None:
        return "none"
    row = con.execute("SELECT 1 FROM nas_files WHERE sha=? AND done_at IS NOT NULL LIMIT 1", (sha,)).fetchone()
    return "done" if row else "pending"


def nas_backfill():
    """NAS 是後來才設的：之前收的照片也要備份。"""
    with db() as con:
        for r in con.execute("SELECT p.photo_id, p.filename, p.raw_sha, p.raw_name, a.slug FROM photos p JOIN albums a ON a.id=p.album_id").fetchall():
            con.execute("INSERT OR IGNORE INTO nas_files (sha, album, kind, filename) VALUES (?,?,'jpeg',?)", (r["photo_id"], r["slug"], r["filename"]))
            if r["raw_sha"]:
                con.execute("INSERT OR IGNORE INTO nas_files (sha, album, kind, filename) VALUES (?,?,'raw',?)", (r["raw_sha"], r["slug"], r["raw_name"]))
        for r in con.execute("SELECT sha256, album, filename FROM raw_pending").fetchall():
            con.execute("INSERT OR IGNORE INTO nas_files (sha, album, kind, filename) VALUES (?,?,'raw',?)", (r["sha256"], r["album"], r["filename"]))


def _nas_dest(con, row) -> str:
    folder = row["album"].strip(".") or "album"     # slug 可以是 ..，不能當路徑
    name = safe_filename(row["filename"])
    rel = f"{folder}/{name}"
    taken = con.execute("SELECT 1 FROM nas_files WHERE path=? LIMIT 1", (rel,)).fetchone()
    if taken or (NAS_DIR / rel).exists():
        rel = f"{folder}/{Path(name).stem}_{row['sha'][:8]}{Path(name).suffix}"
    return rel


def _nas_copy(src: Path, dest: Path):
    """先寫 .part、讀回來比對 SHA 才換名：網路斷在一半不會留下壞檔。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    shutil.copyfile(src, part)
    if sha256_path(part) != sha256_path(src):
        _unlink(part)
        raise OSError(f"{dest.name} 寫到 NAS 後內容不符")
    os.replace(part, dest)


def _exiftool(target: Path, stars, tags: list) -> Optional[str]:
    """寫星等標籤。失敗回傳原因、不丟例外：檔案到 NAS 比標籤重要，寫不進去也要算備份完成。"""
    if not shutil.which("exiftool"):
        return "沒有 exiftool，星等標籤沒寫進檔案"
    if target.suffix == ".xmp" and not stars and not tags:
        return None
    args = ["exiftool", "-q", "-q", "-overwrite_original", "-charset", "iptc=UTF8", "-codedcharacterset=utf8"]
    if stars:
        args.append(f"-XMP:Rating={stars}")
    if target.suffix != ".xmp":
        args += [f"-IPTC:Keywords={t}" for t in tags] or ["-IPTC:Keywords="]
    args += [f"-XMP:Subject={t}" for t in tags] or ["-XMP:Subject="]
    try:
        subprocess.run(args + [str(target)], check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        return f"{target.name} 寫星等標籤失敗：{e}"
    return None


def nas_export(row) -> Optional[str]:
    """備份一個檔案；回傳不影響備份的警告。"""
    sha, kind = row["sha"], row["kind"]
    src = original_path(sha) if kind == "jpeg" else raw_path(sha, row["filename"])
    with db() as con:
        if not src.exists():     # 還沒備份就被刪掉了
            con.execute("DELETE FROM nas_files WHERE sha=? AND album=? AND kind=?", (sha, row["album"], kind))
            return None
        rel = row["path"]
        if not rel:
            rel = _nas_dest(con, row)
            con.execute("UPDATE nas_files SET path=? WHERE sha=? AND album=? AND kind=?", (rel, sha, row["album"], kind))
        col = "photo_id" if kind == "jpeg" else "raw_sha"
        meta = con.execute(f"SELECT p.stars, p.tags FROM photos p JOIN albums a ON a.id=p.album_id WHERE p.{col}=? AND a.slug=?", (sha, row["album"])).fetchone()
    dest = NAS_DIR / rel
    stars, tags = (meta["stars"], json.loads(meta["tags"] or "[]")) if meta else (None, [])
    tmp = DATA_DIR / "tmp" / f"nas_{sha}_{kind}"
    if kind == "jpeg":
        work = tmp.with_suffix(".jpg")
        try:
            shutil.copyfile(src, work)
            warning = _exiftool(work, stars, tags)
            _nas_copy(src if warning else work, dest)
        finally:
            _unlink(work)
        return warning
    if not row["done_at"]:
        _nas_copy(src, dest)
    if not meta:
        return None
    side = tmp.with_suffix(".xmp")
    _unlink(side)
    try:
        warning = _exiftool(side, stars, tags)
        if side.exists():
            _nas_copy(side, dest.with_suffix(".xmp"))
    finally:
        _unlink(side)
    return warning


def nas_tick() -> bool:
    """做一件工作；回傳有沒有事做。"""
    if not nas_ready():
        return False
    now = time.time()
    with db() as con:
        row = con.execute("""SELECT rowid, * FROM nas_files WHERE (done_at IS NULL OR meta_dirty>0) AND next_at<=?
                             ORDER BY done_at IS NOT NULL, rowid LIMIT 1""", (now,)).fetchone()
    if not row:
        return False
    key = (row["sha"], row["album"], row["kind"])
    try:
        warning = nas_export(row)
    except Exception as e:
        delay = min(NAS_MAX_BACKOFF, 30 * 2 ** min(row["attempts"], 7))
        with db() as con:
            con.execute("UPDATE nas_files SET attempts=attempts+1, next_at=?, error=? WHERE sha=? AND album=? AND kind=?",
                        (time.time() + delay, str(e)[:300], *key))
        return True
    with db() as con:
        # 減掉這次讀到的數：做的時候又改星的話還會再寫一次
        con.execute("""UPDATE nas_files SET done_at=COALESCE(done_at, ?), meta_dirty=MAX(0, meta_dirty-?), attempts=0, error=?
                       WHERE sha=? AND album=? AND kind=?""", (time.time(), row["meta_dirty"], warning, *key))
    return True


def nas_worker():
    # uvicorn 開多個 worker 時只讓一個做
    lock = open(DATA_DIR / "nas.lock", "w")
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(30)
    nas_backfill()
    while True:
        try:
            if not nas_tick():
                time.sleep(NAS_POLL_SEC)
        except Exception:
            time.sleep(NAS_POLL_SEC)


def nas_summary(con) -> dict:
    if NAS_DIR is None:
        return {"enabled": False}
    r = con.execute("SELECT COUNT(*) AS n, COUNT(error) AS failed FROM nas_files WHERE done_at IS NULL").fetchone()   # 已完成的 error 只是警告
    err = con.execute("SELECT error FROM nas_files WHERE error IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
    return {"enabled": True, "available": nas_ready(), "pending": r["n"], "failed": r["failed"], "last_error": err["error"] if err else None}


# ---------- App ----------

@asynccontextmanager
async def lifespan(_app):
    if NAS_DIR is not None:
        threading.Thread(target=nas_worker, daemon=True, name="nas").start()
    yield


app = FastAPI(title="Culldrop Server", docs_url=None, redoc_url=None, lifespan=lifespan)


def request_lang(request: Request) -> str:
    return i18n.pick(request.headers.get("accept-language"), request.query_params.get("lang"))


def site_name(lang: str) -> str:
    return SITE_NAME or i18n.SITE_NAMES[lang]


@app.exception_handler(StarletteHTTPException)
async def localized_error(request: Request, exc: StarletteHTTPException):
    """訊息在這裡統一翻譯。手機要判斷錯誤種類看 code，不要比對文字。"""
    body = {"detail": i18n.tr(exc.detail, request_lang(request))}
    code = i18n.code_for(exc.detail)
    if code:
        body["code"] = code
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return {"ok": True, "free_gb": round(shutil.disk_usage(DATA_DIR).free / 1e9, 1)}


# ---------- 上傳（可續傳；與 Culldrop app 的 UploadService 對應）----------

def _h(headers, name: str) -> str:
    """同時接受 X-SnapPipe-* 與 X-Culldrop-*。
    HTTP header 以 latin-1 解碼，手機送的 UTF-8（中文標籤、署名）會變亂碼：能還原成 UTF-8 就還原。"""
    v = (headers.get(f"x-snappipe-{name}") or headers.get(f"x-culldrop-{name}") or "").strip()
    try:
        return v.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    return name[:200]


def file_kind(name: str) -> Optional[str]:
    ext = Path(name).suffix.lower()
    if ext in JPG_EXT:
        return "jpeg"
    if ext in RAW_EXT:
        return "raw"
    return None


def _offset_headers(row) -> dict:
    return {"Upload-Offset": str(row["received"]), "Upload-Length": str(row["total"]),
            "Upload-Complete": "1" if row["finished_at"] else "0"}


def cleanup_stale_uploads(con):
    cutoff = time.time() - UPLOAD_STALE_SEC
    for r in con.execute("SELECT id FROM uploads WHERE finished_at IS NULL AND updated_at < ?", (cutoff,)).fetchall():
        _unlink(upload_tmp(r["id"]))
        con.execute("DELETE FROM uploads WHERE id=?", (r["id"],))


@app.api_route("/api/uploads/{upload_id}", methods=["HEAD"])
def upload_status(upload_id: str, request: Request, authorization: Optional[str] = Header(None)):
    device = require_device(request, authorization)
    with db() as con:
        row = con.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    if not row or row["device"] != device:
        return Response(status_code=404)
    return Response(status_code=200, headers=_offset_headers(row))


def _parse_range(h: str, total_hint: Optional[int]):
    m = re.match(r"^bytes (\d+)-(\d+)/(\d+)$", h or "")
    if not m:
        raise HTTPException(400, "Content-Range 格式要是 bytes S-E/T")
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if end < start or end >= total or total <= 0:
        raise HTTPException(400, "Content-Range 範圍不合理")
    return start, end, total


@app.put("/api/uploads/{upload_id}")
async def upload_chunk(upload_id: str, request: Request, authorization: Optional[str] = Header(None),
                       content_range: Optional[str] = Header(None)):
    device = require_device(request, authorization)
    if not UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(400, "upload id 格式錯誤")
    start, end, total = _parse_range(content_range, None)
    h = request.headers
    with db() as con:
        row = con.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row and row["device"] != device:
            raise HTTPException(403, "這個上傳屬於別的裝置")
        if not row:
            album = _h(h, "album")
            filename = safe_filename(_h(h, "filename"))
            if not SLUG_RE.match(album):
                raise HTTPException(400, "第一段要帶 X-SnapPipe-Album（相簿名稱：文字、數字、. _ -）")
            if not filename:
                raise HTTPException(400, "第一段要帶 X-SnapPipe-Filename")
            kind = file_kind(filename)
            if not kind:
                raise HTTPException(415, f"只收 JPG 與 RAW（{', '.join(sorted(RAW_EXT))}）")
            if start != 0:
                raise HTTPException(409, "沒看過這個上傳，請從 0 開始", headers={"Upload-Offset": "0"})
            if shutil.disk_usage(DATA_DIR).free - total < MIN_FREE:
                raise HTTPException(507, "伺服器磁碟空間不足", headers={"Retry-After": "300"})
            cleanup_stale_uploads(con)
            inflight = con.execute("SELECT COUNT(*) FROM uploads WHERE device=? AND finished_at IS NULL", (device,)).fetchone()[0]
            if inflight >= MAX_INFLIGHT:
                raise HTTPException(429, f"同時進行中的上傳太多（{inflight}），先把手上的傳完", headers={"Retry-After": "10"})
            stars = _h(h, "stars")
            stars_v = int(stars) if stars.isdigit() and 1 <= int(stars) <= 5 else None
            tags = _h(h, "tags")
            try:
                tags_v = json.dumps([str(t)[:80] for t in json.loads(tags)][:40], ensure_ascii=False) if tags else None
            except ValueError:
                tags_v = None
            now = time.time()
            con.execute("""INSERT INTO uploads (id, device, album, filename, kind, total, received, camera, taken_at, stars, tags,
                                                creator, source, started_at, updated_at)
                           VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)""",
                        (upload_id, device, album, filename, kind, total, _h(h, "camera")[:60] or None,
                         _h(h, "taken-at")[:19] or None, stars_v, tags_v, _h(h, "creator")[:80] or None,
                         (_h(h, "source") or "culldrop")[:20], now, now))
            row = con.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row["finished_at"]:
            a = con.execute("SELECT share_key FROM albums WHERE slug=?", (row["album"],)).fetchone()
            return {"complete": True, "photo_id": row["photo_id"], "duplicate": True, "album": row["album"],
                    "share_url": share_url(a["share_key"]) if a else None, "backup": backup_state(con, row["photo_id"])}
    if total != row["total"]:
        raise HTTPException(400, "檔案大小和一開始的不一樣")
    tmp = upload_tmp(upload_id)
    have = tmp.stat().st_size if tmp.exists() else 0
    if have != row["received"]:
        with db() as con:
            con.execute("UPDATE uploads SET received=? WHERE id=?", (have, upload_id))
    if start != have:
        return JSONResponse({"detail": i18n.tr("請從 Upload-Offset 繼續", request_lang(request)), "offset": have}, status_code=409,
                            headers={"Upload-Offset": str(have)})
    want = end - start + 1
    got = 0
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "ab") as f:
        async for chunk in request.stream():
            if got + len(chunk) > want:
                chunk = chunk[: want - got]
            f.write(chunk)
            got += len(chunk)
            if got >= want:
                break
        f.flush()
        os.fsync(f.fileno())
    if got != want:
        with open(tmp, "r+b") as f:
            f.truncate(have)
        raise HTTPException(400, f"這一段只收到 {got}/{want} bytes", headers={"Upload-Offset": str(have)})
    received = have + got
    with db() as con:
        con.execute("UPDATE uploads SET received=?, updated_at=? WHERE id=?", (received, time.time(), upload_id))
        row = con.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    if received < total:
        return JSONResponse({"complete": False, "offset": received}, headers=_offset_headers(row))
    return finalize_upload(row, tmp)


def _reject(row, tmp: Path, status: int, msg: str):
    _unlink(tmp)
    with db() as con:
        con.execute("DELETE FROM uploads WHERE id=?", (row["id"],))
    raise HTTPException(status, msg)


def _ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def stem_of(name: str) -> str:
    return Path(name).stem.lower()


def finalize_upload(row, tmp: Path) -> dict:
    if row["kind"] == "raw":
        try:
            check_raw(tmp, row["filename"])
        except Exception as e:
            _reject(row, tmp, 415, f"不是可接受的 RAW（{e}）")
        return finalize_raw(row, tmp)
    try:
        with Image.open(tmp) as im:
            if im.format not in ("JPEG", "MPO"):
                raise ValueError(im.format)
            im.verify()
    except Exception as e:
        _reject(row, tmp, 415, f"只接受 JPEG（{e}）")

    sha = sha256_path(tmp)
    size = tmp.stat().st_size
    info = exif_info(tmp)
    taken_at = info.get("taken_at") or row["taken_at"]
    camera = row["camera"] or info.get("camera")
    dest = original_path(sha)
    store(tmp, dest)
    try:
        w, h = make_derivatives(dest, sha)
    except Exception as e:
        raise HTTPException(400, f"無法讀取圖片：{e}")

    now = time.time()
    with db() as con:
        album = ensure_album(con, row["album"], shoot_date=(taken_at or "")[:10] or None)
        existing = con.execute("SELECT id FROM photos WHERE album_id=? AND photo_id=?", (album["id"], sha)).fetchone()
        if existing:
            duplicate = True
            photo_pk = existing["id"]
            # 重傳時手機可能帶了新的星等 / 標籤：更新
            if row["stars"] is not None or row["tags"]:
                con.execute("UPDATE photos SET stars=COALESCE(?, stars), tags=COALESCE(?, tags), updated_at=? WHERE id=?",
                            (row["stars"], row["tags"], now, photo_pk))
                nas_touch(con, sha)
        else:
            duplicate = False
            cur = con.execute(
                """INSERT INTO photos (album_id, photo_id, filename, stars, tags, taken_at, width, height, bytes,
                                       camera, creator, source, device, uploaded_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (album["id"], sha, row["filename"], row["stars"], row["tags"] or "[]", taken_at, w, h, size,
                 camera, row["creator"], row["source"] or "culldrop", row["device"], now, now))
            photo_pk = cur.lastrowid
            attach_pending_raw(con, photo_pk, row["device"], row["album"], row["filename"], taken_at)
            con.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album["id"]))
        con.execute("UPDATE uploads SET received=total, finished_at=?, photo_id=?, taken_at=?, camera=? WHERE id=?",
                    (now, sha, taken_at, camera, row["id"]))
        nas_enqueue(con, sha, row["album"], "jpeg", row["filename"])
        share_key = album["share_key"]
        backup = backup_state(con, sha)
    return {"complete": True, "photo_id": sha, "duplicate": duplicate, "album": row["album"], "share_url": share_url(share_key), "backup": backup}


def find_jpg_for_raw(con, device: str, album: str, filename: str, taken_at: Optional[str]):
    """同裝置、同相簿：同檔名 stem → 拍攝時間 ±2 秒內最近的 → 最近 2 分鐘內還沒 RAW 的。"""
    stem = stem_of(filename)
    a = con.execute("SELECT id FROM albums WHERE slug=?", (album,)).fetchone()
    if not a:
        return None
    cands = con.execute("SELECT * FROM photos WHERE album_id=? AND device=? AND raw_sha IS NULL", (a["id"], device)).fetchall()
    for c in cands:
        if stem_of(c["filename"]) == stem:
            return c
    t = _ts(taken_at)
    if t is not None:
        best = None
        for c in cands:
            ct = _ts(c["taken_at"])
            if ct is not None and abs(ct - t) <= PAIR_TOLERANCE and (best is None or abs(ct - t) < best[0]):
                best = (abs(ct - t), c)
        if best:
            return best[1]
    return None


def attach_raw(con, photo_pk: int, sha: str, name: str, size: int):
    con.execute("UPDATE photos SET raw_sha=?, raw_name=?, raw_bytes=?, updated_at=? WHERE id=?",
                (sha, name, size, time.time(), photo_pk))
    nas_touch(con, sha)


def attach_pending_raw(con, photo_pk: int, device: str, album: str, jpg_name: str, taken_at: Optional[str]):
    stem = stem_of(jpg_name)
    rows = con.execute("SELECT * FROM raw_pending WHERE device=? AND album=?", (device, album)).fetchall()
    match = next((r for r in rows if r["stem"] == stem), None)
    if not match:
        t = _ts(taken_at)
        if t is not None:
            for r in rows:
                rt = _ts(r["taken_at"])
                if rt is not None and abs(rt - t) <= PAIR_TOLERANCE:
                    match = r
                    break
    if match:
        attach_raw(con, photo_pk, match["sha256"], match["filename"], match["bytes"])
        con.execute("DELETE FROM raw_pending WHERE id=?", (match["id"],))


def finalize_raw(row, tmp: Path) -> dict:
    sha = sha256_path(tmp)
    size = tmp.stat().st_size
    store(tmp, raw_path(sha, row["filename"]))
    now = time.time()
    with db() as con:
        album = ensure_album(con, row["album"])
        p = find_jpg_for_raw(con, row["device"], row["album"], row["filename"], row["taken_at"])
        if p:
            attach_raw(con, p["id"], sha, row["filename"], size)
            con.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, album["id"]))
        else:
            con.execute("INSERT INTO raw_pending (device, album, stem, sha256, filename, bytes, taken_at, camera, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (row["device"], row["album"], stem_of(row["filename"]), sha, row["filename"], size,
                         row["taken_at"], row["camera"], now))
        con.execute("UPDATE uploads SET received=total, finished_at=?, photo_id=? WHERE id=?", (now, sha, row["id"]))
        nas_enqueue(con, sha, row["album"], "raw", row["filename"])
        share_key = album["share_key"]
        backup = backup_state(con, sha)
    return {"complete": True, "photo_id": sha, "duplicate": False, "album": row["album"], "share_url": share_url(share_key), "backup": backup}


# ---------- 管理 API（裝置 token；手機是相簿的管理系統）----------

class AlbumIn(BaseModel):
    slug: str
    title: Optional[str] = None
    shoot_date: Optional[str] = None
    notes: Optional[str] = None


class AlbumPatch(BaseModel):
    title: Optional[str] = None
    shoot_date: Optional[str] = None
    notes: Optional[str] = None


class DeleteIn(BaseModel):
    photo_ids: Optional[list] = None
    max_stars: Optional[int] = None
    include_unrated: bool = False


class RatingIn(BaseModel):
    stars: Optional[int] = None
    tags: Optional[list] = None


class BackupStatusIn(BaseModel):
    shas: list


class PersonIn(BaseModel):
    name: str
    photo_ids: list


class FaceTagsIn(BaseModel):
    base_rev: int = 0
    people: dict   # {名字: [photo_id, …]}


class FaceSyncIn(BaseModel):
    source: str = ""
    epoch: str = ""
    scans: list = []     # [{"photo": sha, "faces": [{id, person, vector, box}]}]：新分群的照片，伺服器已經有結果就不收
    people: list = []    # [{id, name, created, deleted}]
    faces: list = []     # [{id, photo, person, vector, box, deleted}]：改已經分群過的照片


class CamerasIn(BaseModel):
    cameras: dict   # {key: {"name": "...", "creator": "..."}} 或 {key: "name"}


def disk_json(path: Path) -> Optional[dict]:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total_gb": round(u.total / 1e9, 1), "used_gb": round(u.used / 1e9, 1), "free_gb": round(u.free / 1e9, 1)}


@app.get("/api/manage/me")
def manage_me(request: Request, authorization: Optional[str] = Header(None)):
    device = require_device(request, authorization)
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        backup = nas_summary(con)
    return {"device": device, "api": SERVER_API, "albums": n, "free_gb": round(shutil.disk_usage(DATA_DIR).free / 1e9, 1), "public_url": PUBLIC_URL, "backup": backup,
            "storage": disk_json(DATA_DIR), "nas_storage": disk_json(NAS_DIR) if nas_ready() else None}


@app.post("/api/manage/backup-status")
def manage_backup_status(body: BackupStatusIn, request: Request, authorization: Optional[str] = Header(None)):
    """手機問哪些檔案 NAS 已經收到，收到的才能刪本機檔。"""
    require_device(request, authorization)
    shas = [s for s in body.shas if isinstance(s, str) and SHA_RE.match(s)][:500]
    if NAS_DIR is None:
        return {"enabled": False, "done": shas}
    with db() as con:
        return {"enabled": True, "done": [s for s in shas if backup_state(con, s) == "done"]}


@app.get("/api/manage/albums")
def manage_albums(request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    with db() as con:
        rows = con.execute("SELECT * FROM albums ORDER BY shoot_date DESC, created_at DESC").fetchall()
        return {"single_album": False, "albums": [album_json(con, a) for a in rows]}


def _create_album(body: AlbumIn, request: Request, authorization: Optional[str]):
    require_device(request, authorization)
    if not SLUG_RE.match(body.slug):
        raise HTTPException(400, "相簿名稱只能用文字、數字、. _ -")
    with db() as con:
        a = ensure_album(con, body.slug, body.title, body.shoot_date)
        if body.notes is not None:
            con.execute("UPDATE albums SET notes=? WHERE id=?", (body.notes, a["id"]))
            a = album_by_slug(con, body.slug)
        return album_json(con, a)


@app.post("/api/manage/albums")
def manage_create_album(body: AlbumIn, request: Request, authorization: Optional[str] = Header(None)):
    return _create_album(body, request, authorization)


@app.post("/api/albums")
def create_album_legacy(body: AlbumIn, request: Request, authorization: Optional[str] = Header(None)):
    """舊路徑（app 的 fallback）：同一件事。"""
    return _create_album(body, request, authorization)


@app.get("/api/manage/albums/{slug}")
def manage_album(slug: str, request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        rows = con.execute(f"SELECT * FROM photos WHERE album_id=? {ORDER}", (a["id"],)).fetchall()
        return {**album_json(con, a), "photos": [photo_json(r) for r in rows], "cameras": camera_names(con)}


@app.patch("/api/manage/albums/{slug}")
def manage_patch_album(slug: str, body: AlbumPatch, request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        con.execute("UPDATE albums SET title=COALESCE(?, title), shoot_date=COALESCE(?, shoot_date), notes=COALESCE(?, notes), updated_at=? WHERE id=?",
                    (body.title, body.shoot_date, body.notes, time.time(), a["id"]))
        return album_json(con, album_by_slug(con, slug))


# ---------- 人臉分群完整同步 ----------
# 手機各自掃臉會分出不一樣的群，所以一張照片只分群一次：第一支送上來的算數，其他手機拉下來直接用。
# 紀錄只增不刪（刪除是留一筆 deleted 的記號），手機記著拉到哪個 seq，下次只拿之後變的。

FACE_PAGE = 500            # 一次最多回幾筆（一張臉連向量約 1.5KB）
FACE_PUSH_MAX = 5000       # 一次最多收幾筆
FACE_VECTOR_MAX = 4096     # base64 後的長度上限（512 維 16-bit 是 1368）


def face_meta(con, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM face_sync_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def face_set_meta(con, key: str, value: str):
    con.execute("INSERT INTO face_sync_meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


class FaceWriter:
    """一次寫入裡發 seq、寫紀錄。要在 BEGIN IMMEDIATE 的交易裡用。"""

    def __init__(self, con, source: str):
        self.con, self.source = con, source
        self.seq = int(face_meta(con, "seq", "0"))

    def put(self, kind: str, rid: str, data: dict, photo_id: Optional[str] = None, deleted: bool = False):
        self.seq += 1
        self.con.execute(
            """INSERT INTO face_sync (kind, id, photo_id, data, deleted, seq, source) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(kind, id) DO UPDATE SET photo_id=excluded.photo_id, data=excluded.data, deleted=excluded.deleted,
                                                   seq=excluded.seq, source=excluded.source""",
            (kind, rid, photo_id, "{}" if deleted else json.dumps(data, ensure_ascii=False, separators=(",", ":")), int(deleted), self.seq, self.source))

    def close(self):
        face_set_meta(self.con, "seq", str(self.seq))


def face_forget_photo(con, photo_id: str):
    """照片從伺服器上完全刪掉了：那張的臉和「分群過」的記號都留下刪除記號，向量清掉。"""
    rows = con.execute("SELECT kind, id FROM face_sync WHERE deleted=0 AND ((kind='face' AND photo_id=?) OR (kind='photo' AND id=?))",
                       (photo_id, photo_id)).fetchall()
    if not rows:
        return
    w = FaceWriter(con, "")
    for r in rows:
        w.put(r["kind"], r["id"], {}, photo_id if r["kind"] == "face" else None, deleted=True)
    w.close()


def _face_fields(f) -> Optional[dict]:
    """檢查一張臉：person 要是 UUID；vector 是 base64 字串（手動標的沒有）；box 是四個數字。"""
    if not isinstance(f, dict) or not isinstance(f.get("person"), str) or not UUID_RE.match(f["person"]):
        return None
    out = {"person": f["person"].upper()}
    v = f.get("vector")
    if isinstance(v, str) and v and len(v) <= FACE_VECTOR_MAX and re.fullmatch(r"[A-Za-z0-9+/=]+", v):
        out["vector"] = v
    b = f.get("box")
    if isinstance(b, list) and len(b) == 4 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b):
        out["box"] = [round(float(x), 5) for x in b]
    return out


def face_record_json(r) -> dict:
    out = {"kind": r["kind"], "id": r["id"], "seq": r["seq"]}
    if r["deleted"]:
        out["deleted"] = True
        return out
    if r["kind"] == "face":
        out["photo"] = r["photo_id"]
    out.update(json.loads(r["data"]))
    return out


@app.get("/api/manage/faces")
def manage_faces(request: Request, since: int = 0, limit: int = FACE_PAGE, source: str = "", authorization: Optional[str] = Header(None)):
    """since 之後變過的紀錄，照 seq 排。more = 還有下一頁，用回傳的 seq 接著拉。source 給了就不回自己寫的。"""
    require_device(request, authorization)
    limit = max(1, min(limit, FACE_PAGE))
    since = max(0, since)
    with db() as con:
        # 先讀最大的 seq 再查紀錄：中間有人寫入的話，那幾筆下次會再拿到一次，不會漏
        top = int(face_meta(con, "seq", "0"))
        rows = con.execute("SELECT * FROM face_sync WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        # 沒有下一頁時游標直接到最大的 seq：被 source 濾掉的那些也算拉過了
        cursor = rows[-1]["seq"] if more else max(top, since)
        return {"epoch": face_meta(con, "epoch"), "seq": cursor, "more": more,
                "records": [face_record_json(r) for r in rows if not (source and r["source"] == source)]}


@app.post("/api/manage/faces")
def manage_post_faces(body: FaceSyncIn, request: Request, authorization: Optional[str] = Header(None)):
    """寫入。epoch 要和伺服器的一樣（伺服器還沒有資料時是空字串）：不一樣表示伺服器被清過，回 409，手機重來。"""
    require_device(request, authorization)
    nested = sum(len(s["faces"]) for s in body.scans if isinstance(s, dict) and isinstance(s.get("faces"), list))
    if len(body.scans) + len(body.people) + len(body.faces) + nested > FACE_PUSH_MAX:
        raise HTTPException(413, "人物資料太多")
    source = body.source[:64]
    skipped, unknown = [], []
    with db() as con:
        con.execute("BEGIN IMMEDIATE")   # 發 seq 和「這張照片有沒有人送過」都要在同一個交易裡
        epoch = face_meta(con, "epoch")
        if body.epoch != epoch:
            return JSONResponse({"epoch": epoch, "seq": int(face_meta(con, "seq", "0"))}, status_code=409)
        w = FaceWriter(con, source)
        for p in body.people:
            if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not UUID_RE.match(p["id"]):
                continue
            data = {}
            if isinstance(p.get("name"), str) and p["name"].strip():
                data["name"] = p["name"].strip()[:80]
            if isinstance(p.get("created"), (int, float)) and not isinstance(p.get("created"), bool):
                data["created"] = float(p["created"])
            w.put("person", p["id"].upper(), data, deleted=bool(p.get("deleted")))
        for scan in body.scans:
            sha = scan.get("photo") if isinstance(scan, dict) else None
            if not isinstance(sha, str) or not SHA_RE.match(sha):
                continue
            if not con.execute("SELECT 1 FROM photos WHERE photo_id=? LIMIT 1", (sha,)).fetchone():
                unknown.append(sha)      # 照片還沒傳上來：之後再送
                continue
            if con.execute("SELECT 1 FROM face_sync WHERE kind='photo' AND id=? AND deleted=0", (sha,)).fetchone():
                skipped.append(sha)      # 別支手機先分群過了：用那一份
                continue
            w.put("photo", sha, {})
            for f in scan.get("faces", []) if isinstance(scan.get("faces"), list) else []:
                fields = _face_fields(f)
                if fields and isinstance(f.get("id"), str) and UUID_RE.match(f["id"]):
                    w.put("face", f["id"].upper(), fields, sha)
        for f in body.faces:
            if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not UUID_RE.match(f["id"]):
                continue
            fid = f["id"].upper()
            if f.get("deleted"):
                if con.execute("SELECT 1 FROM face_sync WHERE kind='face' AND id=? AND deleted=0", (fid,)).fetchone():
                    old = con.execute("SELECT photo_id FROM face_sync WHERE kind='face' AND id=?", (fid,)).fetchone()
                    w.put("face", fid, {}, old["photo_id"], deleted=True)
                continue
            sha, fields = f.get("photo"), _face_fields(f)
            if not fields or not isinstance(sha, str) or not SHA_RE.match(sha):
                continue
            if not con.execute("SELECT 1 FROM photos WHERE photo_id=? LIMIT 1", (sha,)).fetchone():
                continue
            w.put("face", fid, fields, sha)
        if w.seq > 0 and not epoch:
            epoch = secrets.token_hex(8)
            face_set_meta(con, "epoch", epoch)
        w.close()
        return {"epoch": epoch, "seq": w.seq, "skipped": skipped, "unknown": unknown}


@app.delete("/api/manage/faces")
def manage_delete_faces(request: Request, authorization: Optional[str] = Header(None)):
    """清掉所有人臉分群資料（含向量），epoch 作廢。其他還開著同步的手機下次會把自己的再送上來。"""
    require_device(request, authorization)
    with db() as con:
        n = con.execute("DELETE FROM face_sync").rowcount
        con.execute("DELETE FROM face_sync_meta")
    return {"removed": n}


def delete_photo_rows(con, album_id: int, rows) -> int:
    deleted = 0
    for r in rows:
        pk, photo_id, raw_sha, raw_name = r["id"], r["photo_id"], r["raw_sha"], r["raw_name"]
        con.execute("DELETE FROM photos WHERE id=?", (pk,))
        deleted += 1
        # 還沒備份就刪的不用備了；已經在 NAS 的留著
        slug = con.execute("SELECT slug FROM albums WHERE id=?", (album_id,)).fetchone()["slug"]
        con.execute("DELETE FROM nas_files WHERE album=? AND sha IN (?, ?) AND done_at IS NULL", (slug, photo_id, raw_sha))
        if not con.execute("SELECT 1 FROM photos WHERE photo_id=? LIMIT 1", (photo_id,)).fetchone():
            for kind in ("s", "t", "p"):
                _unlink(derived_path(photo_id, kind))
            _unlink(og_path(photo_id))
            _unlink(original_path(photo_id))
            con.execute("DELETE FROM uploads WHERE photo_id=?", (photo_id,))   # 之後可以重傳同一張
            face_forget_photo(con, photo_id)
        if raw_sha and raw_name and not con.execute("SELECT 1 FROM photos WHERE raw_sha=? LIMIT 1", (raw_sha,)).fetchone():
            _unlink(raw_path(raw_sha, raw_name))
            con.execute("DELETE FROM uploads WHERE photo_id=?", (raw_sha,))
    if deleted:
        con.execute("UPDATE albums SET updated_at=? WHERE id=?", (time.time(), album_id))
    return deleted


@app.post("/api/manage/albums/{slug}/delete-photos")
def manage_delete_photos(slug: str, body: DeleteIn, request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    if body.photo_ids is None and body.max_stars is None:
        raise HTTPException(400, "要給 photo_ids 或 max_stars")
    with db() as con:
        a = album_by_slug(con, slug)
        rows = []
        if body.photo_ids:
            ids = [i for i in body.photo_ids if isinstance(i, str) and SHA_RE.match(i)]
            if ids:
                q = ",".join("?" * len(ids))
                rows += con.execute(f"SELECT * FROM photos WHERE album_id=? AND photo_id IN ({q})", (a["id"], *ids)).fetchall()
        if body.max_stars is not None:
            if not 1 <= body.max_stars <= 5:
                raise HTTPException(400, "max_stars 要在 1～5")
            cond = "stars <= ?" + (" OR stars IS NULL" if body.include_unrated else "")
            rows += con.execute(f"SELECT * FROM photos WHERE album_id=? AND ({cond})", (a["id"], body.max_stars)).fetchall()
        seen, uniq = set(), []
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        n = delete_photo_rows(con, a["id"], uniq)
        remaining = con.execute("SELECT COUNT(*) FROM photos WHERE album_id=?", (a["id"],)).fetchone()[0]
    return {"deleted": n, "remaining": remaining, "deleted_ids": [r["photo_id"] for r in uniq]}


@app.put("/api/manage/albums/{slug}/people/{person_id}")
def manage_put_person(slug: str, person_id: str, body: PersonIn, request: Request, authorization: Optional[str] = Header(None)):
    """建立或更新一個人的分享連結：照片清單整份取代，分享碼不變。"""
    require_device(request, authorization)
    if not PERSON_RE.match(person_id):
        raise HTTPException(400, "人物 id 格式錯誤")
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(400, "要有名字")
    ids = {i for i in body.photo_ids if isinstance(i, str) and SHA_RE.match(i)}
    now = time.time()
    with db() as con:
        a = album_by_slug(con, slug)
        con.execute("""INSERT INTO album_people (album_id, person_id, name, share_key, updated_at) VALUES (?,?,?,?,?)
                       ON CONFLICT(album_id, person_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at""",
                    (a["id"], person_id, name, new_key(), now))
        p = con.execute("SELECT * FROM album_people WHERE album_id=? AND person_id=?", (a["id"], person_id)).fetchone()
        con.execute("DELETE FROM album_people_photos WHERE person_pk=?", (p["id"],))
        con.executemany("INSERT INTO album_people_photos (person_pk, photo_id) VALUES (?,?)", [(p["id"], i) for i in ids])
    return {"person_id": person_id, "name": name, "share_key": p["share_key"], "share_url": share_url(p["share_key"]), "photos": len(ids)}


@app.delete("/api/manage/albums/{slug}/people/{person_id}")
def manage_delete_person(slug: str, person_id: str, request: Request, authorization: Optional[str] = Header(None)):
    """停止分享：連結立刻失效。"""
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        n = con.execute("DELETE FROM album_people WHERE album_id=? AND person_id=?", (a["id"], person_id)).rowcount
    return {"removed": n > 0}


# ---------- 人物同步 ----------

FACE_TAG_NAMES = 2000      # 一本相簿最多幾個名字
FACE_TAG_PAIRS = 200000    # 最多幾筆「照片 × 名字」


def face_tags_json(con, album_id: int) -> dict:
    row = con.execute("SELECT rev, epoch FROM album_face_revs WHERE album_id=?", (album_id,)).fetchone()
    people: dict = {}
    for r in con.execute("SELECT name, photo_id FROM album_face_tags WHERE album_id=? ORDER BY name, photo_id", (album_id,)):
        people.setdefault(r["name"], []).append(r["photo_id"])
    return {"rev": row["rev"] if row else 0, "epoch": row["epoch"] if row else "", "people": people}


@app.get("/api/manage/albums/{slug}/face-tags")
def manage_face_tags(slug: str, request: Request, authorization: Optional[str] = Header(None)):
    """這本相簿取過名字的人物：名字 → 照片。rev 0 = 還沒有人寫過（或被清掉了）；epoch 清空後會換。"""
    require_device(request, authorization)
    with db() as con:
        return face_tags_json(con, album_by_slug(con, slug)["id"])


@app.put("/api/manage/albums/{slug}/face-tags")
def manage_put_face_tags(slug: str, body: FaceTagsIn, request: Request, authorization: Optional[str] = Header(None)):
    """整份取代。base_rev 要等於現在的 rev：不等表示別支手機先寫了，回 409 和現在的內容，手機合併後再送。"""
    device = require_device(request, authorization)
    pairs = set()
    for raw_name, ids in body.people.items():
        name = raw_name.strip()[:80] if isinstance(raw_name, str) else ""
        if not name or not isinstance(ids, list):
            continue
        pairs |= {(name, i) for i in ids if isinstance(i, str) and SHA_RE.match(i)}
    if len({n for n, _ in pairs}) > FACE_TAG_NAMES or len(pairs) > FACE_TAG_PAIRS:
        raise HTTPException(413, "人物資料太多")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")   # 讀版本和寫入要在同一個交易裡，兩支手機同時寫才不會互蓋
        a = album_by_slug(con, slug)
        current = face_tags_json(con, a["id"])
        if body.base_rev != current["rev"]:
            return JSONResponse(current, status_code=409)
        rev = current["rev"] + 1
        con.execute("DELETE FROM album_face_tags WHERE album_id=?", (a["id"],))
        con.executemany("INSERT INTO album_face_tags (album_id, name, photo_id) VALUES (?,?,?)", [(a["id"], n, i) for n, i in pairs])
        con.execute("""INSERT INTO album_face_revs (album_id, rev, epoch, device, updated_at) VALUES (?,?,?,?,?)
                       ON CONFLICT(album_id) DO UPDATE SET rev=excluded.rev, device=excluded.device, updated_at=excluded.updated_at""",
                    (a["id"], rev, secrets.token_hex(8), device, time.time()))
        return face_tags_json(con, a["id"])


@app.delete("/api/manage/albums/{slug}/face-tags")
def manage_delete_face_tags(slug: str, request: Request, authorization: Optional[str] = Header(None)):
    """清掉這本相簿的人物資料，版本歸零。其他還開著同步的手機下次會把自己的再送上來。"""
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        n = con.execute("DELETE FROM album_face_tags WHERE album_id=?", (a["id"],)).rowcount
        con.execute("DELETE FROM album_face_revs WHERE album_id=?", (a["id"],))
    return {"removed": n}


@app.delete("/api/manage/albums/{slug}")
def manage_delete_album(slug: str, request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        rows = con.execute("SELECT * FROM photos WHERE album_id=?", (a["id"],)).fetchall()
        n = delete_photo_rows(con, a["id"], rows)
        for r in con.execute("SELECT * FROM raw_pending WHERE album=?", (slug,)).fetchall():
            _unlink(raw_path(r["sha256"], r["filename"]))
        con.execute("DELETE FROM raw_pending WHERE album=?", (slug,))
        con.execute("DELETE FROM uploads WHERE album=? AND finished_at IS NULL", (slug,))
        con.execute("DELETE FROM albums WHERE id=?", (a["id"],))
    return {"deleted_photos": n, "album_removed": True}


def _apply_rating(con, row, body: RatingIn) -> dict:
    if body.stars is not None and not 1 <= body.stars <= 5:
        raise HTTPException(400, "stars 要在 1～5")
    tags = json.dumps([str(t)[:80] for t in body.tags][:40], ensure_ascii=False) if body.tags is not None else None
    now = time.time()
    con.execute("UPDATE photos SET stars=COALESCE(?, stars), tags=COALESCE(?, tags), updated_at=? WHERE id=?",
                (body.stars, tags, now, row["id"]))
    con.execute("UPDATE albums SET updated_at=? WHERE id=?", (now, row["album_id"]))
    nas_touch(con, row["photo_id"], row["raw_sha"])
    return photo_json(con.execute("SELECT * FROM photos WHERE id=?", (row["id"],)).fetchone())


@app.post("/api/manage/albums/{slug}/photos/{photo_id}/rating")
def manage_rating(slug: str, photo_id: str, body: RatingIn, request: Request, authorization: Optional[str] = Header(None)):
    require_device(request, authorization)
    with db() as con:
        a = album_by_slug(con, slug)
        row = con.execute("SELECT * FROM photos WHERE album_id=? AND photo_id=?", (a["id"], photo_id)).fetchone()
        if not row:
            raise HTTPException(404, "沒有這張照片")
        return _apply_rating(con, row, body)


@app.post("/api/photos/{photo_id}/rating")
def photo_rating(photo_id: str, body: RatingIn, request: Request, authorization: Optional[str] = Header(None)):
    """不指定相簿：同一個 photo_id 在多本相簿時全部一起改。"""
    require_device(request, authorization)
    with db() as con:
        rows = con.execute("SELECT * FROM photos WHERE photo_id=?", (photo_id,)).fetchall()
        if not rows:
            raise HTTPException(404, "沒有這張照片")
        out = [_apply_rating(con, r, body) for r in rows]
        return out[0] if len(out) == 1 else {"updated": len(out), "photos": out}


@app.put("/api/manage/cameras")
def manage_cameras(body: CamerasIn, request: Request, authorization: Optional[str] = Header(None)):
    """手機端把相機顯示名稱 / 署名同步上來，返圖頁的相機 chip 才有人看得懂的名字。"""
    require_device(request, authorization)
    now = time.time()
    with db() as con:
        for key, v in body.cameras.items():
            if not key or len(key) > 120:
                continue
            name = v.get("name") if isinstance(v, dict) else str(v)
            creator = v.get("creator") if isinstance(v, dict) else None
            if not name:
                continue
            con.execute("INSERT INTO cameras (key, name, creator, updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET name=excluded.name, creator=excluded.creator, updated_at=excluded.updated_at",
                        (key, str(name)[:60], (str(creator)[:60] if creator else None), now))
        return {"cameras": camera_names(con)}


# ---------- 公開：分享 API、媒體、下載（不用 token）----------

IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/api/share/{key}")
def share_album(key: str):
    with db() as con:
        a, visible, title = share_view(con, key)
        rows = con.execute(f"SELECT * FROM photos WHERE album_id=? {ORDER}", (a["id"],)).fetchall()
        cams = camera_names(con)
    photos = [photo_json(r) for r in rows if visible is None or r["photo_id"] in visible]
    for p in photos:
        p.pop("creator", None)
    used = {p["camera"] for p in photos}
    return {"title": title, "shoot_date": a["shoot_date"], "photos": photos, "people": [],
            "cameras": {k: v["name"] for k, v in cams.items() if k in used}, "live": False, "pending": 0, "updated_at": a["updated_at"]}


@app.api_route("/media/{key}/{kind}/{photo_id}", methods=["GET", "HEAD"])
def album_media(key: str, kind: str, photo_id: str):
    if kind not in ("s", "t", "p", "o", "raw") or not SHA_RE.match(photo_id):
        raise HTTPException(404)
    with db() as con:
        a, visible, _ = share_view(con, key)
        r = con.execute("SELECT * FROM photos WHERE album_id=? AND photo_id=?", (a["id"], photo_id)).fetchone()
    if not r or (visible is not None and photo_id not in visible):
        raise HTTPException(404)
    if kind == "o":
        return FileResponse(original_path(photo_id), media_type="image/jpeg", headers=IMMUTABLE, filename=r["filename"])
    if kind == "raw":
        if not r["raw_sha"]:
            raise HTTPException(404, "這張沒有 RAW")
        return FileResponse(raw_path(r["raw_sha"], r["raw_name"]), media_type="application/octet-stream", headers=IMMUTABLE, filename=r["raw_name"])
    p = derived_path(photo_id, kind)
    if not p.exists():
        try:
            make_derivatives(original_path(photo_id), photo_id)
        except Exception:
            raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg" if kind == "p" else "image/webp", headers=IMMUTABLE)


class _ZipBuffer:
    """zipfile 寫到這裡，寫多少就吐多少，不把整包放記憶體。"""
    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def write(self, b):
        self.buf += b
        self.pos += len(b)
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def take(self):
        out = bytes(self.buf)
        self.buf = bytearray()
        return out


def _zip_stream(items):
    buf = _ZipBuffer()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for name, path in items:
            if not path.exists():
                continue
            with zf.open(zipfile.ZipInfo.from_file(path, arcname=name), "w", force_zip64=True) as dst, open(path, "rb") as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    dst.write(chunk)
                    yield buf.take()
    yield buf.take()


@app.get("/api/share/{key}/zip")
def share_zip(key: str, min_stars: int = 0, fmt: str = "jpg"):
    with db() as con:
        a, visible, title = share_view(con, key)
        rows = con.execute(f"SELECT * FROM photos WHERE album_id=? {ORDER}", (a["id"],)).fetchall()
    rows = [r for r in rows if (r["stars"] or 0) >= min_stars and (visible is None or r["photo_id"] in visible)]
    if fmt not in ("jpg", "raw", "all"):
        raise HTTPException(400, "fmt 要是 jpg、raw 或 all")
    items, seen = [], set()
    for r in rows:
        if fmt != "raw":
            name = r["filename"]
            items.append((name if name not in seen else f"{r['photo_id'][:8]}_{name}", original_path(r["photo_id"])))
            seen.add(name)
        if fmt != "jpg" and r["raw_sha"]:
            name = r["raw_name"]
            items.append((name if name not in seen else f"{r['raw_sha'][:8]}_{name}", raw_path(r["raw_sha"], r["raw_name"])))
            seen.add(name)
    if not items:
        raise HTTPException(404, "沒有可下載的檔案")
    zipname = title + (f"_{min_stars}star" if min_stars else "") + {"raw": "_raw", "all": "_jpg_raw"}.get(fmt, "") + ".zip"
    return StreamingResponse(_zip_stream(items), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(zipname)}"})


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ---------- 返圖頁 ----------

def _page(title: str, description: str, og: str, lang: str) -> str:
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(500, f"返圖頁檔案不存在：{index}")
    page = index.read_text(encoding="utf-8")
    name = site_name(lang)
    full = f"{title} · {name}"
    return (page.replace("{{OG}}", og).replace("{{TITLE}}", html.escape(full))
                .replace("{{DESCRIPTION}}", html.escape(description))
                .replace("{{LANG}}", lang)
                .replace("{{SITE_NAME}}", html.escape(name)))


@app.api_route("/a/{key}", methods=["GET", "HEAD"])
def album_page(key: str, request: Request):
    lang = request_lang(request)
    with db() as con:
        a, visible, title = share_view(con, key)
        n = con.execute("SELECT COUNT(*) FROM photos WHERE album_id=?", (a["id"],)).fetchone()[0] if visible is None else len(visible)
        cover = cover_photo(con, a["id"], visible)
    description = f"{a['shoot_date'] or ''} · {i18n.photo_count(n, lang)}"
    return HTMLResponse(_page(title, description, og_tags(key, title, description, cover, lang), lang),
                        headers={"Cache-Control": "no-cache", "Vary": "Accept-Language"})


@app.api_route("/og/{key}/{cover}.jpg", methods=["GET", "HEAD"])
def og_image(key: str, cover: str):
    """網址帶封面 id：換封面就是新網址，通訊軟體才會重抓；刪掉、停止分享後舊網址都 404。"""
    with db() as con:
        a, visible, _ = share_view(con, key)
        current = cover_photo(con, a["id"], visible)
    if not current or current[:20] != cover:
        raise HTTPException(404)
    try:
        path = make_og(current)
    except Exception:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers=IMMUTABLE)


@app.api_route("/", methods=["GET", "HEAD"])
def home(request: Request):
    lang = request_lang(request)
    return HTMLResponse(f"<!doctype html><html lang={lang}><meta charset=utf-8><title>{html.escape(site_name(lang))}</title>"
                        "<body style='background:#141416;color:#9A9AA3;font-family:system-ui;padding:40px'>"
                        f"{i18n.HOME_TEXT[lang]}</body>", headers={"Vary": "Accept-Language"})


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
def favicon():
    return FileResponse(WEB_DIR / "static/icons/favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


if (WEB_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
