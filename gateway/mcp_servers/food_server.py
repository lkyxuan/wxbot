# -*- coding: utf-8 -*-
"""
美食地图 MCP 服务
=================

群友转发的高德 / 大众点评分享、或者直接说「XX 路的 YY 火锅不错」，
由 AI 调用这里的工具：用高德开放平台查到店铺的准确信息，存进本地 SQLite，
之后可以按关键词 / 城市 / 附近查询，并生成高德地图链接。

- 高德：分享链接里带店铺 ID（B0 开头）时直接按 ID 精确查询。
- 大众点评：没有个人可用的开放接口，网页反爬严格，这里不抓点评页面，
  只用卡片标题里的店名去高德搜。

环境变量：
    AMAP_KEY       高德开放平台「Web 服务」类型的 Key（必填）
    FOOD_DB_PATH   数据库路径，默认 gateway/data/food.db
    AMAP_API_BASE  调试用，默认 https://restapi.amap.com

单独运行（调试用）：python food_server.py
"""

import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from mcp.server.mcpserver import MCPServer

API_BASE = os.environ.get("AMAP_API_BASE", "https://restapi.amap.com")
TIMEOUT = 15
DB_PATH = os.environ.get(
    "FOOD_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "food.db"),
)

# 高德 POI ID：B0 开头的 10 位字母数字
# 静态地图、店铺照片的本地存放目录；机器人配置 ai_reply_image_dirs 要包含它
IMAGE_DIR = os.environ.get("FOOD_IMAGE_DIR", os.path.join(os.path.dirname(DB_PATH), "images"))

# 网页地图读取的数据文件（网关把 /food-map 映射到这个目录，见 config.example.json）
MAP_DATA_DIR = os.environ.get("FOOD_MAP_DATA_DIR", os.path.join(os.path.dirname(DB_PATH), "map"))

POI_ID_RE = re.compile(r"\bB0[0-9A-Z]{8}\b")
URL_RE = re.compile(r"https?://[^\s<>\"'，。]+")
AMAP_HOSTS = ("amap.com", "autonavi.com")

server = MCPServer("food")


