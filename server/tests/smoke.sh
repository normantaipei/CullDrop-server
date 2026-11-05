#!/bin/zsh
# 端到端 smoke test：起一個暫時的 Culldrop server，用 curl 跑完整情境。
#   server/tests/smoke.sh [python 直譯器]   （需要 fastapi / uvicorn / Pillow；可用 server/.venv/bin/python）
set -u
PY=${1:-python3}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d); PORT=8766; B=http://127.0.0.1:$PORT; T="Authorization: Bearer devtok"
fail=0; pass=0
chk() { if [ "$1" = "$2" ]; then pass=$((pass+1)); else echo "✘ $3 (got $1, want $2)"; fail=$((fail+1)); fi }
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }

# 測試用 JPG（帶 EXIF）與假 RAW（TIFF 檔頭）
$PY - "$TMP" <<'PYEOF'
import sys, struct
from PIL import Image
from pathlib import Path
tmp = Path(sys.argv[1])
for i, color in enumerate([(200, 80, 80), (80, 200, 80), (80, 80, 200)], 1):
    im = Image.new("RGB", (600, 400) if i != 2 else (400, 600), color)
    ex = Image.Exif(); ex[0x010f] = "SONY"; ex[0x0110] = "ILCE-7M4"
    ex.get_ifd(0x8769)[36867] = f"2026:10:31 14:3{i}:00"   # DateTimeOriginal 放在 Exif IFD，和相機一樣
    im.save(tmp / f"DSC0000{i}.JPG", "JPEG", exif=ex)
# 假 RAW：TIFF little-endian 檔頭 + 垃圾
(tmp / "DSC00001.ARW").write_bytes(b"II*\x00" + b"\x08\x00\x00\x00" + b"\x00" * 5000)
(tmp / "capt0009.ARW").write_bytes(b"II*\x00" + b"\x08\x00\x00\x00" + b"\x01" * 5000)   # 先到的 RAW，等 JPG 來配
PYEOF

(cd "$ROOT/server" && CULLDROP_DATA=$TMP/data CULLDROP_NAS_DIR=$TMP/nas CULLDROP_NAS_POLL=0.2 CULLDROP_TRUSTED_PROXIES=127.0.0.1 CULLDROP_DEVICE_TOKENS="iphone:devtok,ipad:tok2" CULLDROP_PUBLIC_URL=https://photos.test CULLDROP_WEB=$ROOT/web \
  $PY -m uvicorn app.main:app --host 127.0.0.1 --port $PORT > $TMP/server.log 2>&1 &)
for i in {1..40}; do curl -s $B/healthz >/dev/null && break; sleep 0.25; done
trap 'pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $PORT"; rm -rf "$TMP"' EXIT

put() { # put <file> <upload_id> <album> [extra headers...]
  local f=$1 uid=$2 album=$3; shift 3; local SZ=$(stat -f %z "$f")
  curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes 0-$((SZ-1))/$SZ" \
    -H "X-SnapPipe-Album: $album" -H "X-SnapPipe-Filename: $(basename $f)" "$@" --data-binary @"$f" $B/api/uploads/$uid
}

echo "— 身分"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/api/manage/albums)" 401 "no token → 401"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["device"]')" iphone "me → device name"

echo "— 建相簿（多本）"
R=$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"slug":"cos_2026","title":"cos 外拍","shoot_date":"2026-10-31"}' $B/api/manage/albums)
KEY=$(echo $R | j 'd["share_key"]'); chk "$(echo $R | j 'd["title"]')" "cos 外拍" "create album 1"
R2=$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"slug":"wedding_2026"}' $B/api/manage/albums)
KEY2=$(echo $R2 | j 'd["share_key"]'); chk "$(echo $R2 | j 'd["title"]')" wedding_2026 "create album 2 (title=slug)"
chk "$(curl -s -H "$T" $B/api/manage/albums | j 'len(d["albums"])')" 2 "two albums on one server"
chk "$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"slug":"bad slug"}' -o /dev/null -w '%{http_code}' $B/api/manage/albums)" 400 "bad slug → 400"

