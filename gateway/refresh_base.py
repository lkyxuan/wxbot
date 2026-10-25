# -*- coding: utf-8 -*-
"""
云谷美食底库：初始化与每周更新
==============================

以云谷中心为圆心，用高德「多边形搜索」按餐饮大类（050000）把半径内的店拉进美食地图，
和 mcp_servers/food_server.py 共用 gateway/data/food.db 的 places 表。不经过大模型。

- 新店入库（origin = 底库扫描）；已有的店只更新高德字段（评分、人均、营业时间、电话等），
  群友写的备注、推荐人不动。
- 半径内这次没搜到的底库店 miss_count +1，连续两次搜不到标为「疑似关店」，不删除，口碑保留。
  这次有格子没拉完（配额用完、接口报错、调用次数到上限）时跳过这一步，免得误判。
- 高德单次查询能翻到的结果有上限，所以把范围切成格子分别搜；某格结果达到上限就再四分。

用法（在 gateway 目录下）：
    set AMAP_KEY=你的 Key
    python refresh_base.py                        # 圆心默认搜「云谷中心」，半径 3000 米
    python refresh_base.py --center 120.02,30.28  # 指定圆心（高德坐标，经度,纬度）
    python refresh_base.py --radius 5000 --dry-run
圆心第一次确定后存进数据库，之后每周都用同一个点；要换就再传 --center 或 --center-name。
每周一凌晨用 Windows 计划任务跑 refresh_base.bat。
"""

import argparse
import math
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_servers"))
from food_server import _db, _export_map_data  # noqa: E402

API_BASE = os.environ.get("AMAP_API_BASE", "https://restapi.amap.com")
TIMEOUT = 15
FOOD_TYPES = "050000"  # 高德 POI 分类：餐饮服务（含中餐、快餐、咖啡、茶饮、甜品等）
PAGE_SIZE = 25         # 高德每页最多 25 条
CELL_CAP = 200         # 一个格子拉到这么多条就认为可能被截断，四分后重搜
MAX_DEPTH = 4
QPS_CODES = {"10019", "10020", "10021", "10022", "10029"}  # 并发 / QPS 超限，稍等重试
QUOTA_CODES = {"10003", "10044", "10045"}                  # 日 / 月配额用完


class QuotaExceeded(Exception):
    pass


class Amap:
    def __init__(self, key, qps, max_calls):
        self.key, self.interval, self.max_calls = key, 1.0 / qps, max_calls
        self.calls, self._last = 0, 0.0

    def get(self, path, params):
        for attempt in range(5):
            if self.calls >= self.max_calls:
                raise QuotaExceeded(f"本次调用次数已到上限 {self.max_calls}（--max-calls）")
            wait = self._last + self.interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            data = requests.get(f"{API_BASE}{path}", params={**params, "key": self.key}, timeout=TIMEOUT).json()
            if str(data.get("status")) == "1":
                return data
            code = str(data.get("infocode"))
            if code in QUOTA_CODES:
                raise QuotaExceeded(f"高德配额用完：{data.get('info')}（{code}）")
            if code not in QPS_CODES:
                raise RuntimeError(f"高德接口报错：{data.get('info')}（{code}）")
            time.sleep(2 ** attempt)
        raise RuntimeError("高德接口持续限流，稍后再试")