# ============================================================
# 数据库
# ============================================================
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS places (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            poi_id     TEXT UNIQUE,
            name       TEXT NOT NULL,
            city       TEXT,
            district   TEXT,
            address    TEXT,
            lng        REAL,
            lat        REAL,
            category   TEXT,
            tel        TEXT,
            rating     TEXT,
            cost       TEXT,
            photo_url  TEXT,
            note       TEXT,
            added_by   TEXT,
            source     TEXT,
            created_at INTEGER
        )
    """)
    # 底库（refresh_base.py）用到的字段，旧库自动补上
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(places)")}
    for col, decl in (
        ("distance_m", "REAL"),            # 距云谷中心的直线距离（米）
        ("open_time", "TEXT"),             # 营业时间
        ("dianping_url", "TEXT"),
        ("origin", "TEXT"),                # 底库扫描 / 群友分享
        ("status", "TEXT DEFAULT '营业中'"),  # 营业中 / 疑似关店
        ("miss_count", "INTEGER DEFAULT 0"),  # 底库更新时连续几次没搜到
        ("refreshed_at", "INTEGER"),       # 最近一次从高德更新的时间
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE places ADD COLUMN {col} {decl}")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _export_map_data():
    """把收录的店写成网页地图用的 data.json；每次增删后调用。只导出展示需要的字段。"""
    fields = ("id", "name", "city", "district", "address", "lng", "lat", "category", "rating", "cost", "note", "added_by")
    with _db() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(fields)} FROM places WHERE COALESCE(status, '') != '疑似关店' ORDER BY created_at DESC"
        ).fetchall()
    os.makedirs(MAP_DATA_DIR, exist_ok=True)
    tmp = os.path.join(MAP_DATA_DIR, "data.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": int(time.time()), "places": [dict(r) for r in rows]}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(MAP_DATA_DIR, "data.json"))


def _place_line(row):
    parts = [f"#{row['id']} {row['name']}"]
    if row["district"] or row["address"]:
        parts.append(f"{row['city'] or ''}{row['district'] or ''} {row['address'] or ''}".strip())
    extra = []
    if row["category"]:
        extra.append(row["category"].split(";")[-1])
    if row["rating"]:
        extra.append(f"评分{row['rating']}")
    if row["cost"]:
        extra.append(f"人均{row['cost']}元")
    if extra:
        parts.append("，".join(extra))
    if row["note"]:
        parts.append(f"备注：{row['note']}")
    if row["added_by"]:
        parts.append(f"推荐人：{row['added_by']}")
    return "｜".join(parts)


def _map_link(lng, lat, name):
    # 高德 URI API：无需 Key，手机上点开会唤起高德 App / 网页地图
    return f"https://uri.amap.com/marker?position={lng},{lat}&name={quote(name)}&src=wxbot&coordinate=gaode&callnative=1"


# ============================================================
# 高德接口
# ============================================================
def _amap(path, params):
    key = os.environ.get("AMAP_KEY", "").strip()
    if not key:
        raise RuntimeError("没有配置高德 Key（环境变量 AMAP_KEY）")
    data = requests.get(f"{API_BASE}{path}", params={**params, "key": key}, timeout=TIMEOUT).json()
    if str(data.get("status")) != "1":
        raise RuntimeError(f"高德接口报错：{data.get('info')}（{data.get('infocode')}）")
    return data


def _str(v):
    # 高德空字段会返回 [] 而不是空字符串
    return v if isinstance(v, str) else ""


def _parse_poi(p):
    lng, lat = (_str(p.get("location")) or ",").split(",")[:2]
    biz = p.get("biz_ext") or {}
    photos = p.get("photos") or []
    return {
        "poi_id": _str(p.get("id")),
        "name": _str(p.get("name")),
        "city": _str(p.get("cityname")),
        "district": _str(p.get("adname")),
        "address": _str(p.get("address")),
        "lng": float(lng) if lng else None,
        "lat": float(lat) if lat else None,
        "category": _str(p.get("type")),
        "tel": _str(p.get("tel")),
        "rating": _str(biz.get("rating")),
        "cost": _str(biz.get("cost")),
        "photo_url": _str(photos[0].get("url")) if photos else "",
    }


def _poi_by_id(poi_id):
    pois = _amap("/v3/place/detail", {"id": poi_id, "extensions": "all"}).get("pois") or []
    return _parse_poi(pois[0]) if pois else None


def _poi_search(keyword, city=""):
    params = {"keywords": keyword, "extensions": "all", "offset": 5, "page": 1}
    if city:
        params.update({"city": city, "citylimit": "true"})
    return [_parse_poi(p) for p in _amap("/v3/place/text", params).get("pois") or []]


def _locate(location):
    """地点文字转坐标：先按地址地理编码，失败再按地名搜 POI（微信位置消息常是「商场名 + 地址」）。"""
    geos = _amap("/v3/geocode/geo", {"address": location}).get("geocodes") or []
    if geos and _str(geos[0].get("location")):
        return tuple(map(float, geos[0]["location"].split(",")))
    for p in _poi_search(location):
        if p["lng"] is not None:
            return p["lng"], p["lat"]
    return None


def _poi_ids_from_text(text):
    """从文字和其中的高德链接里找 POI ID；高德短链接会跟随跳转后再找。
    大众点评等其它链接不去请求。"""
    ids = POI_ID_RE.findall(text)
    for url in URL_RE.findall(text):
        host = urlparse(url).hostname or ""
        if not host.endswith(AMAP_HOSTS):
            continue
        try:
            final = requests.get(url, timeout=TIMEOUT, allow_redirects=True).url
        except requests.RequestException:
            continue
        ids += POI_ID_RE.findall(final)
        for values in parse_qs(urlparse(final).query).values():
            for v in values:
                ids += POI_ID_RE.findall(v)
    return list(dict.fromkeys(ids))


# ============================================================
# 工具
# ============================================================
@server.tool()
def find_place(text: str, city: str = "") -> str:
    """识别一家店：可以传群友转发的高德 / 大众点评分享内容（卡片标题或链接），
    也可以传店名。返回高德上匹配到的候选店铺（含 POI ID），供确认后调用 save_place 保存。

    text: 分享内容、链接或店名，例如「海底捞(五角场店)」
    city: 城市，可选，例如「上海」；能明显缩小搜索范围
    """
    ids = _poi_ids_from_text(text)
    candidates = [p for p in (_poi_by_id(i) for i in ids[:3]) if p]
    if not candidates:
        # 去掉链接和分享套话，剩下的当作店名搜
        keyword = URL_RE.sub(" ", text)
        keyword = re.sub(r"[【】\[\]「」·•|]|人均\s*\S*元?|大众点评|美团|高德地图|分享|推荐|快来看看|小程序", " ", keyword)
        keyword = " ".join(keyword.split())[:40]
        if not keyword:
            return "没能从内容里识别出店名，请直接告诉我店名和城市"
        candidates = _poi_search(keyword, city)
    if not candidates:
        return "高德上没搜到这家店，可以换个店名或补充城市再试"
    lines = []
    for i, p in enumerate(candidates[:5], 1):
        lines.append(
            f"{i}. {p['name']}｜{p['city']}{p['district']} {p['address']}｜"
            f"{(p['category'] or '').split(';')[-1]}｜评分{p['rating'] or '无'}｜POI ID：{p['poi_id']}"
        )
    return "\n".join(lines)


@server.tool()
def save_place(poi_id: str, note: str = "", added_by: str = "", source: str = "") -> str:
    """把一家店存进美食地图。先用 find_place 拿到 POI ID 再调用。
    同一家店重复保存时，只更新备注和推荐人。

    poi_id: 高德 POI ID（B0 开头）
    note: 群友的评价或推荐理由，可选
    added_by: 推荐人昵称，可选
    source: 来源，例如「高德分享」「大众点评分享」「群聊」，可选
    """
    p = _poi_by_id(poi_id.strip())
    if not p:
        return f"高德上查不到这个 POI ID：{poi_id}"
    with _db() as conn:
        existing = conn.execute("SELECT * FROM places WHERE poi_id = ?", (p["poi_id"],)).fetchone()
        if existing:
            conn.execute(
                "UPDATE places SET note = COALESCE(NULLIF(?, ''), note), added_by = COALESCE(NULLIF(?, ''), added_by) WHERE id = ?",
                (note, added_by, existing["id"]),
            )
            row = conn.execute("SELECT * FROM places WHERE id = ?", (existing["id"],)).fetchone()
        if existing:
            _export_map_data()
            return "这家店已经在美食地图里了，已更新备注：\n" + _place_line(row)
        cur = conn.execute(
            """INSERT INTO places (poi_id, name, city, district, address, lng, lat, category, tel,
                                   rating, cost, photo_url, note, added_by, source, origin, created_at)
               VALUES (:poi_id, :name, :city, :district, :address, :lng, :lat, :category, :tel,
                       :rating, :cost, :photo_url, :note, :added_by, :source, '群友分享', :created_at)""",
            {**p, "note": note, "added_by": added_by, "source": source, "created_at": int(time.time())},
        )
        row = conn.execute("SELECT * FROM places WHERE id = ?", (cur.lastrowid,)).fetchone()
    _export_map_data()
    return "已收录：\n" + _place_line(row) + "\n地图：" + _map_link(row["lng"], row["lat"], row["name"])


@server.tool()
def search_places(keyword: str = "", city: str = "", limit: int = 10) -> str:
    """在已收录的美食地图里查店，按店名、分类、地址、备注模糊匹配。
    keyword 留空时返回最近收录的店。

    keyword: 关键词，例如「火锅」「五角场」
    city: 城市，可选
    limit: 最多返回几家，默认 10
    """
    sql, args = "SELECT * FROM places WHERE COALESCE(status, '') != '疑似关店'", []
    if keyword:
        sql += " AND (name LIKE ? OR category LIKE ? OR address LIKE ? OR district LIKE ? OR note LIKE ?)"
        args += [f"%{keyword}%"] * 5
    if city:
        sql += " AND city LIKE ?"
        args.append(f"%{city}%")
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 30)))
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
    if not rows:
        return "美食地图里还没有符合条件的店"
    return "\n".join(_place_line(r) for r in rows)


@server.tool()
def nearby_places(location: str, radius_km: float = 3, limit: int = 10) -> str:
    """查某个地点附近已收录的店，按距离从近到远。

    location: 地点或地址，例如「上海五角场」「杭州西湖」，或群友发的微信位置里的地点名和地址
    radius_km: 半径（公里），默认 3
    limit: 最多返回几家，默认 10
    """
    center = _locate(location)
    if not center:
        return f"高德上找不到这个地点：{location}"
    lng0, lat0 = center
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM places WHERE lng IS NOT NULL AND lat IS NOT NULL AND COALESCE(status, '') != '疑似关店'"
        ).fetchall()

    def dist_km(r):
        # 球面距离（haversine），城市范围内足够准确
        p1, p2 = math.radians(lat0), math.radians(r["lat"])
        dp, dl = p2 - p1, math.radians(r["lng"] - lng0)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(a))

    hits = sorted(((dist_km(r), r) for r in rows), key=lambda x: x[0])
    hits = [(d, r) for d, r in hits if d <= radius_km][: max(1, min(int(limit), 30))]
    if not hits:
        return f"{location} 附近 {radius_km} 公里内还没有收录的店"
    return "\n".join(f"{_place_line(r)}｜距离{d:.1f}公里" for d, r in hits)


@server.tool()
def place_map_link(place_id: int) -> str:
    """生成某家已收录店铺的高德地图链接（手机点开即可导航）。

    place_id: 店铺在美食地图里的编号（查询结果里 # 后面的数字）
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
    if not row:
        return f"没有编号为 {place_id} 的店"
    if row["lng"] is None:
        return f"{row['name']} 没有坐标，无法生成地图链接"
    return f"{row['name']}：{_map_link(row['lng'], row['lat'], row['name'])}"