echo "— 上傳 JPG（帶星等 / 標籤 / 相機 / 署名）+ RAW 配對"
IDS=()
for i in 1 2 3; do
  R=$(put $TMP/DSC0000$i.JPG "aaaa000$i$(printf '%036d' 0)" cos_2026 -H "X-SnapPipe-Stars: $((6-i))" -H 'X-SnapPipe-Tags: ["portrait","人像","AI_VISION"]' -H "X-SnapPipe-Camera: SERIAL5530" -H "X-SnapPipe-Creator: Norman" -H "X-SnapPipe-Source: culldrop")
  IDS+=($(echo $R | j 'd["photo_id"]'))
done
chk "$(echo $R | j 'd["complete"]')" True "jpg complete"
chk "$(echo $R | j 'd["share_url"]')" "https://photos.test/a/$KEY" "share_url uses public url"
R=$(put $TMP/DSC00001.ARW "bbbb0001$(printf '%036d' 0)" cos_2026)
chk "$(echo $R | j 'd["complete"]')" True "raw complete"
D=$(curl -s -H "$T" $B/api/manage/albums/cos_2026)
chk "$(echo $D | j 'len(d["photos"])')" 3 "3 photos"
chk "$(echo $D | j '[p["stars"] for p in d["photos"]]')" "[5, 4, 3]" "stars from header, ordered"
chk "$(echo $D | j 'd["photos"][0]["tags"]')" "['portrait', '人像', 'AI_VISION']" "tags from header"
chk "$(echo $D | j 'd["photos"][0]["raw_name"]')" DSC00001.ARW "raw paired by stem"
chk "$(echo $D | j 'd["photos"][0]["camera"]')" SERIAL5530 "camera key kept"
chk "$(echo $D | j 'd["photos"][0]["creator"]')" Norman "creator kept"
chk "$(echo $D | j 'd["photos"][1]["h"]')" 600 "portrait size"

echo "— RAW 先到、JPG 後到（PTP 流水號，用拍攝時間配）"
R=$(put $TMP/capt0009.ARW "cccc0009$(printf '%036d' 0)" wedding_2026 -H "X-SnapPipe-Taken-At: 2026-10-31T14:32:01")
cp $TMP/DSC00002.JPG $TMP/capt0010.JPG
R=$(put $TMP/capt0010.JPG "cccc0010$(printf '%036d' 0)" wedding_2026)   # EXIF 14:32:00，差 1 秒
chk "$(curl -s -H "$T" $B/api/manage/albums/wedding_2026 | j 'd["photos"][0]["raw_name"]')" capt0009.ARW "raw_pending paired by time"

echo "— 分段上傳、續傳、409"
f=$TMP/DSC00003.JPG; SZ=$(stat -f %z $f); UID3=dddd0003$(printf '%036d' 0); HALF=$((SZ/2))
head -c $HALF $f > $TMP/part1; tail -c $((SZ-HALF)) $f > $TMP/part2
R=$(curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes 0-$((HALF-1))/$SZ" -H "X-SnapPipe-Album: wedding_2026" -H "X-SnapPipe-Filename: DSC00003.JPG" --data-binary @$TMP/part1 $B/api/uploads/$UID3)
chk "$(echo $R | j 'd["offset"]')" $HALF "partial offset"
chk "$(curl -s -I -H "$T" $B/api/uploads/$UID3 | grep -i upload-offset | tr -d '\r' | awk '{print $2}')" $HALF "HEAD offset"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "$T" -H "Content-Range: bytes 0-$((HALF-1))/$SZ" --data-binary @$TMP/part1 $B/api/uploads/$UID3)" 409 "wrong offset → 409"
R=$(curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes $HALF-$((SZ-1))/$SZ" --data-binary @$TMP/part2 $B/api/uploads/$UID3)
chk "$(echo $R | j 'd["complete"]')" True "resume completes"
chk "$(echo $R | j 'd["duplicate"]')" False "new in this album"
chk "$(curl -s -X PUT -H "$T" -H "Content-Range: bytes 0-$((SZ-1))/$SZ" --data-binary @$f $B/api/uploads/$UID3 | j 'd["duplicate"]')" True "re-send finished id → duplicate"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer tok2" -H "Content-Range: bytes 0-1/$SZ" --data-binary 'xx' $B/api/uploads/$UID3)" 403 "other device → 403"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "$T" -H "Content-Range: bytes 0-3/4" -H "X-SnapPipe-Album: cos_2026" -H "X-SnapPipe-Filename: x.png" --data-binary 'abcd' $B/api/uploads/eeee$(printf '%040d' 0))" 415 "png → 415"

