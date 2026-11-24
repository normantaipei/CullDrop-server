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

(cd "$ROOT/server" && CULLDROP_DATA=$TMP/data CULLDROP_NAS_DIR=$TMP/nas CULLDROP_NAS_POLL=0.2 CULLDROP_TRUSTED_PROXIES=127.0.0.1 CULLDROP_DEVICE_TOKENS="iphone:devtok,ipad:tok2" CULLDROP_PUBLIC_URL=https://photos.test CULLDROP_RETOUCH=1 CULLDROP_WEB=$ROOT/web \
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

echo "— 版本：app 靠它知道伺服器要不要更新"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["api"]')" 4 "api level reported"

echo "— 人物同步：名字 → 照片，帶版本寫入"
FT=$B/api/manage/albums/cos_2026/face-tags; T2="Authorization: Bearer tok2"
ftput() { curl -s -o $TMP/ft.json -w '%{http_code}' -X PUT -H "$1" -H 'Content-Type: application/json' -d "$2" $FT; }
chk "$(curl -s -o /dev/null -w '%{http_code}' $FT)" 401 "face tags need token"
chk "$(curl -s -H "$T" $FT | j 'd["rev"]')" 0 "nothing written yet"
chk "$(ftput "$T" "{\"base_rev\":0,\"people\":{\" Saki \":[\"$J2\",\"bad\"],\"\":[\"$J1X\"],\"Mio\":[\"$J1X\",\"$J2\"]}}")" 200 "first write"
chk "$(j 'd["rev"]' < $TMP/ft.json)" 1 "rev starts at 1"
chk "$(j 'sorted(d["people"])' < $TMP/ft.json)" "['Mio', 'Saki']" "names trimmed, empty name dropped"
chk "$(j 'len(d["people"]["Saki"])' < $TMP/ft.json)" 1 "bad photo id dropped"
chk "$(curl -s -H "$T2" $FT | j 'len(d["people"]["Mio"])')" 2 "another device reads the same"
chk "$(ftput "$T2" "{\"base_rev\":0,\"people\":{\"Other\":[\"$J2\"]}}")" 409 "stale write is refused"
chk "$(j 'd["rev"]' < $TMP/ft.json)" 1 "conflict returns the current version"
chk "$(j 'sorted(d["people"])' < $TMP/ft.json)" "['Mio', 'Saki']" "conflict returns the current people"
chk "$(curl -s -H "$T" $FT | j 'sorted(d["people"])')" "['Mio', 'Saki']" "refused write changed nothing"
chk "$(ftput "$T2" "{\"base_rev\":1,\"people\":{\"Saki\":[\"$J1X\"]}}")" 200 "write with the current version"
chk "$(curl -s -H "$T" $FT | j '(d["rev"], d["people"])')" "(2, {'Saki': ['$J1X']})" "whole document replaced"
EP=$(curl -s -H "$T" $FT | j 'd["epoch"]')
chk "${#EP}" 16 "epoch issued on first write"
chk "$(j 'd["epoch"]' < $TMP/ft.json)" "$EP" "epoch stays across writes"
chk "$(curl -s -H "$T" $B/api/manage/albums/wedding_2026/face-tags | j '(d["rev"], d["people"])')" "(0, {})" "other album untouched"
chk "$(curl -s -o /dev/null -w '%{http_code}' -H "$T" $B/api/manage/albums/nope/face-tags)" 404 "unknown album"
chk "$(curl -s $B/api/share/$KEY | grep -c Saki)" 0 "names are not on the public share API"
chk "$(curl -s -X DELETE -H "$T" $FT | j 'd["removed"]')" 1 "cleared"
chk "$(curl -s -H "$T" $FT | j '(d["rev"], d["epoch"], d["people"])')" "(0, '', {})" "version back to 0 after clearing"
ftput "$T" "{\"base_rev\":0,\"people\":{\"Saki\":[\"$J2\"]}}" >/dev/null
chk "$([ "$(j 'd["epoch"]' < $TMP/ft.json)" != "$EP" ] && echo new)" new "new epoch after clearing"
curl -s -X DELETE -H "$T" $FT >/dev/null

echo "— 人臉分群完整同步：一張照片只分群一次，其他手機拉下來用"
FS=$B/api/manage/faces
P1=11111111-1111-4111-8111-111111111111; P2=22222222-2222-4222-8222-222222222222
F1=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA1; F2=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA2; F3=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA3
fpost() { curl -s -o $TMP/fs.json -w '%{http_code}' -X POST -H "$1" -H 'Content-Type: application/json' -d "$2" $FS; }
chk "$(curl -s -o /dev/null -w '%{http_code}' $FS)" 401 "face sync needs token"
chk "$(curl -s -H "$T" $FS | j '(d["epoch"], d["seq"], d["more"], d["records"])')" "('', 0, False, [])" "empty at first"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["api"]')" 4 "api level 4 (reported)"
NOPE=$(printf 'f%.0s' {1..64})
chk "$(fpost "$T" "{\"source\":\"one\",\"epoch\":\"\",\"people\":[{\"id\":\"$P1\",\"name\":\" Saki \",\"created\":1},{\"id\":\"bad\"}],\"scans\":[{\"photo\":\"$J2\",\"faces\":[{\"id\":\"$F1\",\"person\":\"$P1\",\"vector\":\"AAEC\",\"box\":[0.1,0.2,0.3,0.4]},{\"id\":\"$F2\",\"person\":\"nope\"}]},{\"photo\":\"$NOPE\",\"faces\":[]}]}")" 200 "first scan accepted"
EPF=$(j 'd["epoch"]' < $TMP/fs.json)
chk "${#EPF}" 16 "epoch issued"
chk "$(j 'd["unknown"]' < $TMP/fs.json)" "['$NOPE']" "photo not on the server is held back"
chk "$(j 'd["skipped"]' < $TMP/fs.json)" "[]" "nothing skipped"
chk "$(curl -s -H "$T2" "$FS?source=two" | j 'sorted((r["kind"], r.get("name"), r.get("photo"), r.get("vector"), r.get("box")) for r in d["records"])' | tr -d ' ')" "[('face',None,'$J2','AAEC',[0.1,0.2,0.3,0.4]),('person','Saki',None,None,None),('photo',None,None,None,None)]" "another phone pulls people, faces and the scanned marker"
chk "$(curl -s -H "$T" "$FS?source=one" | j '(len(d["records"]), d["seq"])')" "(0, 3)" "own writes are not sent back, cursor still advances"
chk "$(fpost "$T2" "{\"source\":\"two\",\"epoch\":\"$EPF\",\"scans\":[{\"photo\":\"$J2\",\"faces\":[{\"id\":\"$F3\",\"person\":\"$P2\"}]}]}")" 200 "second scan of the same photo"
chk "$(j 'd["skipped"]' < $TMP/fs.json)" "['$J2']" "already clustered photo is not clustered again"
chk "$(curl -s -H "$T" $FS | j 'len([r for r in d["records"] if r["kind"]=="face"])')" 1 "the first result stands"
chk "$(fpost "$T2" "{\"source\":\"two\",\"epoch\":\"stale\",\"people\":[{\"id\":\"$P2\",\"name\":\"X\"}]}")" 409 "wrong epoch refused"
chk "$(j 'd["epoch"]' < $TMP/fs.json)" "$EPF" "conflict tells the current epoch"
chk "$(fpost "$T2" "{\"source\":\"two\",\"epoch\":\"$EPF\",\"people\":[{\"id\":\"$P2\",\"name\":\"Mio\"}],\"faces\":[{\"id\":\"$F1\",\"photo\":\"$J2\",\"person\":\"$P2\",\"vector\":\"AAEC\"},{\"id\":\"$F3\",\"photo\":\"$J2\",\"person\":\"$P2\"}]}")" 200 "edit: reassign a face, add a manual one"
SEQ=$(j 'd["seq"]' < $TMP/fs.json)
chk "$(curl -s -H "$T" "$FS?since=3&source=one" | j 'sorted((r["kind"], r.get("person") or r.get("name")) for r in d["records"])' | tr -d ' ')" "[('face','$P2'),('face','$P2'),('person','Mio')]" "only what changed since the cursor"
chk "$(curl -s -H "$T" "$FS?since=3&limit=1" | j '(len(d["records"]), d["more"])')" "(1, True)" "paged"
chk "$(fpost "$T" "{\"source\":\"one\",\"epoch\":\"$EPF\",\"faces\":[{\"id\":\"$F3\",\"deleted\":true}],\"people\":[{\"id\":\"$P1\",\"deleted\":true}]}")" 200 "delete a face and a person"
chk "$(curl -s -H "$T2" "$FS?since=$SEQ" | j 'sorted((r["kind"], r.get("deleted"), "vector" in r) for r in d["records"])' | tr -d ' ')" "[('face',True,False),('person',True,False)]" "deletions arrive as markers without data"
chk "$(curl -s $B/api/share/$KEY | grep -c -E 'AAEC|Mio')" 0 "face data is not on the public share API"
chk "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE $FS)" 401 "clearing needs token"

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

echo "— 刪照片後那張的人臉資料也清掉；清空後重新開始"
chk "$(curl -s -H "$T" "$B/api/manage/faces" | j 'len([r for r in d["records"] if r["kind"] != "person" and not r.get("deleted")])')" 0 "faces of deleted photos are gone"
chk "$(curl -s -H "$T" "$B/api/manage/faces" | j 'len([r for r in d["records"] if "vector" in r])')" 0 "no vector left behind"
chk "$(curl -s -X DELETE -H "$T" $B/api/manage/faces | j 'd["removed"] > 0')" True "cleared"
chk "$(curl -s -H "$T" $B/api/manage/faces | j '(d["epoch"], d["seq"])')" "('', 0)" "fresh epoch after clearing"

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

echo "— 中文相簿名、標籤：iOS 會把 header 從中文處切掉，用 -Encoded 那份"
enc() { python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=''))" "$1"; }
$PY - "$TMP" <<'PYEOF'
import sys
from PIL import Image
for i in (7, 8):
    Image.new("RGB", (300, 200), (i * 20, 90, 90)).save(f"{sys.argv[1]}/zh{i}.jpg", "JPEG")
