// 返圖頁的多語系。原文是繁中，語言由伺服器決定（?lang= → 瀏覽器語言 → CULLDROP_LANG），寫在 <html lang>。
const LANG = document.documentElement.lang || "zh-Hant";
const I18N = {
  en: {
    "下載 ▾": "Download ▾", "取消": "Cancel", "讀取中…": "Loading…", "關閉": "Close",
    "上一張": "Previous", "下一張": "Next", "下載 JPG": "Download JPG", "下載 RAW": "Download RAW",
    "還沒有照片": "No photos yet", "全部": "All", "下載中…": "Downloading…",
    "點一下存到照片": "Tap to save to Photos", "存到照片": "Save to Photos",
    "下載失敗，再試一次": "Download failed. Try again.", "全部 JPG": "All JPG", "全部 RAW": "All RAW",
    "ZIP 會存到「檔案」App": "The ZIP is saved to the Files app",
    "下載第 {i}/{n} 批 {k}/{m}…": "Downloading batch {i}/{n}: {k}/{m}…",
    "存第 {i}/{n} 批（{m} 張）": "Save batch {i}/{n} ({m})",
    "{n} 張 JPG": "JPG: {n}", "已處理 {n} 張": "Done: {n}", "找不到這本相簿": "Album not found",
  },
  ja: {
    "下載 ▾": "ダウンロード ▾", "取消": "キャンセル", "讀取中…": "読み込み中…", "關閉": "閉じる",
    "上一張": "前へ", "下一張": "次へ", "下載 JPG": "JPG をダウンロード", "下載 RAW": "RAW をダウンロード",
    "還沒有照片": "写真はまだありません", "全部": "すべて", "下載中…": "ダウンロード中…",
    "點一下存到照片": "タップして写真に保存", "存到照片": "写真に保存",
    "下載失敗，再試一次": "ダウンロードに失敗しました。もう一度お試しください。", "全部 JPG": "すべての JPG", "全部 RAW": "すべての RAW",
    "ZIP 會存到「檔案」App": "ZIP は「ファイル」App に保存されます",
    "下載第 {i}/{n} 批 {k}/{m}…": "バッチ {i}/{n} をダウンロード中 {k}/{m}…",
    "存第 {i}/{n} 批（{m} 張）": "バッチ {i}/{n} を保存({m} 枚)",
    "{n} 張 JPG": "JPG {n} 枚", "已處理 {n} 張": "{n} 枚を処理しました", "找不到這本相簿": "アルバムが見つかりません",
  },
};
function t(text, vars = {}) {
  const s = (I18N[LANG] || {})[text] ?? text;
  return s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
}
function photoCount(n) {
  if (LANG === "ja") return `${n} 枚`;
  if (LANG === "en") return n === 1 ? "1 photo" : `${n} photos`;
  return `${n} 張`;
}
function fileCount(n) {
  if (LANG === "ja") return `${n} 件`;
  if (LANG === "en") return n === 1 ? "1 file" : `${n} files`;
  return `${n} 個`;
}
for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
for (const el of document.querySelectorAll("[data-i18n-label]")) el.setAttribute("aria-label", t(el.dataset.i18nLabel));