echo "— 公開：分享 API、媒體、返圖頁、zip（都不用 token）"
S=$(curl -s $B/api/share/$KEY)
chk "$(echo $S | j 'len(d["photos"])')" 3 "share photos"
chk "$(echo $S | j '"creator" in d["photos"][0]')" False "share hides creator"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$KEY/s/${IDS[1]})" 200 "small thumb"
chk "$(curl -s -o /dev/null -w '%{content_type}' $B/media/$KEY/p/${IDS[1]})" image/jpeg "preview jpeg"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$KEY/raw/${IDS[1]})" 200 "raw download"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$KEY/raw/${IDS[2]})" 404 "no raw → 404"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$KEY2/s/${IDS[1]})" 404 "photo not in other album"
chk "$(curl -s $B/a/$KEY | grep -c '<title>cos 外拍 · 返圖')" 1 "album page title"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/static/app.js)" 200 "static js"
curl -s "$B/api/share/$KEY/zip?min_stars=4" -o $TMP/a.zip
chk "$(python3 -c "import zipfile,sys; print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" $TMP/a.zip)" 2 "zip ★4+ has 2"
chk "$(python3 -c "import zipfile,sys; print(zipfile.ZipFile(sys.argv[1]).testzip())" $TMP/a.zip)" None "zip valid"

curl -s "$B/api/share/$KEY/zip?fmt=raw" -o $TMP/r.zip
chk "$(python3 -c "import zipfile,sys; print(zipfile.ZipFile(sys.argv[1]).namelist())" $TMP/r.zip)" "['DSC00001.ARW']" "zip raw only"
curl -s "$B/api/share/$KEY/zip?fmt=all" -o $TMP/b.zip
chk "$(python3 -c "import zipfile,sys; n=zipfile.ZipFile(sys.argv[1]).namelist(); print(len(n), sum(x.endswith('.ARW') for x in n))" $TMP/b.zip)" "4 1" "zip jpg + raw"
chk "$(curl -s -o /dev/null -w '%{http_code}' "$B/api/share/$KEY/zip?fmt=nope")" 400 "bad zip format"

echo "— 相機名稱同步 → 返圖頁 chip"
curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d '{"cameras":{"SERIAL5530":{"name":"A7IV","creator":"Norman"}}}' $B/api/manage/cameras >/dev/null
chk "$(curl -s $B/api/share/$KEY | j 'd["cameras"]["SERIAL5530"]')" A7IV "camera name on share"

echo "— 多語系"
chk "$(curl -s -H 'Accept-Language: en' -H "$T" $B/api/manage/albums/no-such | j 'd["detail"]')" "Album not found" "error in English"
chk "$(curl -s -H 'Accept-Language: ja' -H "$T" $B/api/manage/albums/no-such | j 'd["code"]')" album_not_found "code does not depend on language"
chk "$(curl -s -H 'Accept-Language: ja' $B/a/$KEY | grep -c '<html lang="ja">')" 1 "page language from browser"
chk "$(curl -s "$B/a/$KEY?lang=en" | grep -c '<html lang="en">')" 1 "page language from ?lang="

echo "— 改星、改相簿"
chk "$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"stars":2}' $B/api/photos/${IDS[1]}/rating | j 'd["stars"]')" 2 "rating"
chk "$(curl -s -X PATCH -H "$T" -H 'Content-Type: application/json' -d '{"title":"cos 外拍 v2"}' $B/api/manage/albums/cos_2026 | j 'd["title"]')" "cos 外拍 v2" "patch title"

