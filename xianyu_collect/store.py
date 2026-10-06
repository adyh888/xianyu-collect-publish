"""SQLite 存储：采集商品表 + 详情表 + 采集日志。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    title        TEXT,
    price        TEXT,
    pic_url      TEXT,
    area         TEXT,
    seller       TEXT,
    want_count   INTEGER DEFAULT 0,
    publish_time TEXT,
    item_url     TEXT,
    keyword      TEXT,
    raw_json     TEXT,
    first_seen_at REAL,
    updated_at   REAL
);
CREATE TABLE IF NOT EXISTS item_details (
    item_id      TEXT PRIMARY KEY REFERENCES items(item_id),
    title        TEXT,
    price        TEXT,
    descr        TEXT,
    image_urls   TEXT,  -- JSON 数组
    sku_json     TEXT,  -- JSON [{props, price, quantity, image}]
    quantity     INTEGER,
    seller_id    TEXT,
    raw_json     TEXT,
    collected_at REAL
);
CREATE TABLE IF NOT EXISTS collect_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    action  TEXT,
    keyword TEXT,
    ok      INTEGER,
    count   INTEGER,
    message TEXT
);
"""


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI 线程池会跨线程使用连接：check_same_thread=False + 写锁
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # 轻量迁移：旧库补列
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(item_details)")}
            if "seller_id" not in cols:
                self._conn.execute("ALTER TABLE item_details ADD COLUMN seller_id TEXT")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- 商品

    def upsert_items(self, items: list, keyword: str = "") -> int:
        now = time.time()
        with self._lock, self._conn:
            for it in items:
                self._conn.execute(
                    """
                    INSERT INTO items (item_id, title, price, pic_url, area, seller,
                                       want_count, publish_time, item_url, keyword,
                                       raw_json, first_seen_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        title=excluded.title, price=excluded.price, pic_url=excluded.pic_url,
                        area=excluded.area, seller=excluded.seller, want_count=excluded.want_count,
                        publish_time=excluded.publish_time, item_url=excluded.item_url,
                        keyword=excluded.keyword, raw_json=excluded.raw_json, updated_at=excluded.updated_at
                    """,
                    (
                        it["item_id"], it.get("title"), it.get("price"), it.get("pic_url"),
                        it.get("area"), it.get("seller"), it.get("want_count", 0),
                        it.get("publish_time"), it.get("item_url"), keyword,
                        json.dumps(it.get("raw_data"), ensure_ascii=False),
                        now, now,
                    ),
                )
        return len(items)

    def upsert_detail(self, item_id: str, detail: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO item_details (item_id, title, price, descr, image_urls,
                                          sku_json, quantity, seller_id, raw_json, collected_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price, descr=excluded.descr,
                    image_urls=excluded.image_urls, sku_json=excluded.sku_json,
                    quantity=excluded.quantity, seller_id=excluded.seller_id,
                    raw_json=excluded.raw_json, collected_at=excluded.collected_at
                """,
                (
                    item_id, detail.get("title"), detail.get("price"), detail.get("desc"),
                    json.dumps(detail.get("images") or [], ensure_ascii=False),
                    json.dumps(detail.get("skus") or [], ensure_ascii=False),
                    detail.get("quantity"),
                    detail.get("seller_id") or "",
                    json.dumps(detail.get("raw_data"), ensure_ascii=False),
                    time.time(),
                ),
            )

    # ---------------------------------------------------------------- 查询

    def list_items(self, limit: int = 20, keyword: str = "") -> list:
        sql = "SELECT item_id, title, price, want_count, keyword FROM items"
        params: tuple = ()
        if keyword:
            sql += " WHERE keyword LIKE ?"
            params = (f"%{keyword}%",)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def get_detail(self, item_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM item_details WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["image_urls"] = json.loads(d.get("image_urls") or "[]")
        d["skus"] = json.loads(d.get("sku_json") or "[]")
        return d

    def log(self, action: str, keyword: str = "", ok: bool = True, count: int = 0, message: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO collect_log (ts, action, keyword, ok, count, message) VALUES (?,?,?,?,?,?)",
                (time.time(), action, keyword, 1 if ok else 0, count, message[:500]),
            )
