# -*- coding: utf-8 -*-
"""
云谷美食 MCP 服务
=================

店库分两层，都在本地 SQLite：
- 店铺（places）：公开信息，来自高德。底库由 refresh_base.py 批量拉云谷周边的店，
  群友转发的高德 / 大众点评分享也会入库。
- 口碑（mentions）：群友说过的话，每条挂在一家店上（推荐、踩雷、去过、想去……）。
  推荐时群友口碑优先于高德评分。

- 高德：分享链接里带店铺 ID（B0 开头）时直接按 ID 精确查询。
- 大众点评：没有个人可用的开放接口，网页反爬严格，这里不抓点评页面，
  只用卡片标题里的店名去高德搜。
- 店铺都给高德链接（uri.amap.com），手机点开唤起高德 App，不自建网页。

环境变量：
    AMAP_KEY       高德开放平台「Web 服务」类型的 Key（必填）
    FOOD_DB_PATH   数据库路径，默认 gateway/data/food.db
    AMAP_API_BASE  调试用，默认 https://restapi.amap.com

单独运行（调试用）：python food_server.py
"""

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

# 静态地图、店铺照片的本地存放目录；机器人配置 ai_reply_image_dirs 要包含它
IMAGE_DIR = os.environ.get("FOOD_IMAGE_DIR", os.path.join(os.path.dirname(DB_PATH), "images"))

# 高德 POI ID：B0 开头的 10 位字母数字
POI_ID_RE = re.compile(r"\bB0[0-9A-Z]{8}\b")
URL_RE = re.compile(r"https?://[^\s<>\"'，。]+")
AMAP_HOSTS = ("amap.com", "autonavi.com")

# 底库还没跑过（meta 里没有圆心）时用的云谷中心：高德「云谷中心」灯彩街1009号
DEFAULT_CENTER = (120.040246, 30.315336, "云谷中心")

MENTION_KINDS = ("推荐", "踩雷", "一般", "去过", "想去", "分享")
# 群友评分：没报分数的评价按语气折算
KIND_SCORE = {"推荐": 4, "一般": 3, "踩雷": 2}
OPEN = "COALESCE(p.status, '') != '疑似关店'"

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
    # 口碑：群友说过的话，一条挂一家店
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mentions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id   INTEGER NOT NULL,
            sender     TEXT,
            said_at    INTEGER,
            kind       TEXT,      -- 推荐 / 踩雷 / 一般 / 去过 / 想去 / 分享
            score      INTEGER,   -- 1～5，只有明确说了才填
            tags       TEXT,      -- 要点，逗号分隔
            quote      TEXT,      -- 原话
            via        TEXT,      -- 自动抽取 / 打卡指令 / 分享卡片
            msg_key    TEXT,      -- 来源消息标识，防止同一句话重复入库
            created_at INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_place ON mentions(place_id)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mentions_msg ON mentions(msg_key, place_id) "
        "WHERE msg_key IS NOT NULL AND msg_key != ''"
    )
    return conn


