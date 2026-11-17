"""覆盖核验：在单个 SQLite 快照内核算目标矩形的覆盖与缺口。

规则：
- 仅「现役修订 + 带足迹」的指定瓦片贡献覆盖；
  missing（成果/修订未提交）、superseded（已非现役）、no_footprint（无足迹）分别列出。
- 足迹与目标求交后只计目标内实际覆盖；相交、重叠只计一次，超出目标的部分丢弃。
- 全部坐标/面积使用 Python 任意精度整数，坐标跨度再大也精确，无浮点误差。
- 缺口以矩形差集法生成，天然互不重叠。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import database as db

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)，右下不含


def intersect(a: Rect, b: Rect) -> Optional[Rect]:
    """两个矩形的交集；不相交（含仅边界相接）时返回 None。"""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if left < right and top < bottom:
        return left, top, right, bottom
    return None


def subtract_rect(rect: Rect, cutter: Rect) -> list[Rect]:
    """从 rect 中挖去 cutter，返回至多 4 个互不重叠的剩余矩形。

    切割顺序固定为左、右、上、下；每个切出的残片不再被 cutter 覆盖。
    """
    clip = intersect(rect, cutter)
    if clip is None:
        return [rect]
    left, top, right, bottom = rect
    cleft, ctop, cright, cbottom = clip
    pieces: list[Rect] = []
    if cleft > left:  # 左条带
        pieces.append((left, top, cleft, bottom))
    if cright < right:  # 右条带
        pieces.append((cright, top, right, bottom))
    # 中间横带的上、下部分（左右已被条带覆盖）
    if ctop > top:
        pieces.append((cleft, top, cright, ctop))
    if cbottom < bottom:
        pieces.append((cleft, cbottom, cright, bottom))
    return pieces


def compute_gaps(target: Rect, footprints: list[Rect]) -> list[Rect]:
    """返回 target 内未被任一足迹覆盖的互不重叠缺口矩形。

    逐个用裁剪后的足迹对当前残片集合做差集；重叠区域随第一次覆盖即消失，
    因此多瓦片重叠不会产生重复计数，输出矩形之间也互不重叠。
    """
    remaining: list[Rect] = [target]
    for fp in footprints:
        clip = intersect(target, fp)
        if clip is None:
            continue
        next_remaining: list[Rect] = []
        for rect in remaining:
            next_remaining.extend(subtract_rect(rect, clip))
        remaining = next_remaining
    # 确定性输出：先上后下、先左后右
    remaining.sort(key=lambda r: (r[1], r[0], r[2], r[3]))
    return remaining


def rect_area(rect: Rect) -> int:
    return (rect[2] - rect[0]) * (rect[3] - rect[1])


def _row_footprint(row: sqlite3.Row) -> Optional[Rect]:
    if row["footprint_left"] is None:
        return None
    return (
        row["footprint_left"],
        row["footprint_top"],
        row["footprint_right"],
        row["footprint_bottom"],
    )


def verify_coverage(
    conn: sqlite3.Connection,
    target: Rect,
    tile_refs: list[tuple[str, int]],
) -> dict[str, Any]:
    """在调用方提供的快照事务内完成一次覆盖核验。

    所有修订状态与足迹均来自同一已提交快照，不会混用并发提交前后的状态。
    """
    # 请求内重复指定的（成果号, 修订号）按一个处理，保持首次出现顺序
    seen_refs: set[tuple[str, int]] = set()
    ordered_refs: list[tuple[str, int]] = []
    for ref in tile_refs:
        if ref not in seen_refs:
            seen_refs.add(ref)
            ordered_refs.append(ref)

    contributing: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    no_footprint: list[dict[str, Any]] = []
    clipped_footprints: list[Rect] = []

    for product_id, revision in ordered_refs:
        row = db.get_revision(conn, product_id, revision)
        label = f"{product_id}@{revision}"
        if row is None:
            missing.append({
                "product_id": product_id,
                "revision": revision,
                "reason": "missing",
                "detail": f"成果 {label} 从未提交（成果号或修订号不存在）",
            })
            continue

        active_row = db.get_active_revision(conn, product_id)
        if active_row is None or active_row["revision"] != revision:
            active_rev = active_row["revision"] if active_row is not None else None
            superseded.append({
                "product_id": product_id,
                "revision": revision,
                "reason": "superseded",
                "detail": (
                    f"修订 {label} 已非现役，现役修订为 {product_id}@{active_rev}，"
                    "旧修订不再贡献覆盖"
                ),
            })
            continue

        footprint = _row_footprint(row)
        if footprint is None:
            no_footprint.append({
                "product_id": product_id,
                "revision": revision,
                "reason": "no_footprint",
                "detail": f"现役修订 {label} 的清单未带像素足迹，无法参与覆盖核算",
            })
            continue

        clip = intersect(target, footprint)
        if clip is None:
            # 足迹完全落在目标外：仍属于有效覆盖者，但目标内实际覆盖为 0
            contributing.append({
                "product_id": product_id,
                "revision": revision,
                "footprint": _footprint_dict(footprint),
                "clipped_footprint": None,
                "applied_area": 0,
                "is_active": True,
            })
            continue

        clipped_footprints.append(clip)
        contributing.append({
            "product_id": product_id,
            "revision": revision,
            "footprint": _footprint_dict(footprint),
            "clipped_footprint": _footprint_dict(clip),
            "applied_area": rect_area(clip),
            "is_active": True,
        })

    gap_rects = compute_gaps(target, clipped_footprints)
    target_area = rect_area(target)
    gaps = [
        {
            "left": g[0],
            "top": g[1],
            "right": g[2],
            "bottom": g[3],
            "area": rect_area(g),
        }
        for g in gap_rects
    ]
    gap_area = sum(g["area"] for g in gaps)

    return {
        "target": _footprint_dict(target),
        "target_area": target_area,
        "covered_area": target_area - gap_area,
        "gap_area": gap_area,
        "gaps": gaps,
        "contributing_tiles": contributing,
        "missing_tiles": missing,
        "superseded_tiles": superseded,
        "no_footprint_tiles": no_footprint,
    }


def _footprint_dict(rect: Optional[Rect]) -> Optional[dict[str, int]]:
    if rect is None:
        return None
    return {
        "left": rect[0],
        "top": rect[1],
        "right": rect[2],
        "bottom": rect[3],
    }
