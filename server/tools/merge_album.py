"""把一本相簿併進另一本：舊版 app 上傳中文相簿名時被切掉，伺服器多開了一本（例：「2026_11_15_」→「2026_11_15_殘星會」）。

在 server/ 目錄、用伺服器的環境變數跑（先停服務，避免同時寫入）：

    systemctl stop culldrop
    set -a; . /etc/culldrop.env; set +a
    .venv/bin/python -m tools.merge_album 2026_11_15_ 2026_11_15_殘星會            # 只看會做什麼
    .venv/bin/python -m tools.merge_album 2026_11_15_ 2026_11_15_殘星會 --apply    # 真的做
    systemctl start culldrop

照片檔、NAS 上的檔案都不動（NAS 資料夾名稱維持原樣）。分享連結：來源相簿的整本連結會失效，人物連結會搬過去。
"""
import sys

from app.main import db


def plan(con, src_slug: str, dst_slug: str) -> dict:
    src = con.execute("SELECT * FROM albums WHERE slug=?", (src_slug,)).fetchone()
    dst = con.execute("SELECT * FROM albums WHERE slug=?", (dst_slug,)).fetchone()
    if not src or not dst:
        raise SystemExit(f"找不到相簿：{src_slug if not src else dst_slug}")
    if src["id"] == dst["id"]:
        raise SystemExit("來源和目標是同一本")
    have = {r[0] for r in con.execute("SELECT photo_id FROM photos WHERE album_id=?", (dst["id"],))}
    photos = con.execute("SELECT photo_id FROM photos WHERE album_id=?", (src["id"],)).fetchall()
    return {"src": src, "dst": dst, "move": sum(r[0] not in have for r in photos), "dupe": sum(r[0] in have for r in photos)}


def merge(con, src, dst):
    s, d = src["id"], dst["id"]
    # 照片：目標已經有同一張就丟掉來源那筆
    con.execute("DELETE FROM photos WHERE album_id=? AND photo_id IN (SELECT photo_id FROM photos WHERE album_id=?)", (s, d))
    con.execute("UPDATE photos SET album_id=? WHERE album_id=?", (d, s))
    # 人物連結：同一個人就把照片併過去，其他整筆搬
    for p in con.execute("SELECT * FROM album_people WHERE album_id=?", (s,)).fetchall():
        same = con.execute("SELECT id FROM album_people WHERE album_id=? AND person_id=?", (d, p["person_id"])).fetchone()
        if same:
            con.execute("INSERT OR IGNORE INTO album_people_photos (person_pk, photo_id) SELECT ?, photo_id FROM album_people_photos WHERE person_pk=?", (same["id"], p["id"]))
            con.execute("DELETE FROM album_people WHERE id=?", (p["id"],))
        else:
            con.execute("UPDATE album_people SET album_id=? WHERE id=?", (d, p["id"]))
    con.execute("INSERT OR IGNORE INTO album_face_tags (album_id, name, photo_id) SELECT ?, name, photo_id FROM album_face_tags WHERE album_id=?", (d, s))
    con.execute("DELETE FROM album_face_tags WHERE album_id=?", (s,))
    con.execute("DELETE FROM album_face_revs WHERE album_id=?", (s,))
    con.execute("UPDATE album_face_revs SET rev=rev+1 WHERE album_id=?", (d,))   # 手機重讀
    # 用 slug 記的：上傳紀錄（之後重傳認得）、RAW 等配對、NAS 佇列（檔案位置不變）
    for table, col in (("uploads", "album"), ("raw_pending", "album")):
        con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (dst["slug"], src["slug"]))
    con.execute("UPDATE OR IGNORE nas_files SET album=? WHERE album=?", (dst["slug"], src["slug"]))
    con.execute("DELETE FROM nas_files WHERE album=?", (src["slug"],))
    con.execute("UPDATE albums SET updated_at=strftime('%s','now') WHERE id=?", (d,))
    con.execute("DELETE FROM albums WHERE id=?", (s,))


def main(argv):
    if len(argv) < 2:
        raise SystemExit(__doc__)
    apply = "--apply" in argv
    src_slug, dst_slug = [a for a in argv if a != "--apply"][:2]
    with db() as con:
        p = plan(con, src_slug, dst_slug)
        print(f"「{src_slug}」→「{dst_slug}」：搬 {p['move']} 張，重複 {p['dupe']} 張（丟掉來源那筆），「{src_slug}」會刪掉")
        if not apply:
            print("只是預覽；確定要做加 --apply")
            return
        merge(con, p["src"], p["dst"])
        n = con.execute("SELECT COUNT(*) FROM photos WHERE album_id=?", (p["dst"]["id"],)).fetchone()[0]
        print(f"完成：「{dst_slug}」現在 {n} 張")


if __name__ == "__main__":
    main(sys.argv[1:])