PYEOF
SZ=$(stat -f %z $TMP/zh7.jpg)
R=$(curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes 0-$((SZ-1))/$SZ" \
  -H "X-SnapPipe-Album: 2026_11_15_" -H "X-Culldrop-Album-Encoded: $(enc 2026_11_15_殘星會)" \
  -H "X-SnapPipe-Filename: ZH7.JPG" -H 'X-SnapPipe-Tags: ["' -H "X-Culldrop-Tags-Encoded: $(enc '["人像","AI_VISION"]')" \
  -H "X-Culldrop-Creator-Encoded: $(enc 諾曼)" --data-binary @$TMP/zh7.jpg $B/api/uploads/cccc0007$(printf '%032d' 0))
chk "$(echo $R | j 'd["album"]')" "2026_11_15_殘星會" "chinese album name kept"
chk "$(curl -s -H "$T" $B/api/manage/albums/2026_11_15_殘星會 | j 'd["photos"][0]["tags"]')" "['人像', 'AI_VISION']" "chinese tags kept"
chk "$(curl -s -H "$T" $B/api/manage/albums/2026_11_15_殘星會 | j 'd["photos"][0]["creator"]')" "諾曼" "chinese creator kept"
chk "$(curl -s -H "$T" $B/api/manage/me | j 'd["api"]')" 4 "api level 4"
# 舊版 app 造成的分裂：同一場被拆成兩本，用工具併回來
SZ=$(stat -f %z $TMP/zh8.jpg)
curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes 0-$((SZ-1))/$SZ" \
  -H "X-SnapPipe-Album: 2026_11_15_" -H "X-SnapPipe-Filename: ZH8.JPG" --data-binary @$TMP/zh8.jpg $B/api/uploads/cccc0008$(printf '%032d' 0) >/dev/null
SPLIT_KEY=$(curl -s -H "$T" $B/api/manage/albums/2026_11_15_ | j 'd["share_key"]')
curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d '{"name":"Saki","photo_ids":[]}' $B/api/manage/albums/2026_11_15_/people/saki >/dev/null
MERGE() { (cd "$ROOT/server" && CULLDROP_DATA=$TMP/data $PY -m tools.merge_album 2026_11_15_ 2026_11_15_殘星會 "$@" 2>&1); }
chk "$(MERGE | head -1)" "「2026_11_15_」→「2026_11_15_殘星會」：搬 1 張，重複 0 張（丟掉來源那筆），「2026_11_15_」會刪掉" "merge preview"
chk "$(curl -s -o /dev/null -w '%{http_code}' -H "$T" $B/api/manage/albums/2026_11_15_)" 200 "preview changes nothing"
chk "$(MERGE --apply | tail -1)" "完成：「2026_11_15_殘星會」現在 2 張" "merge applied"
chk "$(curl -s -o /dev/null -w '%{http_code}' -H "$T" $B/api/manage/albums/2026_11_15_)" 404 "split album gone"
chk "$(curl -s -H "$T" $B/api/manage/albums/2026_11_15_殘星會 | j 'sorted(p["name"] for p in d["photos"])')" "['ZH7.JPG', 'ZH8.JPG']" "photos together"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/api/share/$SPLIT_KEY)" 404 "old split link gone"
chk "$(curl -s -X PUT -H "$T" -H 'Content-Type: application/json' -d '{"name":"Saki","photo_ids":[]}' $B/api/manage/albums/2026_11_15_殘星會/people/saki | j 'd["share_key"] is not None')" True "person link moved"
chk "$(curl -s -X PUT -H "$T" -H "Content-Type: application/octet-stream" -H "Content-Range: bytes 0-$((SZ-1))/$SZ" -H "X-SnapPipe-Album: 2026_11_15_" -H "X-SnapPipe-Filename: ZH8.JPG" --data-binary @$TMP/zh8.jpg $B/api/uploads/cccc0008$(printf '%032d' 0) | j 'd["duplicate"]')" True "re-send after merge is a duplicate"
chk "$(MERGE nope 2>&1 | tail -1)" "找不到相簿：2026_11_15_" "merge refuses missing album"