def _download(url, params, filename):
    """下载图片到 IMAGE_DIR；已下载过直接复用。返回绝对路径，失败返回空字符串。"""
    path = os.path.abspath(os.path.join(IMAGE_DIR, filename))
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    os.makedirs(IMAGE_DIR, exist_ok=True)
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException:
        return ""
    if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
        return ""
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


@server.tool()
def place_images(place_id: int, include_photo: bool = True) -> str:
    """生成某家已收录店铺的图片：高德静态地图（标出店铺位置），以及高德上的店铺照片（如果有）。
    返回的每张图片是单独一行「[图片]本地路径」，回复时把这些行原样单独成行写出，机器人会把它们作为图片发到群里。

    place_id: 店铺编号
    include_photo: 是否附带店铺照片，默认是
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
    if not row:
        return f"没有编号为 {place_id} 的店"
    lines = []
    if row["lng"] is not None:
        loc = f"{row['lng']},{row['lat']}"
        key = os.environ.get("AMAP_KEY", "").strip()
        path = _download(
            f"{API_BASE}/v3/staticmap",
            {"location": loc, "zoom": 16, "size": "600*400", "scale": 2, "markers": f"mid,,A:{loc}", "key": key},
            f"map_{row['id']}.png",
        )
        if path:
            lines.append(f"[图片]{path}")
    if include_photo and row["photo_url"]:
        ext = os.path.splitext(urlparse(row["photo_url"]).path)[1].lower()
        ext = ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
        path = _download(row["photo_url"], None, f"photo_{row['id']}{ext}")
        if path:
            lines.append(f"[图片]{path}")
    if not lines:
        return f"{row['name']} 暂时没有可发的图片"
    return f"{row['name']} 的图片：\n" + "\n".join(lines)


@server.tool()
def delete_place(place_id: int) -> str:
    """从美食地图里删除一家店。只在群友明确要求删除时调用。

    place_id: 店铺编号
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
        if not row:
            return f"没有编号为 {place_id} 的店"
        conn.execute("DELETE FROM places WHERE id = ?", (int(place_id),))
    _export_map_data()
    return f"已删除：{row['name']}"


@server.tool()
def map_page_link() -> str:
    """返回美食地图网页的链接：一张地图上标出所有收录的店，可搜索。群友想看全部店铺、要「地图」时发这个链接。"""
    url = os.environ.get("FOOD_MAP_URL", "").strip()
    if not url:
        return "网页地图还没有配置对外访问地址（环境变量 FOOD_MAP_URL）"
    return f"美食地图（共收录的店都在上面）：{url}"


if __name__ == "__main__":
    _export_map_data()
    server.run()
