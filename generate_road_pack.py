#!/usr/bin/env python3
"""
OSM Overpass API から名前付き道路データを取得し、
road1 アプリ用の道路パック JSON を生成するスクリプト。

Usage:
    python3 scripts/generate_road_pack.py

出力:
    manifest.json
    packs/tokyo_major.json
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field

# ============================================================
# 設定
# ============================================================

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BBOX = "35.5,139.4,35.85,139.95"  # 東京都周辺
SEGMENT_LENGTH_METERS = 50.0       # セグメント1本の目標長（m）

# 取得対象の道路定義
ROADS = [
    {
        "id": "koshu_kaido",
        "name": "甲州街道",
        "query_name": "甲州街道",
        "query_extra": "",
    },
    {
        "id": "yasukuni_dori",
        "name": "靖国通り",
        "query_name": "靖国通り",
        "query_extra": "",
    },
    {
        "id": "meiji_dori",
        "name": "明治通り",
        "query_name": "明治通り",
        "query_extra": "",
    },
    {
        "id": "yamate_dori",
        "name": "山手通り",
        "query_name": "山手通り",
        "query_extra": "",
    },
    {
        "id": "kannana_dori",
        "name": "環七通り",
        "query_name": "環七通り",
        "query_extra": "",
    },
]

PACK_ID = "tokyo_major"
PACK_NAME = "東京都主要道路"
PACK_DESCRIPTION = "甲州街道・靖国通り・明治通り・山手通り・環七通りのデータ"
PACK_CATEGORY = "都市部主要道路"
PACK_REGION = "東京都"
DATA_VERSION = "20260424"

# ============================================================
# 地理計算（純粋関数）
# ============================================================

EARTH_RADIUS = 6_371_000.0  # m


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離（m）をHaversine公式で計算"""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return EARTH_RADIUS * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def total_length(points: list[tuple[float, float]]) -> float:
    """点列の総距離（m）"""
    return sum(
        haversine(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )


# ============================================================
# Overpass API 呼び出し
# ============================================================


def fetch_road_ways(road_def: dict) -> list[dict]:
    """Overpass API から指定道路の way 要素を取得"""
    query = f"""
[out:json][timeout:60][bbox:{BBOX}];
way["name"="{road_def['query_name']}"]{road_def['query_extra']};
out geom;
"""
    data = urllib.parse.urlencode({"data": query.strip()}).encode("utf-8")
    req = urllib.request.Request(OVERPASS_URL, data=data, method="POST")
    req.add_header("User-Agent", "road1-pack-generator/1.0")

    print(f"  Fetching {road_def['name']}...", end="", flush=True)
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    elements = result.get("elements", [])
    print(f" {len(elements)} ways")
    return elements


# ============================================================
# Way からセグメント用の点列を抽出
# ============================================================


def extract_way_point_lists(ways: list[dict]) -> list[list[tuple[float, float]]]:
    """
    各 way のジオメトリを点列のリストとして返す。
    名前付き道路はOSMで片側車線ごとに別wayになるため、
    1本に結合せず、各wayを独立した点列として保持する。
    踏破判定は近傍マッチング（中点間距離）なので結合不要。
    ただし重複セグメント（片側車線）は後段で除去する。
    """
    result = []
    for way in ways:
        geom = way.get("geometry", [])
        if len(geom) < 2:
            continue
        pts = [(g["lat"], g["lon"]) for g in geom]
        result.append(pts)
    return result


# ============================================================
# セグメント分割
# ============================================================


def split_points_into_segments(
    points: list[tuple[float, float]], target_length: float, start_index: int
) -> list[dict]:
    """
    1本の点列を target_length（m）ごとのセグメントに分割する。
    """
    if len(points) < 2:
        return []

    segments = []
    current_points = [points[0]]
    current_length = 0.0
    seg_index = start_index

    for i in range(1, len(points)):
        d = haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        current_length += d
        current_points.append(points[i])

        if current_length >= target_length:
            seg = {
                "index": seg_index,
                "points": [[p[0], p[1]] for p in current_points],
                "lengthMeters": round(current_length, 1),
            }
            segments.append(seg)
            seg_index += 1
            current_points = [points[i]]
            current_length = 0.0

    # 残り
    if len(current_points) >= 2 and current_length >= 10.0:
        seg = {
            "index": seg_index,
            "points": [[p[0], p[1]] for p in current_points],
            "lengthMeters": round(current_length, 1),
        }
        segments.append(seg)

    return segments


def segment_midpoint(seg: dict) -> tuple[float, float]:
    """セグメントの中点を返す"""
    pts = seg["points"]
    mid_idx = len(pts) // 2
    return (pts[mid_idx][0], pts[mid_idx][1])


def deduplicate_segments(segments: list[dict], threshold: float = 25.0) -> list[dict]:
    """
    中点が閾値以内のセグメントを重複とみなして除去する。
    片側車線による重複wayを排除するため。
    """
    result = []
    used_midpoints: list[tuple[float, float]] = []

    for seg in segments:
        mid = segment_midpoint(seg)
        is_dup = any(
            haversine(mid[0], mid[1], um[0], um[1]) < threshold
            for um in used_midpoints
        )
        if not is_dup:
            result.append(seg)
            used_midpoints.append(mid)

    return result


def build_segments_from_ways(
    ways: list[dict], target_length: float
) -> list[dict]:
    """
    複数 way から全セグメントを生成し、重複を除去する。
    """
    way_points = extract_way_point_lists(ways)
    all_segments = []
    idx = 0

    for pts in way_points:
        segs = split_points_into_segments(pts, target_length, idx)
        all_segments.extend(segs)
        idx += len(segs)

    # 重複除去
    deduped = deduplicate_segments(all_segments)

    # indexを振り直す
    for i, seg in enumerate(deduped):
        seg["index"] = i

    return deduped


# ============================================================
# メイン処理
# ============================================================


def main():
    output_dir = os.path.dirname(__file__) or "."
    packs_dir = os.path.join(output_dir, "packs")
    os.makedirs(packs_dir, exist_ok=True)

    roads_data = []

    print(f"=== 道路パック生成: {PACK_NAME} ===\n")

    for road_def in ROADS:
        ways = fetch_road_ways(road_def)
        if not ways:
            print(f"  WARNING: {road_def['name']} のデータが見つかりません")
            continue

        # 全 way からセグメント生成 + 重複除去
        segments = build_segments_from_ways(ways, SEGMENT_LENGTH_METERS)
        road_total = sum(s["lengthMeters"] for s in segments)
        print(f"  → {len(segments)} segments (deduped), total {road_total:.0f}m")

        road = {
            "id": road_def["id"],
            "name": road_def["name"],
            "totalLengthMeters": round(road_total, 1),
            "segments": segments,
        }
        roads_data.append(road)
        print()

        # Overpass API のレート制限を避ける
        time.sleep(2)

    # パック JSON 生成
    pack = {
        "packId": PACK_ID,
        "dataVersion": DATA_VERSION,
        "roads": roads_data,
    }

    pack_path = os.path.join(packs_dir, f"{PACK_ID}.json")
    with open(pack_path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2)

    pack_size = os.path.getsize(pack_path)
    print(f"Pack saved: {pack_path} ({pack_size:,} bytes)")

    # マニフェスト JSON 生成
    import hashlib

    with open(pack_path, "rb") as f:
        checksum = f"sha256:{hashlib.sha256(f.read()).hexdigest()}"

    manifest = {
        "version": 1,
        "packs": [
            {
                "id": PACK_ID,
                "name": PACK_NAME,
                "description": PACK_DESCRIPTION,
                "category": PACK_CATEGORY,
                "region": PACK_REGION,
                "dataVersion": DATA_VERSION,
                "fileSizeBytes": pack_size,
                "checksum": checksum,
            }
        ],
    }

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Manifest saved: {manifest_path}")
    print(f"\nDone! {len(roads_data)} roads, "
          f"{sum(len(r['segments']) for r in roads_data)} total segments")


if __name__ == "__main__":
    main()