echo "— 修容（捏臉）：存紀錄、兩個人接力自動合併、改到同一張臉、輸出、還原"
$PY - "$TMP" "$ROOT/server" <<'PYEOF'
import sys
from pathlib import Path
from PIL import Image, ImageChops
sys.path.insert(0, sys.argv[2])
from app import retouch
im = Image.effect_noise((1200, 800), 60).convert("RGB")
ex = Image.Exif(); ex.get_ifd(0x8769)[36867] = "2026:11:20 10:00:00"
im.save(Path(sys.argv[1]) / "RT01.JPG", "JPEG", quality=90, exif=ex)
# 位移是 0 的格子要和原圖一模一樣；動過的只在元件的半徑內
box = (300, 200, 900, 600)
flat = im.transform((600, 400), Image.Transform.MESH, retouch.mesh(box, [["m", 0.5, 0.33, 0.1, 0.0, 0.0]], 1200, 800), Image.Resampling.BICUBIC)
assert ImageChops.difference(flat, im.crop(box)).getbbox() is None, "identity mesh changed pixels"
moved = retouch.warp(im.copy(), {"f": {"prims": [["m", 0.5, 400 / 1200, 0.1, 0.025, 0.0]]}})
x0, y0, x1, y1 = ImageChops.difference(moved, im).getbbox()      # 圓心 (600, 400)、半徑 120px，格子邊緣差 1～2px
assert 478 <= x0 <= 500 and 278 <= y0 <= 300 and 700 <= x1 <= 722 and 500 <= y1 <= 522, (x0, y0, x1, y1)
# 圖層（磨皮、調色…）：只動形狀裡面，沒用到的形狀不存，不合規格的擋掉
square = [0.4, 0.25, 0.6, 0.25, 0.6, 0.45, 0.4, 0.45]
raw = {"box": [0.35, 0.2, 0.65, 0.5], "shapes": {"skin": square, "unused": square},
       "layers": [["band", ["skin"], [], 0.004, 0.002, 0.02, 0.9, 0.5, 1], ["color", ["skin"], [], 0.004, 0.8, [1.05, 1, 0.95], [0, 0, 0], 0.9, 0, 0],
                  ["even", ["skin"], [], 0.004, 0.03, 0.5], ["shine", ["skin"], [], 0.004, 0.03, 0.5, 0.5], ["color", ["skin"], [], 0.004, 1, [1, 1, 1], [0, 0, 0], 1, 0, 0]]}