def _center(conn):
    """云谷中心坐标：底库脚本存的圆心优先。"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'base_center'").fetchone()
    if row:
        lng, lat, name = row["value"].split(",", 2)
        return float(lng), float(lat), name
    return DEFAULT_CENTER


def _dist_km(lng0, lat0, lng, lat):
    # 球面距离（haversine），城市范围内足够准确
    p1, p2 = math.radians(lat0), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lng - lng0)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mention_stats(conn, place_ids):
    """每家店的口碑汇总：{place_id: {"good", "bad", "went", "want", "total", "tags", "quotes", "score", "raters"}}。
    群友评分 score：每人只算最新的一次评价，报了分用原分，没报按语气折算（KIND_SCORE），满分 5；raters 是评过分的人数。"""
    stats = {}
    if not place_ids:
        return stats
    ids = list(place_ids)
    rows = []
    for i in range(0, len(ids), 500):  # 底库有上万家店，分批查，避开 SQLite 参数个数上限
        chunk = ids[i:i + 500]
        rows += conn.execute(
            f"SELECT * FROM mentions WHERE place_id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall()
    rows.sort(key=lambda m: (m["said_at"] or 0, m["id"]), reverse=True)
    latest = {}  # (place_id, 发言人) -> 这个人最新一次给的分
    for m in rows:
        s = stats.setdefault(m["place_id"], {"good": 0, "bad": 0, "went": set(), "want": set(), "total": 0, "tags": [], "quotes": []})
        s["total"] += 1
        score = m["score"] or 0
        personal = score or KIND_SCORE.get(m["kind"])
        if personal:
            # 按时间倒序，第一次遇到的就是这个人最新的评价；没署名的每条单独算
            latest.setdefault((m["place_id"], m["sender"] or f"#{m['id']}"), personal)
        if m["kind"] == "推荐" or score >= 4:
            s["good"] += 1
        elif m["kind"] == "踩雷" or (score and score <= 2):
            s["bad"] += 1
        if m["sender"]:
            if m["kind"] == "想去":
                s["want"].add(m["sender"])
            elif m["kind"] in ("推荐", "踩雷", "一般", "去过"):
                s["went"].add(m["sender"])
        for t in re.split(r"[，,、;；]\s*", m["tags"] or ""):
            if t and t not in s["tags"]:
                s["tags"].append(t)
        if m["quote"] and len(s["quotes"]) < 2 and m["kind"] != "想去":
            s["quotes"].append(f"{m['sender'] or '群友'}：{m['quote']}")
    for s in stats.values():
        s["score"], s["raters"] = None, 0
    for (place_id, _), score in latest.items():
        s = stats[place_id]
        s["raters"] += 1
        s["score"] = (s["score"] or 0) + score
    for s in stats.values():
        if s["raters"]:
            s["score"] = round(s["score"] / s["raters"], 1)
    return stats


def _stats_text(s):
    if not s:
        return ""
    parts = []
    counts = [f"推荐{s['good']}" if s["good"] else "", f"踩雷{s['bad']}" if s["bad"] else ""]
    counts = "、".join(c for c in counts if c)
    if s["raters"]:
        parts.append(f"群友评分{s['score']}（{s['raters']}人评" + (f"，{counts}" if counts else "") + "）")
    else:
        parts.append("群友口碑：" + (counts or f"提到{s['total']}次"))
    if s["tags"]:
        parts.append("要点：" + "、".join(s["tags"][:5]))
    parts += [f"「{q}」" for q in s["quotes"]]
    return "；".join(parts)


def _place_line(row, stats=None, dist_km=None):
    parts = [f"#{row['id']} {row['name']}"]
    if row["district"] or row["address"]:
        parts.append(f"{row['city'] or ''}{row['district'] or ''} {row['address'] or ''}".strip())
    extra = []
    if row["category"]:
        extra.append(row["category"].split(";")[-1])
    if row["rating"]:
        extra.append(f"高德评分{row['rating']}")
    if row["cost"]:
        extra.append(f"人均{row['cost']}元")
    if dist_km is None and row["distance_m"] is not None:
        dist_km = row["distance_m"] / 1000
    if dist_km is not None:
        extra.append(f"距离{dist_km:.1f}公里")
    if extra:
        parts.append("，".join(extra))
    if stats:
        parts.append(_stats_text(stats))
    if row["note"]:
        parts.append(f"备注：{row['note']}")
    return "｜".join(parts)


def _map_link(lng, lat, name):
    # 高德 URI API：无需 Key，手机上点开会唤起高德 App
    return f"https://uri.amap.com/marker?position={lng},{lat}&name={quote(name)}&src=wxbot&coordinate=gaode&callnative=1"


def _link(row):
    """店铺的高德链接：有高德 POI ID 就用高德自己的店铺页（短，手机上点开可跳高德 App），否则用坐标标点链接。"""
    if row["poi_id"]:
        return f"https://www.amap.com/detail/{row['poi_id']}"
    if row["lng"] is not None:
        return _map_link(row["lng"], row["lat"], row["name"])
    return ""


def _with_link(row):
    link = _link(row)
    return f"\n  高德：{link}" if link else ""


def _parse_time(text):
    """「2026-10-25 12:01:02」这种时间转时间戳；解析不了就用现在。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(text.strip(), fmt)))
        except (ValueError, AttributeError):
            continue
    return int(time.time())


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


def _clean_keyword(text):
    """去掉链接和分享套话，剩下的当作店名。"""
    keyword = URL_RE.sub(" ", text)
    keyword = re.sub(r"[【】\[\]「」·•|]|人均\s*\S*元?|大众点评|美团|高德地图|分享|推荐|快来看看|小程序|链接", " ", keyword)
    return " ".join(keyword.split())[:40]