# ============================================================
# 坐标
# ============================================================
def dist_m(lng1, lat1, lng2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def grid(center, radius, cell):
    """把圆的外接正方形切成边长约 cell 米的格子，只留和圆相交的。格子是 (minlng, minlat, maxlng, maxlat)。"""
    lng0, lat0 = center
    dlat = 1 / 111320
    dlng = 1 / (111320 * math.cos(math.radians(lat0)))
    n = max(1, math.ceil(2 * radius / cell))
    step = 2 * radius / n
    cells = []
    for i in range(n):
        for j in range(n):
            x1, y1 = -radius + i * step, -radius + j * step
            x2, y2 = x1 + step, y1 + step
            # 格子上离圆心最近的点在圆外，就整格跳过
            nx, ny = min(max(0, x1), x2), min(max(0, y1), y2)
            if nx * nx + ny * ny > radius * radius:
                continue
            cells.append((lng0 + x1 * dlng, lat0 + y1 * dlat, lng0 + x2 * dlng, lat0 + y2 * dlat))
    return cells


def split4(rect):
    x1, y1, x2, y2 = rect
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    return [(x1, y1, mx, my), (mx, y1, x2, my), (x1, my, mx, y2), (mx, my, x2, y2)]


# ============================================================
# 高德
# ============================================================
def _str(v):
    # 高德空字段会返回 [] 而不是空字符串
    return v if isinstance(v, str) else ""


def parse_v5(p):
    lng, lat = (_str(p.get("location")) or ",").split(",")[:2]
    biz = p.get("business") or {}
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
        "tel": _str(biz.get("tel")),
        "rating": _str(biz.get("rating")),
        "cost": _str(biz.get("cost")),
        "open_time": _str(biz.get("opentime_week")) or _str(biz.get("opentime_today")),
        "photo_url": _str(photos[0].get("url")) if photos else "",
    }


def search_cell(api, rect, depth, log):
    """拉一个格子里的所有餐饮店；结果到上限就四分递归。返回 (pois, 是否完整)。"""
    x1, y1, x2, y2 = rect
    params = {
        "polygon": f"{x1:.6f},{y2:.6f}|{x2:.6f},{y1:.6f}",  # 矩形：左上|右下
        "types": FOOD_TYPES,
        "page_size": PAGE_SIZE,
        "show_fields": "business,photos",
    }
    pois, page = [], 1
    while True:
        items = api.get("/v5/place/polygon", {**params, "page_num": page}).get("pois") or []
        pois += items
        if len(items) < PAGE_SIZE:
            return pois, True
        if len(pois) >= CELL_CAP:
            if depth < MAX_DEPTH:
                out, complete = [], True
                for sub in split4(rect):
                    sub_pois, ok = search_cell(api, sub, depth + 1, log)
                    out += sub_pois
                    complete = complete and ok
                return out, complete
            log(f"  格子 {params['polygon']} 已切到最小仍有 {len(pois)}+ 家，可能不全")
            return pois, False
        page += 1


def resolve_center(api, conn, args):
    if args.center:
        lng, lat = map(float, args.center.split(","))
        return lng, lat, args.center_name or "手动指定"
    if not args.center_name:
        row = conn.execute("SELECT value FROM meta WHERE key = 'base_center'").fetchone()
        if row:
            lng, lat, name = row["value"].split(",", 2)
            return float(lng), float(lat), name
    name = args.center_name or "云谷中心"
    pois = api.get("/v5/place/text", {"keywords": name, "region": "杭州", "city_limit": "true", "page_size": 5}).get("pois") or []
    for p in pois:
        if _str(p.get("location")):
            lng, lat = map(float, p["location"].split(","))
            return lng, lat, f"{_str(p.get('name'))}（{_str(p.get('address'))}）"
    raise RuntimeError(f"高德上找不到圆心「{name}」，请用 --center 经度,纬度 指定")


# ============================================================
# 入库
# ============================================================
AMAP_FIELDS = ("name", "city", "district", "address", "lng", "lat", "category", "tel", "rating", "cost", "open_time", "photo_url")


def upsert(conn, p, now):
    """新店插入；老店用高德的新值覆盖（新值为空时保留旧值），不碰备注、推荐人、来源。返回是否新店。"""
    row = conn.execute("SELECT id FROM places WHERE poi_id = ?", (p["poi_id"],)).fetchone()
    if row:
        sets = ", ".join(f"{f} = COALESCE(NULLIF(:{f}, ''), {f})" for f in AMAP_FIELDS)
        conn.execute(
            f"UPDATE places SET {sets}, status = '营业中', miss_count = 0, refreshed_at = :now WHERE id = :id",
            {**p, "now": now, "id": row["id"]},
        )
        return False
    conn.execute(
        f"""INSERT INTO places (poi_id, {', '.join(AMAP_FIELDS)}, source, origin, status, miss_count, refreshed_at, created_at)
            VALUES (:poi_id, {', '.join(':' + f for f in AMAP_FIELDS)}, '高德底库', '底库扫描', '营业中', 0, :now, :now)""",
        {**p, "now": now},
    )
    return True