face = retouch.clean_face(raw, 800 / 1200)
assert [layer[0] for layer in face["layers"]] == ["band", "color", "even", "shine"] and sorted(face["shapes"]) == ["skin"], face
x0, y0, x1, y1 = ImageChops.difference(retouch.paint(im.copy(), {"f": face}), im).getbbox()
assert 440 <= x0 <= 482 and 260 <= y0 <= 302 and 718 <= x1 <= 760 and 538 <= y1 <= 580, (x0, y0, x1, y1)
for bad in (["band", ["nope"], [], 0.004, 0.002, 0.02, 0.9, 0.5, 1], ["band", ["skin"], [], 0.004, 0.02, 0.002, 0.9, 0.5, 1],
            ["color", ["skin"], [], 0.004, 9, [1, 1, 1], [0, 0, 0], 1, 0, 0], ["blur", ["skin"], [], 0.004]):
    try:
        retouch.clean_face({**raw, "layers": [bad]}, 800 / 1200)
        raise SystemExit(f"accepted a bad layer: {bad}")
    except retouch.Invalid:
        pass
PYEOF
chk $? 0 "warp and layers: identity stays identical, changes stay inside the face, bad layers are refused"
RK=$(curl -s -H "$T" -H 'Content-Type: application/json' -d '{"slug":"retouch_test"}' $B/api/manage/albums | j 'd["share_key"]')
RP=$(put $TMP/RT01.JPG "dddd0001$(printf '%036d' 0)" retouch_test | j 'd["photo_id"]')
RU=$B/api/share/$RK/retouch/$RP
rt() { curl -s -H 'Content-Type: application/json' -d "$1" ${2:-$RU}; }
rtc() { curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$1" ${2:-$RU}; }
FA='{"box":[0.2,0.2,0.4,0.4],"params":{"slim":40},"prims":[["m",0.3,0.3,0.08,0.01,0]]}'
FA2='{"box":[0.21,0.2,0.41,0.4],"params":{"slim":80},"prims":[["m",0.3,0.3,0.08,0.02,0]]}'
FB='{"box":[0.6,0.2,0.8,0.4],"params":{"eye":30},"prims":[["s",0.7,0.3,1,0,0.05,0.04,0.2]]}'
chk "$(curl -s $B/api/share/$RK | j 'd["retouch"]["vendor"].startswith("/vendor/")')" True "share json says retouch is on"
chk "$(curl -s $RU | j '(d["head"], d["state"], d["commits"])')" "(None, {}, [])" "no history yet"
R=$(rt "{\"parent\":null,\"author\":\"阿明\",\"message\":\"第一筆\",\"faces\":{\"faceaaaa\":$FA}}"); C1=$(echo $R | j 'd["head"]')
chk "$(echo $R | j '(len(d["commits"]), d["state"]["faceaaaa"]["by"], d["commits"][0]["changed"], d["commits"][0]["message"])')" "(1, '阿明', ['faceaaaa'], '第一筆')" "first commit"
chk "$(curl -s $B/api/share/$RK | j 'd["photos"][0]["retouch"]')" "$C1" "gallery points at the commit"
chk "$(curl -s "$RU?head=$C1" | j 'd')" "{'head': '$C1', 'unchanged': True}" "polling with the current head is cheap"
# 小美開始改的時候還沒人存過（parent = null），她改的是另一張臉 → 自動接在阿明後面
R=$(rt "{\"parent\":null,\"author\":\"小美\",\"faces\":{\"facebbbb\":$FB}}"); C2=$(echo $R | j 'd["head"]')
chk "$(echo $R | j '(sorted(d["state"]), d["commits"][0]["parent"] == d["commits"][1]["id"])')" "(['faceaaaa', 'facebbbb'], True)" "another face merges on top"
# 同一張臉（位置重疊、id 不同）兩邊都改 → 409，什麼都不存；force → 用後到的
R=$(rt "{\"parent\":null,\"author\":\"小美\",\"faces\":{\"facecccc\":$FA2}}")
chk "$(rtc "{\"parent\":null,\"author\":\"小美\",\"faces\":{\"facecccc\":$FA2}}")" 409 "same face changed by both → 409"
chk "$(echo $R | j '(d["code"], d["conflicts"], d["head"])')" "('retouch_conflict', ['faceaaaa'], '$C2')" "conflict names the face, nothing saved"
R=$(rt "{\"parent\":null,\"author\":\"小美\",\"force\":true,\"faces\":{\"facecccc\":$FA2}}"); C3=$(echo $R | j 'd["head"]')
chk "$(echo $R | j '(sorted(d["state"]), d["state"]["faceaaaa"]["params"], d["state"]["faceaaaa"]["by"])')" "(['faceaaaa', 'facebbbb'], {'slim': 80.0}, '小美')" "force keeps mine, under the existing face id"
chk "$(rt "{\"parent\":\"$C3\",\"author\":\"小美\",\"faces\":{\"faceaaaa\":$FA2}}" | j '(d["head"], len(d["commits"]))')" "('$C3', 3)" "saving the same thing adds no commit"
# 輸出：原尺寸、保留 EXIF、只動臉的範圍
curl -s -o $TMP/rt_o.jpg -D $TMP/rt_h.txt $B/media/$RK/retouch/$C3/o/$RP
chk "$(grep -ci 'RT01_retouch.jpg' $TMP/rt_h.txt)" 1 "retouched download has its own filename"
chk "$($PY -c "
from PIL import Image, ImageChops, ImageStat
a, b = Image.open('$TMP/RT01.JPG'), Image.open('$TMP/rt_o.jpg')
d = ImageChops.difference(a, b)
inside, outside = ImageStat.Stat(d.crop((310, 310, 410, 410))).mean[0], ImageStat.Stat(d.crop((0, 600, 200, 800))).mean[0]
print(b.size, b.getexif().get_ifd(0x8769).get(36867), inside > 4 * outside)")" "(1200, 800) 2026:11:20 10:00:00 True" "full-size render: warped at the face, untouched elsewhere, EXIF kept"
chk "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' $B/media/$RK/retouch/$C3/t/$RP)" "200 image/webp" "retouched thumbnail"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$RK/retouch/000000000000/p/$RP)" 404 "unknown commit → 404"
chk "$(curl -s $B/api/share/$RK/zip | $PY -c "import sys, zipfile, io; print(sorted(zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())).namelist()))")" "['RT01.JPG', 'RT01_retouch.jpg']" "zip has the original and the retouched copy"
# 還原：不刪紀錄，還原本身也是一筆
R=$(rt "{\"commit\":\"$C1\",\"author\":\"阿明\"}" $RU/restore)
chk "$(echo $R | j '(sorted(d["state"]), d["state"]["faceaaaa"]["params"], d["commits"][0]["kind"], d["commits"][0]["ref"], len(d["commits"]))')" "(['faceaaaa'], {'slim': 40.0}, 'restore', '$C1', 4)" "restore is a new commit, history kept"
chk "$(ls $TMP/data/retouch/*/ | grep -c "_${C3}_")" 0 "renders of the replaced commit are removed"
R=$(rt '{"commit":null,"author":"阿明"}' $RU/restore)
chk "$(echo $R | j '(d["state"], len(d["commits"]))')" "({}, 5)" "restore to the original"
chk "$(curl -s $B/api/share/$RK | j 'd["photos"][0].get("retouch")')" None "gallery shows the original again"
chk "$(curl -s $RU/commits/$C2 | j 'sorted(d["state"])')" "['faceaaaa', 'facebbbb']" "old commits stay readable"
# 擋掉的
chk "$(rtc '{"author":"x","faces":{"faceaaaa":{"box":[0.2,0.2,0.4,0.4],"prims":[["m",0.9,0.6,0.05,0.01,0]]}}}')" 400 "can't warp far from the face"
chk "$(rtc '{"author":"x","faces":{"faceaaaa":{"box":[0.2,0.2,0.4,0.4],"prims":[["m",0.3,0.3,0.05,0.04,0]]}}}')" 400 "can't push further than half the radius"
chk "$(rtc "{\"author\":\"x\",\"faces\":{\"BAD ID\":$FA}}")" 400 "bad face id → 400"
chk "$(rtc "{\"author\":\"x\",\"parent\":\"000000000000\",\"faces\":{\"faceaaaa\":$FA}}")" 404 "unknown parent → 404"
chk "$(curl -s -H 'Accept-Language: en' -H 'Content-Type: application/json' -d "{\"author\":\"\",\"faces\":{\"faceaaaa\":$FA}}" $RU | j 'd["detail"]')" "The name must be 1 to 24 characters" "a name is required, errors are localized"
chk "$(rtc "{\"author\":\"x\",\"faces\":{\"faceaaaa\":$FA}}" $B/api/share/nokey/retouch/$RP)" 404 "unknown share key → 404"
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/vendor/mediapipe-0.0.0/vision_bundle.mjs)" 404 "only the pinned vendor files are served"
# 皮膚、膚色的圖層也跟著紀錄走，輸出時先調色再變形
FL='{"box":[0.2,0.2,0.4,0.4],"params":{"smooth":60,"whiten":30},"shapes":{"oval":[0.22,0.22,0.38,0.22,0.38,0.38,0.22,0.38]},"layers":[["band",["oval"],[],0.004,0.001,0.01,0.6,0.02,0.1],["color",["oval"],[],0.004,0.8,[1,1,1],[0,0,0],0.95,0,0]]}'
R=$(rt "{\"parent\":null,\"author\":\"小美\",\"faces\":{\"faceaaaa\":$FL}}"); C6=$(echo $R | j 'd["head"]')
chk "$(echo $R | j '([l[0] for l in d["state"]["faceaaaa"]["layers"]], d["state"]["faceaaaa"]["prims"], d["commits"][0]["params"]["faceaaaa"])')" "(['band', 'color'], [], {'smooth': 60.0, 'whiten': 30.0})" "skin layers are saved with the face"
curl -s -o $TMP/rt_l.jpg $B/media/$RK/retouch/$C6/o/$RP
chk "$($PY -c "
from PIL import Image, ImageStat
a, b = Image.open('$TMP/RT01.JPG'), Image.open('$TMP/rt_l.jpg')
inside = lambda im: ImageStat.Stat(im.crop((300, 300, 420, 420)).convert('L'))
print(inside(b).mean[0] > inside(a).mean[0] + 5, inside(b).stddev[0] < inside(a).stddev[0])")" "True True" "render with layers: brighter and smoother inside the shape"
chk "$(rtc '{"author":"x","faces":{"faceaaaa":{"box":[0.2,0.2,0.4,0.4],"shapes":{"oval":[0.22,0.22,0.38,0.22,0.38,0.38]},"layers":[["band",["nope"],[],0.004,0.001,0.01,0.6,0.02,0.1]]}}}')" 400 "layer naming a missing shape → 400"
rt "{\"commit\":null,\"author\":\"阿明\"}" $RU/restore >/dev/null
# 刪照片：紀錄和輸出的檔一起清
C5=$(rt "{\"parent\":null,\"author\":\"阿明\",\"faces\":{\"faceaaaa\":$FA}}" | j 'd["head"]')
chk "$(curl -s -o /dev/null -w '%{http_code}' $B/media/$RK/retouch/$C5/s/$RP)" 200 "render again after restoring"
curl -s -o /dev/null -H "$T" -H 'Content-Type: application/json' -d "{\"photo_ids\":[\"$RP\"]}" $B/api/manage/albums/retouch_test/delete-photos
chk "$(ls $TMP/data/retouch/*/ | grep -c .)" 0 "deleting the photo removes its renders"
chk "$(curl -s -o /dev/null -w '%{http_code}' $RU)" 404 "and its history"

echo; echo "passed $pass, failed $fail"
[ $fail -eq 0 ]