echo "— 個人連結：只看得到這個人的照片"
J2=${IDS[2]}; J1X=${IDS[1]}
R=$(curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d "{\"name\":\"Saki\",\"photo_ids\":[\"$J2\",\"bad\"]}" $B/api/manage/albums/cos_2026/people/person-1)
PK=$(echo $R | j 'd["share_key"]')
chk "$(echo $R | j 'd["photos"]')" 1 "person link created, bad id dropped"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/json' -d '{"name":"x","photo_ids":[]}' $B/api/manage/albums/cos_2026/people/p2)" 401 "person link needs token"
chk "$(curl -s $B/api/share/$PK | j 'len(d["photos"])')" 1 "only this person's photos"
chk "$(curl -s $B/api/share/$PK | j 'd["photos"][0]["id"]')" "$J2" "the right photo"
chk "$(curl -s $B/api/share/$PK | j 'd["title"]')" "cos 外拍 v2 · Saki" "title has the name"
chk "$(curl -s $B/api/share/$PK | grep -c $KEY)" 0 "album share key not leaked"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/o/$J2)" 200 "own photo opens"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/o/$J1X)" 404 "other photo blocked"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/s/$J1X)" 404 "other thumbnail blocked"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/raw/$J1X)" 404 "other raw blocked"
curl -s "$B/api/share/$PK/zip" -o $TMP/p.zip
chk "$(python3 -c "import zipfile,sys; print(len(zipfile.ZipFile(sys.argv[1]).namelist()))" $TMP/p.zip)" 1 "zip has only this person"
chk "$(curl -s $B/a/$PK | grep -c '<title>cos 外拍 v2 · Saki · 返圖')" 1 "page title"
chk "$(curl -s $B/api/share/$KEY | j 'len(d["photos"])')" 3 "album link unaffected"
echo "— 通訊軟體預覽圖"
ogurl() { curl -s $B/a/$1 | sed -n 's/.*og:image" content="\([^"]*\)".*/\1/p' | sed "s#https://photos.test#$B#"; }
OG=$(ogurl $KEY); OGP=$(ogurl $PK)
chk "$(curl -s $B/a/$KEY | grep -c 'photos.test/og/')" 1 "album page has absolute og:image"
chk "$(curl -s $B/a/$KEY | grep -c 'summary_large_image')" 1 "large card"
chk "$(curl -s $B/a/$KEY | grep -c 'name="robots" content="noindex"')" 1 "not indexed"
chk "$(curl -s -o $TMP/og.jpg -w '%{http_code} %{content_type}' $OG)" "200 image/jpeg" "og image served without token"
chk "$($PY -c "from PIL import Image; import sys; print(Image.open(sys.argv[1]).size)" $TMP/og.jpg)" "(1200, 630)" "og size"
chk "$(curl -s -o /dev/null -w '%{http_code}' -I $OG)" 200 "og head works for crawlers"
chk "$OGP" "$(echo $OG | sed "s#/og/$KEY/#/og/$PK/#")" "person cover is their own photo"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/og/$PK/$(echo $J1X | cut -c1-20).jpg)" 404 "person link cannot fetch someone else as cover"
R=$(curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d "{\"name\":\"Low\",\"photo_ids\":[\"${IDS[3]}\"]}" $B/api/manage/albums/cos_2026/people/low)
LK=$(echo $R | j 'd["share_key"]')
chk "$(curl -s $B/a/$LK | grep -c 'og:image')" 0 "no ★4 photo, no preview image"
chk "$(curl -s $B/a/$LK | grep -c 'og:url')" 1 "text preview still there"
curl -s -X DELETE -H "$T" $B/api/manage/albums/cos_2026/people/low >/dev/null

R=$(curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d "{\"name\":\"Saki 2\",\"photo_ids\":[\"$J1X\"]}" $B/api/manage/albums/cos_2026/people/person-1)
chk "$(echo $R | j 'd["share_key"]')" "$PK" "update keeps the link"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/o/$J2)" 404 "removed photo blocked at once"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/o/$J1X)" 200 "added photo opens"
curl -s -X DELETE -H "$T" $B/api/manage/albums/cos_2026/people/person-1 >/dev/null
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/api/share/$PK)" 404 "stopped link is gone"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$PK/o/$J1X)" 404 "stopped link media gone"
R=$(curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d '{"name":"W","photo_ids":[]}' $B/api/manage/albums/wedding_2026/people/w1)
WK=$(echo $R | j 'd["share_key"]')