def main():
    ap = argparse.ArgumentParser(description="云谷美食底库初始化 / 每周更新")
    ap.add_argument("--center", help="圆心坐标「经度,纬度」（高德 GCJ-02）")
    ap.add_argument("--center-name", help="圆心地名，用高德在杭州搜，默认「云谷中心」")
    ap.add_argument("--radius", type=int, default=3000, help="半径（米），默认 3000")
    ap.add_argument("--cell", type=int, default=1000, help="初始格子边长（米），默认 1000")
    ap.add_argument("--qps", type=float, default=2, help="每秒最多请求几次，默认 2")
    ap.add_argument("--max-calls", type=int, default=1500, help="本次最多调用几次高德，保护月配额，默认 1500")
    ap.add_argument("--dry-run", action="store_true", help="只搜不写库")
    args = ap.parse_args()

    key = os.environ.get("AMAP_KEY", "").strip()
    if not key:
        sys.exit("没有配置高德 Key（环境变量 AMAP_KEY）")
    api = Amap(key, args.qps, args.max_calls)
    log = lambda s: print(s, flush=True)  # noqa: E731

    conn = _db()
    lng0, lat0, center_name = resolve_center(api, conn, args)
    log(f"圆心：{center_name} {lng0:.6f},{lat0:.6f}，半径 {args.radius} 米")

    cells = grid((lng0, lat0), args.radius, args.cell)
    found, complete = {}, True
    for i, rect in enumerate(cells, 1):
        try:
            pois, ok = search_cell(api, rect, 0, log)
        except (QuotaExceeded, RuntimeError, requests.RequestException, ValueError) as e:
            log(f"停止：{e}。已拉到的店照常入库，下次再补全")
            complete = False
            break
        complete = complete and ok
        for raw in pois:
            p = parse_v5(raw)
            if p["poi_id"] and p["lng"] is not None and dist_m(lng0, lat0, p["lng"], p["lat"]) <= args.radius:
                found[p["poi_id"]] = p
        log(f"[{i}/{len(cells)}] 本格 {len(pois)} 家，累计 {len(found)} 家，已调用高德 {api.calls} 次")

    if args.dry_run:
        log(f"试运行结束：半径内 {len(found)} 家，调用高德 {api.calls} 次，未写库")
        return

    now = int(time.time())
    new = flagged = missed = 0
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('base_center', ?)",
            (f"{lng0},{lat0},{center_name}",),
        )
        for p in found.values():
            new += upsert(conn, p, now)
        if complete:
            # 半径内、这次没搜到的底库店记一次缺席；连续两次就标疑似关店
            for r in conn.execute(
                "SELECT id, poi_id, lng, lat, miss_count FROM places WHERE origin = '底库扫描' AND lng IS NOT NULL"
            ).fetchall():
                if r["poi_id"] in found or dist_m(lng0, lat0, r["lng"], r["lat"]) > args.radius:
                    continue
                misses = (r["miss_count"] or 0) + 1
                status = "疑似关店" if misses >= 2 else None
                conn.execute(
                    "UPDATE places SET miss_count = ?, status = COALESCE(?, status) WHERE id = ?",
                    (misses, status, r["id"]),
                )
                missed += 1
                flagged += bool(status)
        # 所有有坐标的店（包括群友分享的）都重算到圆心的距离
        for r in conn.execute("SELECT id, lng, lat FROM places WHERE lng IS NOT NULL AND lat IS NOT NULL").fetchall():
            conn.execute("UPDATE places SET distance_m = ? WHERE id = ?", (round(dist_m(lng0, lat0, r["lng"], r["lat"])), r["id"]))
    conn.close()
    _export_map_data()

    log(
        f"完成：半径内 {len(found)} 家，新增 {new} 家，更新 {len(found) - new} 家；"
        + (f"本次没搜到 {missed} 家，其中 {flagged} 家标为疑似关店；" if complete else "本次没拉完，跳过关店判断；")
        + f"调用高德 {api.calls} 次"
    )


if __name__ == "__main__":
    main()
