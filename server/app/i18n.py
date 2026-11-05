"""伺服器的多語系：回應語言與錯誤訊息。程式裡的原文是繁中，這裡只放英文與日文。

語言順序：?lang= → Accept-Language → CULLDROP_LANG（沒帶語言的請求，例如通訊軟體抓預覽）。
"""
import os
import re
from typing import Optional

LANGS = ("zh-Hant", "en", "ja")


def normalize(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip().lower()
    if v.startswith("zh"):
        return "zh-Hant"
    if v.startswith("ja"):
        return "ja"
    if v.startswith("en"):
        return "en"
    return None


DEFAULT_LANG = normalize(os.environ.get("CULLDROP_LANG")) or "zh-Hant"


def pick(accept_language: Optional[str], query: Optional[str] = None) -> str:
    """Accept-Language 有帶但都不支援時用英文，和 app 一樣。"""
    chosen = normalize(query)
    if chosen:
        return chosen
    if not accept_language:
        return DEFAULT_LANG
    ranked = []
    for i, part in enumerate(accept_language.split(",")):
        tag, _, params = part.strip().partition(";")
        m = re.search(r"q=([0-9.]+)", params)
        try:
            q = float(m.group(1)) if m else 1.0
        except ValueError:
            q = 0.0
        ranked.append((-q, i, tag))
    for _, _, tag in sorted(ranked):
        chosen = normalize(tag)
        if chosen:
            return chosen
    return "en"


# 繁中原文（{} 是變動的部分）、英文、日文
_MESSAGES = [
    ("無法辨識來源 IP", "Can't identify the source IP", "接続元の IP を識別できません"),
    ("來源 {} 不在允許的網段", "Source {0} is not in an allowed network", "接続元 {0} は許可されたネットワークにありません"),
    ("伺服器未設定 CULLDROP_DEVICE_TOKENS", "CULLDROP_DEVICE_TOKENS is not set on the server", "サーバーに CULLDROP_DEVICE_TOKENS が設定されていません"),
    ("token 錯太多次，暫時封鎖", "Too many wrong tokens. Temporarily blocked.", "トークンの誤りが多すぎるため、一時的にブロックしました"),
    ("裝置 token 錯誤", "Wrong device token", "デバイストークンが正しくありません"),
    ("沒有這本相簿", "Album not found", "アルバムが見つかりません"),
    ("沒有這張照片", "Photo not found", "写真が見つかりません"),
    ("這張沒有 RAW", "This photo has no RAW", "この写真に RAW はありません"),
    ("沒有可下載的檔案", "No files to download", "ダウンロードできるファイルがありません"),
    ("Content-Range 格式要是 bytes S-E/T", "Content-Range must be bytes S-E/T", "Content-Range は bytes S-E/T の形式にしてください"),
    ("Content-Range 範圍不合理", "Content-Range is out of range", "Content-Range の範囲が正しくありません"),
    ("upload id 格式錯誤", "Invalid upload id", "upload id の形式が正しくありません"),
    ("這個上傳屬於別的裝置", "This upload belongs to another device", "このアップロードは別のデバイスのものです"),
    ("第一段要帶 X-SnapPipe-Album（相簿名稱：文字、數字、. _ -）", "The first chunk needs X-SnapPipe-Album (album name: letters, numbers, . _ -)", "最初のチャンクには X-SnapPipe-Album(アルバム名: 文字、数字、. _ -)が必要です"),
    ("第一段要帶 X-SnapPipe-Filename", "The first chunk needs X-SnapPipe-Filename", "最初のチャンクには X-SnapPipe-Filename が必要です"),
    ("只收 JPG 與 RAW（{}）", "Only JPG and RAW are accepted ({0})", "JPG と RAW のみ受け付けます({0})"),
    ("沒看過這個上傳，請從 0 開始", "Unknown upload. Start from 0.", "不明なアップロードです。0 からやり直してください"),
    ("伺服器磁碟空間不足", "The server is out of disk space", "サーバーのディスク容量が足りません"),
    ("同時進行中的上傳太多（{}），先把手上的傳完", "Too many uploads in progress ({0}). Finish the current ones first.", "同時アップロードが多すぎます({0})。先に進行中のものを完了してください"),
    ("檔案大小和一開始的不一樣", "The file size changed since the upload started", "ファイルサイズが最初と異なります"),
    ("請從 Upload-Offset 繼續", "Continue from Upload-Offset", "Upload-Offset から続けてください"),
    ("這一段只收到 {}/{} bytes", "Only {0}/{1} bytes of this chunk arrived", "このチャンクは {0}/{1} バイトしか受信できませんでした"),
    ("不是可接受的 RAW（{}）", "Not an accepted RAW file ({0})", "受け付けられる RAW ではありません({0})"),
    ("{} 檔頭不符", "{0} header doesn't match", "{0} のヘッダーが一致しません"),
    ("只接受 JPEG（{}）", "Only JPEG is accepted ({0})", "JPEG のみ受け付けます({0})"),
    ("無法讀取圖片：{}", "Can't read the image: {0}", "画像を読み込めません: {0}"),
    ("相簿名稱只能用文字、數字、. _ -", "Album names can only use letters, numbers, . _ -", "アルバム名に使えるのは文字、数字、. _ - のみです"),
    ("要給 photo_ids 或 max_stars", "Provide photo_ids or max_stars", "photo_ids または max_stars を指定してください"),
    ("max_stars 要在 1～5", "max_stars must be 1 to 5", "max_stars は 1〜5 で指定してください"),
    ("人物 id 格式錯誤", "Invalid person id", "人物 id の形式が正しくありません"),
    ("要有名字", "A name is required", "名前が必要です"),
    ("stars 要在 1～5", "stars must be 1 to 5", "stars は 1〜5 で指定してください"),
    ("fmt 要是 jpg、raw 或 all", "fmt must be jpg, raw, or all", "fmt は jpg、raw、all のいずれかにしてください"),
    ("返圖頁檔案不存在：{}", "Gallery page file is missing: {0}", "ギャラリーページのファイルがありません: {0}"),
]
_COMPILED = [(re.compile("^" + re.escape(zh).replace(r"\{\}", "(.+)") + "$", re.S), en, ja) for zh, en, ja in _MESSAGES]


def tr(message: str, lang: str) -> str:
    """翻譯錯誤訊息；不在表上的（例如框架的 Not Found）原樣回傳。"""
    if lang == "zh-Hant" or not isinstance(message, str):
        return message
    for pattern, en, ja in _COMPILED:
        m = pattern.match(message)
        if m:
            parts = [tr(g, lang) for g in m.groups()]
            return (ja if lang == "ja" else en).format(*parts)
    return message


def code_for(message) -> Optional[str]:
    """訊息會翻譯，程式要判斷就看 code。"""
    return "album_not_found" if message == "沒有這本相簿" else None


SITE_NAMES = {"zh-Hant": "返圖", "en": "Photos", "ja": "写真"}


def photo_count(n: int, lang: str) -> str:
    if lang == "ja":
        return f"{n} 枚"
    if lang == "en":
        return "1 photo" if n == 1 else f"{n} photos"
    return f"{n} 張"


HOME_TEXT = {
    "zh-Hant": "Culldrop Server · 請用相簿分享連結開啟",
    "en": "Culldrop Server · Open this with an album share link",
    "ja": "Culldrop Server · アルバムの共有リンクから開いてください",
}