echo "— NAS 備份（沒有標記檔 = 沒掛上，不能算備份）"
mkdir -p $TMP/nas
J1=${IDS[1]}
bstat() { curl -s -H "$T" -H 'Content-Type: application/json' -d "{\"shas\":[\"$J1\",\"nope\"]}" $B/api/manage/backup-status | j 'len(d["done"])'; }
me() { curl -s -H "$T" $B/api/manage/me | j "d['backup']['$1']"; }
sleep 0.6
chk "$(ls $TMP/nas | wc -l | tr -d ' ')" 0 "nothing written while not mounted"
chk "$(bstat)" 0 "not done while not mounted"
chk "$(me available)" False "me: nas not available"
touch $TMP/nas/.culldrop-nas
for i in {1..60}; do [ "$(me pending)" = 0 ] && break; sleep 0.25; done
chk "$(me pending)" 0 "queue drained"
chk "$(bstat)" 1 "done after mount"
chk "$(ls $TMP/nas/cos_2026/DSC00001.JPG $TMP/nas/cos_2026/DSC00001.ARW 2>/dev/null | wc -l | tr -d ' ')" 2 "jpg + raw in album folder, original names"
chk "$(ls $TMP/nas/*/capt0009.ARW 2>/dev/null | wc -l | tr -d ' ')" 1 "raw that came first copied"
chk "$(find $TMP/nas -name '*.part' | wc -l | tr -d ' ')" 0 "no partial files"
if command -v exiftool >/dev/null; then
  xr() { exiftool -s3 -XMP:Rating $TMP/nas/cos_2026/$1; }
  chk "$(xr DSC00001.JPG)" 2 "rating in nas jpg"
  chk "$(xr DSC00001.xmp)" 2 "rating in raw sidecar"
  chk "$(exiftool -s3 -XMP:Subject $TMP/nas/cos_2026/DSC00001.xmp)" "portrait, 人像, AI_VISION" "tags in raw sidecar"
  chk "$(exiftool -s3 -IPTC:Keywords $TMP/nas/cos_2026/DSC00001.JPG)" "portrait, 人像, AI_VISION" "utf-8 iptc keywords"
  curl -s -H "$T" -H 'Content-Type: application/json' -d '{"stars":4}' $B/api/photos/$J1/rating >/dev/null
  for i in {1..40}; do [ "$(xr DSC00001.JPG)$(xr DSC00001.xmp)" = 44 ] && break; sleep 0.25; done
  chk "$(xr DSC00001.JPG)$(xr DSC00001.xmp)" 44 "re-rate reaches nas"
  curl -s -H "$T" -H 'Content-Type: application/json' -d '{"stars":2}' $B/api/photos/$J1/rating >/dev/null
fi
NASCOUNT=$(ls $TMP/nas/cos_2026 | wc -l | tr -d ' ')