def _local_matches(conn, keyword, city="", limit=5):
    """在店库里按店名找：先整串匹配，再用括号前的主店名匹配。给了城市就只找这个城市的店。"""
    tries = [keyword]
    base = re.split(r"[(（]", keyword)[0].strip()
    if base and base != keyword:
        tries.append(base)
    city_sql = " AND city LIKE ?" if city.strip() else ""
    for kw in tries:
        args = [f"%{kw}%"] + ([f"%{city.strip()}%"] if city.strip() else []) + [limit]
        rows = conn.execute(
            f"SELECT * FROM places WHERE name LIKE ?{city_sql} ORDER BY COALESCE(distance_m, 1e12) LIMIT ?", args
        ).fetchall()
        if rows:
            return rows
    return []


def _insert_place(conn, p, source):
    cur = conn.execute(
        """INSERT INTO places (poi_id, name, city, district, address, lng, lat, category, tel,
                               rating, cost, photo_url, source, origin, created_at)
           VALUES (:poi_id, :name, :city, :district, :address, :lng, :lat, :category, :tel,
                   :rating, :cost, :photo_url, :source, '群友分享', :created_at)""",
        {**p, "source": source, "created_at": int(time.time())},
    )
    lng0, lat0, _ = _center(conn)
    if p["lng"] is not None:
        conn.execute(
            "UPDATE places SET distance_m = ? WHERE id = ?",
            (round(_dist_km(lng0, lat0, p["lng"], p["lat"]) * 1000), cur.lastrowid),
        )
    return conn.execute("SELECT * FROM places WHERE id = ?", (cur.lastrowid,)).fetchone()