echo "— 刪除：依星等、多選、整本"
R=$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"max_stars":2}' $B/api/manage/albums/cos_2026/delete-photos)
chk "$(echo $R | j 'd["deleted"]')" 1 "delete ★2 → 1"; chk "$(echo $R | j 'd["remaining"]')" 2 "remaining 2"
P1=${IDS[1]}
chk "$(ls $TMP/data/originals/${P1:0:2}/$P1.jpg 2>/dev/null | wc -l | tr -d ' ')" 0 "original removed"
chk "$(ls $TMP/data/derived/${P1:0:2}/ 2>/dev/null | grep -c $P1)" 0 "derived removed"
chk "$(ls $TMP/data/originals/*/*.arw 2>/dev/null | wc -l | tr -d ' ')" 1 "paired raw removed, other raw kept"
chk "$(curl -s -o /dev/null -w '%{http_code}' -I -H "$T" $B/api/uploads/aaaa0001$(printf '%036d' 0))" 404 "upload record cleared"
R=$(curl -s -H "$T" -H 'Content-Type: application/json' -d "{\"photo_ids\":[\"${IDS[2]}\",\"nope\"]}" $B/api/manage/albums/cos_2026/delete-photos)
chk "$(echo $R | j 'd["deleted"]')" 1 "delete by id"
chk "$(curl -s -o /dev/null -w '%{http_code}' $OG)" 404 "deleted cover: preview url gone"
R=$(curl -s -X DELETE -H "$T" $B/api/manage/albums/wedding_2026); chk "$(echo $R | j 'd["album_removed"]')" True "album removed"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/api/share/$KEY2)" 404 "removed album share → 404"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/api/share/$WK)" 404 "person links go with the album"
chk "$(find $TMP/data/derived -name '*_og.jpg' | wc -l | tr -d ' ')" 0 "preview file removed with the last copy"
chk "$(curl -s -H "$T" $B/api/manage/albums | j 'len(d["albums"])')" 1 "one album left"
chk "$(ls $TMP/nas/cos_2026 | wc -l | tr -d ' ')" $NASCOUNT "deleting photos keeps nas copies"
chk "$(me failed)" 0 "no backup failures"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["storage"]["total_gb"] > 0')" True "me: server disk"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["nas_storage"]["free_gb"] > 0')" True "me: nas disk"
chk "$(grep -ci 'traceback' $TMP/server.log)" 0 "no server tracebacks"

echo "— 封鎖：只鎖連錯的人，正確的 token 永遠放行"
BAD="Authorization: Bearer wrong"
code() { curl -s -o /dev/null -w '%{http_code}' "$@" $B/api/manage/me; }
for i in {1..10}; do code -H "$BAD" -H 'X-Forwarded-For: 203.0.113.9' >/dev/null; done
chk "$(code -H "$BAD" -H 'X-Forwarded-For: 203.0.113.9')" 429 "attacker banned"
chk "$(code -H "$T" -H 'X-Forwarded-For: 198.51.100.7')" 200 "phone from elsewhere unaffected"
chk "$(code -H "$BAD" -H 'X-Forwarded-For: 198.51.100.8')" 401 "others not banned"
chk "$(code -H "$BAD" -H 'X-Forwarded-For: 1.2.3.4, 203.0.113.9')" 429 "fake left entry does not escape the ban"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "$T" -H 'X-Forwarded-For: 203.0.113.9' -H 'Content-Type: application/json' -d '{"shas":[]}' $B/api/manage/backup-status)" 200 "correct token passes even from a banned ip"
chk "$(code -H "$BAD" -H 'X-Forwarded-For: 203.0.113.9')" 401 "correct token clears the ban"
(cd "$ROOT/server" && CULLDROP_DATA=$TMP/ip CULLDROP_TRUSTED_PROXIES=10.0.0.1,192.168.1.0/24 $PY - <<'PYEOF'
from starlette.requests import Request
from app.main import client_ip
def req(peer, xff=None):
    h = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": h, "client": (peer, 1)})
cases = [
    (req("8.8.8.8", "9.9.9.9"), "8.8.8.8", "untrusted peer cannot spoof"),
    (req("10.0.0.1", "9.9.9.9"), "9.9.9.9", "trusted proxy"),
    (req("192.168.1.10", "9.9.9.9"), "9.9.9.9", "trusted cidr"),
    (req("10.0.0.1", "7.7.7.7, 9.9.9.9"), "9.9.9.9", "rightmost wins"),
    (req("10.0.0.1"), "10.0.0.1", "no header"),
    (req("10.0.0.1", "garbage"), "10.0.0.1", "garbage header"),
    (req("10.0.0.1", " ::ffff:9.9.9.9 "), "9.9.9.9", "ipv4 mapped"),
]
for r, want, name in cases:
    got = client_ip(r)
    print("ok" if got == want else f"bad {name}: {got}")
PYEOF
) > $TMP/ip.txt 2>&1
chk "$(sort -u $TMP/ip.txt)" ok "client ip rules"

echo; echo "passed $pass, failed $fail"
[ $fail -eq 0 ]