def _write_mention(conn, place_id, sender, kind, quote_text="", tags="", score=0, via="", msg_key="", said_at=""):
    """写一条口碑，返回 True；同一消息同一家店已经记过时返回 False。
    例外：先记了「分享」（save_place 自动记的），同一条消息再记评价时，把那条分享升级成评价。"""
    score = int(score or 0)
    if kind != "分享" and msg_key.strip():
        cur = conn.execute(
            """UPDATE mentions SET kind = ?, score = ?, tags = ?, quote = COALESCE(NULLIF(?, ''), quote),
                                   via = COALESCE(NULLIF(?, ''), via)
               WHERE msg_key = ? AND place_id = ? AND kind = '分享'""",
            (kind, score if 1 <= score <= 5 else None, tags.strip(), quote_text.strip(), via.strip(),
             msg_key.strip(), place_id),
        )
        if cur.rowcount:
            return True
    cur = conn.execute(
        """INSERT OR IGNORE INTO mentions (place_id, sender, said_at, kind, score, tags, quote, via, msg_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (place_id, sender.strip(), _parse_time(said_at) if said_at else int(time.time()), kind,
         score if 1 <= score <= 5 else None, tags.strip(), quote_text.strip(), via.strip(), msg_key.strip() or None,
         int(time.time())),
    )
    return cur.rowcount > 0


# ============================================================
# 工具
# ============================================================
@server.tool()
def find_place(text: str, city: str = "杭州") -> str:
    """识别一家店：可以传群友转发的高德 / 大众点评分享内容（卡片标题或链接），也可以传店名。
    先在本地店库里找，找到就返回店铺编号（#数字），可以直接用来记口碑；
    店库里没有才去高德搜，返回 POI ID，确认后用 save_place 入库。

    text: 分享内容、链接或店名，例如「老头儿油爆虾(西溪店)」
    city: 城市，默认杭州
    """
    ids = _poi_ids_from_text(text)
    with _db() as conn:
        local = []
        for i in ids[:3]:
            row = conn.execute("SELECT * FROM places WHERE poi_id = ?", (i,)).fetchone()
            if row:
                local.append(row)
        keyword = _clean_keyword(text)
        if not local and not ids and keyword:
            local = _local_matches(conn, keyword, city)
        if local:
            stats = _mention_stats(conn, [r["id"] for r in local])
            return "店库里已有：\n" + "\n".join(_place_line(r, stats.get(r["id"])) for r in local)
    candidates = [p for p in (_poi_by_id(i) for i in ids[:3]) if p]
    if not candidates:
        if not keyword:
            return "没能从内容里识别出店名，请直接告诉我店名"
        candidates = _poi_search(keyword, city)
    if not candidates:
        return "店库和高德上都没找到这家店，可以换个店名再试"
    lines = ["店库里没有，高德上的候选（用 save_place 入库）："]
    for i, p in enumerate(candidates[:5], 1):
        lines.append(
            f"{i}. {p['name']}｜{p['city']}{p['district']} {p['address']}｜"
            f"{(p['category'] or '').split(';')[-1]}｜高德评分{p['rating'] or '无'}｜POI ID：{p['poi_id']}"
        )
    return "\n".join(lines)


@server.tool()
def save_place(poi_id: str, added_by: str = "", note: str = "", source: str = "", msg_key: str = "", said_at: str = "") -> str:
    """把高德上的一家店收进店库（先用 find_place 拿到 POI ID）。已经在库里就直接返回它的编号。
    传了 added_by 时，顺便给这家店记一条「分享」口碑（note 作为原话）；
    群友明确评价了好坏的，入库后另用 record_mention 记推荐 / 踩雷。

    poi_id: 高德 POI ID（B0 开头）
    added_by: 分享人昵称，可选
    note: 分享时说的话，可选
    source: 来源，例如「高德分享」「大众点评分享」「群聊」，可选
    msg_key: 来源消息标识（旁听时系统提示里给的），可选，用于去重
    said_at: 消息时间，例如「2026-10-25 12:01:02」，可选
    """
    poi_id = poi_id.strip()
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE poi_id = ?", (poi_id,)).fetchone()
        existed = row is not None
        if not existed:
            p = _poi_by_id(poi_id)
            if not p:
                return f"高德上查不到这个 POI ID：{poi_id}"
            row = _insert_place(conn, p, source)
        if added_by.strip():
            via = "分享卡片" if "分享" in source else "自动抽取"
            _write_mention(conn, row["id"], added_by, "分享", note, "", 0, via, msg_key, said_at)
    head = "这家店已经在店库里了：\n" if existed else "已收录：\n"
    return head + _place_line(row) + _with_link(row)


@server.tool()
def record_mention(
    place_id: int,
    sender: str,
    kind: str,
    quote: str = "",
    tags: str = "",
    score: int = 0,
    via: str = "自动抽取",
    msg_key: str = "",
    said_at: str = "",
) -> str:
    """给一家已在店库里的店记一条群友口碑。同一条消息（msg_key）对同一家店只记一次。

    place_id: 店铺编号（# 后面的数字）
    sender: 谁说的（群昵称）
    kind: 推荐 / 踩雷 / 一般 / 去过 / 想去 / 分享 之一。转述别人、自己没去过的记「想去」
    quote: 原话，尽量保留原文，推荐时会引用
    tags: 要点，逗号分隔，例如「毛肚好吃,排队久,适合聚餐」
    score: 1～5 分，只有打卡指令或原话里明确给了分才填，否则填 0
    via: 自动抽取 / 打卡指令 / 分享卡片
    msg_key: 消息标识（旁听时系统提示里给的），用于去重
    said_at: 消息时间，例如「2026-10-25 12:01:02」，不填就用现在
    """
    kind = kind.strip()
    if kind not in MENTION_KINDS:
        return f"kind 只能是：{'、'.join(MENTION_KINDS)}"
    if not sender.strip():
        return "sender 不能为空"
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
        if not row:
            return f"没有编号为 {place_id} 的店，先用 find_place 找到它"
        if not _write_mention(conn, row["id"], sender, kind, quote, tags, score, via, msg_key, said_at):
            return f"这条消息已经给 {row['name']} 记过了"
        s = _mention_stats(conn, [row["id"]]).get(row["id"])
    score_text = f" {int(score)}分" if 1 <= int(score or 0) <= 5 else ""
    now = f"；现在群友评分{s['score']}（{s['raters']}人评）" if s and s["raters"] else ""
    return f"已记录：{sender.strip()} 对 #{row['id']} {row['name']}：{kind}{score_text}" + (f"（{tags}）" if tags else "") + now


@server.tool()
def recommend(keyword: str = "", near: str = "", max_km: float = 3, max_cost: float = 0, limit: int = 5) -> str:
    """按条件从店库里挑店推荐，群友评分优先：群友评 3 分及以上的按评分排前面，没人评过的按距离由近到远，
    群友评分低于 3 的排最后（高德评分几乎家家都有，分不出好坏，只作参考）。
    结果带距离、口碑摘要、群友原话和高德链接。

    keyword: 分类或口味关键词，例如「火锅」「面」「咖啡」「烧烤」，也会匹配群友口碑里的要点；不限就留空
    near: 从哪出发，留空表示云谷中心；也可以是地点名，例如「西溪印象城」
    max_km: 最远多少公里，默认 3；群友说「开车」「远点也行」可以放到 7，0 表示不限
    max_cost: 人均上限（元），0 表示不限
    limit: 推荐几家，默认 5
    """
    with _db() as conn:
        if near.strip():
            c = _locate(near)
            if not c:
                return f"高德上找不到这个地点：{near}"
            lng0, lat0, where = c[0], c[1], near.strip()
        else:
            lng0, lat0, where = _center(conn)
        sql, args = f"SELECT p.* FROM places p WHERE {OPEN} AND p.lng IS NOT NULL", []
        if keyword.strip():
            kw = f"%{keyword.strip()}%"
            sql += (" AND (p.name LIKE ? OR p.category LIKE ? OR p.note LIKE ? OR p.id IN "
                    "(SELECT place_id FROM mentions WHERE tags LIKE ? OR quote LIKE ?))")
            args += [kw] * 5
        rows = conn.execute(sql, args).fetchall()
        hits = []
        for r in rows:
            d = _dist_km(lng0, lat0, r["lng"], r["lat"])
            cost = _num(r["cost"])
            if max_km and d > max_km:
                continue
            if max_cost and cost is not None and cost > max_cost:
                continue
            hits.append((d, r))
        stats = _mention_stats(conn, [r["id"] for _, r in hits])

    def rank(item):
        # 群友评 3 分及以上的店按群友评分排最前；没人评过的按距离；群友评分低于 3 的排最后
        d, r = item
        s = stats.get(r["id"])
        if not s or not s["raters"]:
            return (1, 0, d)
        return (0 if s["score"] >= 3 else 2, -s["score"], d)

    hits.sort(key=rank)
    hits = hits[: max(1, min(int(limit), 10))]
    if not hits:
        return "店库里没有符合条件的店，可以放宽距离、人均或换个关键词"
    lines = [f"从{where}出发："]
    for d, r in hits:
        lines.append(_place_line(r, stats.get(r["id"]), d) + _with_link(r))
    return "\n".join(lines)


@server.tool()
def place_detail(place_id: int) -> str:
    """一家店的全部信息：高德资料（地址、电话、营业时间、评分、人均）、所有群友口碑、谁去过、谁想去、高德链接。

    place_id: 店铺编号
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
        if not row:
            return f"没有编号为 {place_id} 的店"
        mentions = conn.execute(
            "SELECT * FROM mentions WHERE place_id = ? ORDER BY said_at DESC, id DESC LIMIT 20", (row["id"],)
        ).fetchall()
        s = _mention_stats(conn, [row["id"]]).get(row["id"])
    lines = [_place_line(row)]
    if row["tel"]:
        lines.append(f"电话：{row['tel']}")
    if row["open_time"]:
        lines.append(f"营业时间：{row['open_time']}")
    if row["status"] == "疑似关店":
        lines.append("注意：最近两次底库更新在高德上都没搜到，可能已经关店")
    if s:
        lines.append(_stats_text(s))
        if s["went"]:
            lines.append("去过：" + "、".join(sorted(s["went"])))
        if s["want"]:
            lines.append("想去：" + "、".join(sorted(s["want"])))
    if mentions:
        lines.append("群友说过：")
        for m in mentions:
            when = time.strftime("%m-%d", time.localtime(m["said_at"])) if m["said_at"] else ""
            score = f" {m['score']}分" if m["score"] else ""
            body = m["quote"] or m["tags"] or ""
            lines.append(f"- {when} {m['sender'] or '群友'} {m['kind']}{score}" + (f"：{body}" if body else ""))
    else:
        lines.append("群里还没人评价过这家")
    if _link(row):
        lines.append("高德：" + _link(row))
    return "\n".join(lines)


@server.tool()
def who_wants(keyword: str = "", place_id: int = 0) -> str:
    """组局用：哪些人想去、去过某家店或某类店。都不传时列出想去人数最多的店。

    keyword: 店名或分类关键词，例如「火锅」，可选
    place_id: 店铺编号，可选，优先于 keyword
    """
    with _db() as conn:
        sql = f"SELECT p.* FROM places p WHERE {OPEN} AND p.id IN (SELECT place_id FROM mentions)"
        args = []
        if place_id:
            sql += " AND p.id = ?"
            args.append(int(place_id))
        elif keyword.strip():
            kw = f"%{keyword.strip()}%"
            sql += " AND (p.name LIKE ? OR p.category LIKE ?)"
            args += [kw, kw]
        rows = conn.execute(sql, args).fetchall()
        stats = _mention_stats(conn, [r["id"] for r in rows])
    rows = [r for r in rows if stats.get(r["id"]) and (stats[r["id"]]["want"] or stats[r["id"]]["went"])]
    rows.sort(key=lambda r: (len(stats[r["id"]]["want"]), len(stats[r["id"]]["went"])), reverse=True)
    if not rows:
        return "还没有人说过想去或去过符合条件的店"
    lines = []
    for r in rows[:10]:
        s = stats[r["id"]]
        parts = [f"#{r['id']} {r['name']}"]
        if s["want"]:
            parts.append("想去：" + "、".join(sorted(s["want"])))
        if s["went"]:
            parts.append("去过：" + "、".join(sorted(s["went"])))
        lines.append("｜".join(parts))
    return "\n".join(lines)


@server.tool()
def search_places(keyword: str = "", limit: int = 10) -> str:
    """在店库里按店名、分类、地址查店（不排序推荐，适合「有没有 XX 店」这类问题）。
    keyword 留空时返回最近收录的店。

    keyword: 关键词，例如「火锅」「印象城」
    limit: 最多返回几家，默认 10
    """
    sql, args = f"SELECT p.* FROM places p WHERE {OPEN}", []
    if keyword:
        sql += " AND (p.name LIKE ? OR p.category LIKE ? OR p.address LIKE ? OR p.district LIKE ? OR p.note LIKE ?)"
        args += [f"%{keyword}%"] * 5
    sql += " ORDER BY p.created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 30)))
    with _db() as conn:
        rows = conn.execute(sql, args).fetchall()
        stats = _mention_stats(conn, [r["id"] for r in rows])
    if not rows:
        return "店库里还没有符合条件的店"
    return "\n".join(_place_line(r, stats.get(r["id"])) for r in rows)


@server.tool()
def nearby_places(location: str = "", radius_km: float = 1.5, limit: int = 10) -> str:
    """查某个地点附近的店，按距离从近到远。

    location: 地点或地址，例如「西溪印象城」，或群友发的微信位置里的地点名和地址；留空表示云谷中心
    radius_km: 半径（公里），默认 1.5
    limit: 最多返回几家，默认 10
    """
    with _db() as conn:
        if location.strip():
            center = _locate(location)
            if not center:
                return f"高德上找不到这个地点：{location}"
            lng0, lat0, where = center[0], center[1], location.strip()
        else:
            lng0, lat0, where = _center(conn)
        rows = conn.execute(f"SELECT p.* FROM places p WHERE p.lng IS NOT NULL AND p.lat IS NOT NULL AND {OPEN}").fetchall()
        hits = sorted(((_dist_km(lng0, lat0, r["lng"], r["lat"]), r) for r in rows), key=lambda x: x[0])
        hits = [(d, r) for d, r in hits if d <= radius_km][: max(1, min(int(limit), 30))]
        stats = _mention_stats(conn, [r["id"] for _, r in hits])
    if not hits:
        return f"{where} 附近 {radius_km} 公里内店库里还没有店"
    return "\n".join(_place_line(r, stats.get(r["id"]), d) for d, r in hits)


@server.tool()
def place_map_link(place_id: int) -> str:
    """生成某家店的高德地图链接（手机点开唤起高德 App，可导航）。

    place_id: 店铺编号（查询结果里 # 后面的数字）
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
    if not row:
        return f"没有编号为 {place_id} 的店"
    if not _link(row):
        return f"{row['name']} 没有坐标，无法生成地图链接"
    return f"{row['name']}：{_link(row)}"


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
    """生成某家店的图片：高德静态地图（标出店铺位置），以及高德上的店铺照片（如果有）。
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
    """从店库里删除一家店和它的口碑。只在群友明确要求删除时调用。

    place_id: 店铺编号
    """
    with _db() as conn:
        row = conn.execute("SELECT * FROM places WHERE id = ?", (int(place_id),)).fetchone()
        if not row:
            return f"没有编号为 {place_id} 的店"
        conn.execute("DELETE FROM mentions WHERE place_id = ?", (int(place_id),))
        conn.execute("DELETE FROM places WHERE id = ?", (int(place_id),))
    return f"已删除：{row['name']}"


if __name__ == "__main__":
    server.run()
