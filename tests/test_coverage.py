"""覆盖核验（缺口矩形）端到端自测。

运行：pytest -q（需在项目根目录，且依赖已安装）
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.main import create_app


def _tile(product_id, revision, summary="原始瓦片", footprint=None):
    item = {"product_id": product_id, "revision": revision, "summary": summary}
    if footprint is not None:
        item["footprint"] = {
            "left": footprint[0],
            "top": footprint[1],
            "right": footprint[2],
            "bottom": footprint[3],
        }
    return item


def _gaps(target, tiles):
    return {
        "target": {
            "left": target[0], "top": target[1],
            "right": target[2], "bottom": target[3],
        },
        "tiles": [
            {"product_id": pid, "revision": rev} for pid, rev in tiles
        ],
    }


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c


def _post(client, items):
    return client.post("/api/batches", json={"items": items})


# ---------------------------------------------------------------------------
# 1. 足迹登记与向后兼容
# ---------------------------------------------------------------------------

def test_footprint_is_persisted_and_returned_in_history(client):
    r = _post(client, [_tile("R-1", 1, footprint=(10, 20, 110, 220))])
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    hist = client.get("/api/products/R-1/revisions").json()
    assert hist["revisions"][0]["footprint"] == {
        "left": 10, "top": 20, "right": 110, "bottom": 220
    }


def test_legacy_manifest_without_footprint_still_submits(client):
    # 旧清单不带足迹：正常提交、可追溯
    r = _post(client, [{"product_id": "R-0", "revision": 1, "summary": "旧清单瓦片"}])
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    hist = client.get("/api/products/R-0/revisions").json()
    assert hist["revisions"][0]["footprint"] is None
    # 谱系功能不受影响
    assert client.get("/api/products/R-0/status").json()["deliverable"] is True


def test_invalid_footprint_rejected_by_validation(client):
    # right <= left（右下边界不含，必须严格大于）
    r = _post(client, [_tile("R-1", 1, footprint=(10, 10, 10, 20))])
    assert r.status_code == 422
    r = _post(client, [_tile("R-1", 1, footprint=(10, 10, 20, 5))])
    assert r.status_code == 422


def test_same_key_different_footprint_rejects_whole_batch(client):
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 10, 10))])
    # 同号同修订但足迹不同 → 整批拒收，零写入
    r = _post(client, [
        _tile("R-1", 1, footprint=(0, 0, 20, 20)),
        _tile("R-NEW", 1, footprint=(0, 0, 5, 5)),
    ])
    body = r.json()
    assert body["status"] == "rejected" and body["inserted_count"] == 0
    assert body["conflicts"][0]["product_id"] == "R-1"
    assert client.get("/api/products/R-NEW/status").status_code == 404
    # 原足迹保持不变
    hist = client.get("/api/products/R-1/revisions").json()
    assert hist["revisions"][0]["footprint"]["right"] == 10


def test_identical_resubmit_with_footprint_is_duplicate(client):
    payload = [_tile("R-1", 1, footprint=(0, 0, 10, 10))]
    first = _post(client, payload).json()
    second = _post(client, payload).json()
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"


# ---------------------------------------------------------------------------
# 2. 基础缺口核算
# ---------------------------------------------------------------------------

def test_full_coverage_has_no_gaps(client):
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 100, 100))])
    r = client.post("/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)]))
    body = r.json()
    assert r.status_code == 200
    assert body["gaps"] == []
    assert body["gap_area"] == 0
    assert body["covered_area"] == 10000 and body["target_area"] == 10000
    assert len(body["contributing_tiles"]) == 1
    assert body["missing_tiles"] == body["superseded_tiles"] == body["no_footprint_tiles"] == []


def test_no_tiles_whole_target_is_one_gap(client):
    r = client.post("/api/coverage/gaps", json=_gaps((5, 6, 15, 26), []))
    body = r.json()
    assert len(body["gaps"]) == 1
    assert body["gaps"][0] == {"left": 5, "top": 6, "right": 15, "bottom": 26, "area": 200}
    assert body["gap_area"] == 200 and body["covered_area"] == 0


def test_partial_coverage_gap_rectangles_are_disjoint(client):
    # 目标 0..100 见方，瓦片覆盖左半
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 40, 100))])
    body = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)])
    ).json()
    assert body["gap_area"] == 6000 and body["covered_area"] == 4000
    assert body["gaps"] == [{"left": 40, "top": 0, "right": 100, "bottom": 100, "area": 6000}]


def test_gap_rectangles_are_mutually_non_overlapping(client):
    # 十字形覆盖，残片应被切成 4 个互不重叠的角落矩形
    _post(client, [
        _tile("H", 1, footprint=(0, 40, 100, 60)),   # 横条
        _tile("V", 1, footprint=(40, 0, 60, 100)),   # 竖条
    ])
    body = client.post(
        "/api/coverage/gaps",
        json=_gaps((0, 0, 100, 100), [("H", 1), ("V", 1)]),
    ).json()
    # 覆盖面积 = 横条 2000 + 竖条 2000 - 交集 400 = 3600
    assert body["covered_area"] == 3600
    assert body["gap_area"] == 6400
    assert sum(g["area"] for g in body["gaps"]) == body["gap_area"]
    # 两两不相交
    rects = [(g["left"], g["top"], g["right"], g["bottom"]) for g in body["gaps"]]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert max(a[0], b[0]) >= min(a[2], b[2]) or max(a[1], b[1]) >= min(a[3], b[3])


def test_overlapping_footprints_counted_once(client):
    # 两个大面积重叠的瓦片，覆盖不得重复计数
    _post(client, [
        _tile("R-1", 1, footprint=(0, 0, 80, 80)),
        _tile("R-2", 1, footprint=(20, 20, 100, 100)),
    ])
    body = client.post(
        "/api/coverage/gaps",
        json=_gaps((0, 0, 100, 100), [("R-1", 1), ("R-2", 1)]),
    ).json()
    # 并集面积 = 6400 + 6400 - 3600 = 9200；缺口 = 两个 L 形残片合计 800
    assert body["covered_area"] == 9200
    assert body["gap_area"] == 800


def test_footprint_outside_target_is_clipped(client):
    # 足迹远大于目标且偏移：只计目标内部分
    _post(client, [_tile("R-1", 1, footprint=(-50, -60, 50, 60))])
    body = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)])
    ).json()
    assert body["covered_area"] == 50 * 60
    assert body["gap_area"] == 10000 - 3000
    contrib = body["contributing_tiles"][0]
    assert contrib["clipped_footprint"] == {"left": 0, "top": 0, "right": 50, "bottom": 60}
    assert contrib["applied_area"] == 3000


def test_footprint_completely_outside_target_contributes_nothing(client):
    _post(client, [_tile("R-1", 1, footprint=(200, 200, 300, 300))])
    body = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)])
    ).json()
    assert body["covered_area"] == 0
    assert body["gap_area"] == 10000
    assert body["contributing_tiles"][0]["applied_area"] == 0
    assert body["contributing_tiles"][0]["clipped_footprint"] is None


def test_negative_coordinates_supported(client):
    _post(client, [_tile("R-1", 1, footprint=(-100, -100, 0, 0))])
    body = client.post(
        "/api/coverage/gaps", json=_gaps((-100, -100, 0, 0), [("R-1", 1)])
    ).json()
    assert body["gap_area"] == 0 and body["covered_area"] == 10000


# ---------------------------------------------------------------------------
# 3. 问题瓦片分类
# ---------------------------------------------------------------------------

def test_missing_superseded_and_no_footprint_tiles_listed(client):
    _post(client, [
        _tile("R-OLD", 1, footprint=(0, 0, 10, 10)),
        _tile("R-OLD", 2, footprint=(0, 0, 10, 10)),   # rev1 退役
        _tile("R-NFP", 1),                              # 现役但无足迹
        _tile("R-OK", 1, footprint=(0, 0, 10, 10)),
    ])
    body = client.post("/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [
        ("GHOST", 1),       # 从未提交
        ("R-OLD", 1),       # 已非现役
        ("R-NFP", 1),       # 无足迹
        ("R-OK", 1),        # 正常贡献
    ])).json()

    assert [(t["product_id"], t["reason"]) for t in body["missing_tiles"]] == [
        ("GHOST", "missing")
    ]
    assert [(t["product_id"], t["reason"]) for t in body["superseded_tiles"]] == [
        ("R-OLD", "superseded")
    ]
    assert [(t["product_id"], t["reason"]) for t in body["no_footprint_tiles"]] == [
        ("R-NFP", "no_footprint")
    ]
    assert [t["product_id"] for t in body["contributing_tiles"]] == ["R-OK"]
    # 退役修订不贡献覆盖，目标内仅 100 像素被覆盖
    assert body["covered_area"] == 100
    assert body["gap_area"] == 9900


def test_superseded_revision_still_traceable_but_not_covering(client):
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 50, 100))])
    _post(client, [_tile("R-1", 2, footprint=(50, 0, 100, 100))])
    # 旧修订仍可追溯
    old = client.get("/api/products/R-1/status", params={"revision": 1}).json()
    assert old["is_active"] is False
    # 指定旧修订核验：被标记 superseded，不贡献任何覆盖
    body = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)])
    ).json()
    assert body["superseded_tiles"][0]["revision"] == 1
    assert body["covered_area"] == 0 and body["gap_area"] == 10000
    # 指定现役修订则覆盖右半
    body2 = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 2)])
    ).json()
    assert body2["covered_area"] == 5000 and body2["gap_area"] == 5000


def test_new_revision_supersedes_old_in_single_snapshot(client):
    # 换版：新修订提交后，同成果号旧修订立即不再贡献覆盖
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 100, 100))])
    full = client.post(
        "/api/coverage/gaps", json=_gaps((0, 0, 100, 100), [("R-1", 1)])
    ).json()
    assert full["gap_area"] == 0
    _post(client, [_tile("R-1", 2, footprint=(0, 0, 30, 30))])
    after = client.post(
        "/api/coverage/gaps",
        json=_gaps((0, 0, 100, 100), [("R-1", 1), ("R-1", 2)]),
    ).json()
    # 旧修订被拒收于覆盖之外，仅新修订的 900 像素生效
    assert [t["product_id"] for t in after["superseded_tiles"]] == ["R-1"]
    assert after["covered_area"] == 900 and after["gap_area"] == 9100


def test_duplicate_tile_refs_deduped(client):
    _post(client, [_tile("R-1", 1, footprint=(0, 0, 10, 10))])
    body = client.post("/api/coverage/gaps", json={
        "target": {"left": 0, "top": 0, "right": 100, "bottom": 100},
        "tiles": [
            {"product_id": "R-1", "revision": 1},
            {"product_id": "R-1", "revision": 1},
        ],
    }).json()
    assert len(body["contributing_tiles"]) == 1
    assert body["covered_area"] == 100


def test_more_than_64_tiles_rejected(client):
    refs = [{"product_id": f"R-{i}", "revision": 1} for i in range(65)]
    r = client.post("/api/coverage/gaps", json={
        "target": {"left": 0, "top": 0, "right": 10, "bottom": 10},
        "tiles": refs,
    })
    assert r.status_code == 422


def test_invalid_target_rejected(client):
    r = client.post("/api/coverage/gaps", json={
        "target": {"left": 0, "top": 0, "right": 0, "bottom": 10},
        "tiles": [],
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 4. 大坐标精确整数核算
# ---------------------------------------------------------------------------

def test_huge_coordinates_exact_integer_arithmetic(client):
    big = 10**12
    # 目标边长 10^12，瓦片各覆盖一半（无浮点，面积精确）
    _post(client, [
        _tile("R-1", 1, footprint=(0, 0, big // 2, big)),
        _tile("R-2", 1, footprint=(big // 2, 0, big, big)),
    ])
    body = client.post(
        "/api/coverage/gaps",
        json=_gaps((0, 0, big, big), [("R-1", 1), ("R-2", 1)]),
    ).json()
    assert body["target_area"] == big * big
    assert body["gap_area"] == 0
    assert body["covered_area"] == big * big

    # 留出一条 big/2 像素宽的缝：缺口面积精确等于 big * (big/2)
    _post(client, [_tile("R-3", 1, footprint=(big, 0, big + big // 2, big))])
    body2 = client.post("/api/coverage/gaps", json=_gaps(
        (0, 0, big + big // 2, big),
        [("R-1", 1), ("R-3", 1)],
    )).json()
    assert body2["gap_area"] == big * (big // 2)
    assert body2["gaps"] == [{
        "left": big // 2, "top": 0, "right": big, "bottom": big,
        "area": big * (big // 2),
    }]


# ---------------------------------------------------------------------------
# 5. 写入失败不得留下部分足迹（原子性）
# ---------------------------------------------------------------------------

def test_failed_write_leaves_no_partial_footprint(tmp_path, monkeypatch):
    # 用连接包装器在第二条 revisions 插入时注入故障，验证整批回滚、无部分足迹
    db_path = str(tmp_path / "atomic.db")
    app = create_app(db_path)
    real_connect = db.connect
    state = {"inserts": 0}

    class FlakyConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("INSERT INTO REVISIONS"):
                state["inserts"] += 1
                if state["inserts"] == 2:
                    raise sqlite3.OperationalError("simulated write failure")
            return self._inner.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def flaky_connect(path):
        return FlakyConnection(real_connect(path))

    monkeypatch.setattr(db, "connect", flaky_connect)
    with TestClient(app) as c:
        with pytest.raises(sqlite3.OperationalError):
            c.post("/api/batches", json={"items": [
                _tile("R-1", 1, footprint=(0, 0, 10, 10)),
                _tile("R-2", 1, footprint=(0, 0, 20, 20)),
            ]})
    monkeypatch.undo()

    # 回滚后：两个修订都不存在，没有任何部分足迹残留；批次表也无 accepted 记录
    with TestClient(app) as c:
        assert c.get("/api/products/R-1/status").status_code == 404
        assert c.get("/api/products/R-2/status").status_code == 404
        batches = c.get("/api/batches").json()
        assert all(b["status"] != "accepted" for b in batches)


# ---------------------------------------------------------------------------
# 6. 并发：单次核验从同一已提交快照读取，不混用换版前后状态
# ---------------------------------------------------------------------------

def test_coverage_reads_single_consistent_snapshot(client):
    import threading

    _post(client, [_tile("R-1", 1, footprint=(0, 0, 100, 100))])
    results: list[tuple[int, int]] = []

    def updater():
        # 换版：新修订只覆盖左上角小区域，旧修订随即退役
        _post(client, [_tile("R-1", 2, footprint=(0, 0, 10, 10))])

    t = threading.Thread(target=updater)
    t.start()
    for _ in range(5):
        body = client.post("/api/coverage/gaps", json=_gaps(
            (0, 0, 100, 100), [("R-1", 1)]
        )).json()
        # 无论命中旧快照还是新快照，面积恒等式必须自洽
        assert body["target_area"] == body["covered_area"] + body["gap_area"]
        # 旧快照：rev1 现役全覆盖；新快照：rev1 已退役、零覆盖
        assert (body["gap_area"], body["covered_area"]) in [(0, 10000), (10000, 0)]
        results.append((body["gap_area"], body["covered_area"]))
    t.join()
    assert results
    # 换版完成后的稳定结论：rev1 被标记 superseded，目标全部为缺口
    final = client.post("/api/coverage/gaps", json=_gaps(
        (0, 0, 100, 100), [("R-1", 1)]
    )).json()
    assert final["gap_area"] == 10000
    assert final["superseded_tiles"][0]["revision"] == 1


# ---------------------------------------------------------------------------
# 7. 旧库迁移：无足迹列的数据库升级后正常工作
# ---------------------------------------------------------------------------

def test_legacy_database_migrates_and_keeps_old_rows(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    # 用旧版表结构手工建库并写入一条旧清单
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE batches (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL, item_count INTEGER NOT NULL,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            note TEXT, submitted_at TEXT NOT NULL
        );
        CREATE TABLE revisions (
            product_id TEXT NOT NULL, revision INTEGER NOT NULL,
            summary TEXT NOT NULL, parent_product_id TEXT,
            parent_revision INTEGER, batch_id INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            PRIMARY KEY (product_id, revision)
        );
        INSERT INTO batches VALUES (1, 'accepted', 1, 1, NULL, '2026-01-01T00:00:00+00:00');
        INSERT INTO revisions VALUES ('OLD', 1, '历史瓦片', NULL, NULL, 1, '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        # 旧数据仍可追溯且无足迹
        hist = c.get("/api/products/OLD/revisions").json()
        assert hist["revisions"][0]["footprint"] is None
        # 新数据可正常带足迹入库并参与核验
        r = c.post("/api/batches", json={"items": [_tile("NEW", 1, footprint=(0, 0, 5, 5))]})
        assert r.json()["status"] == "accepted"
        body = c.post("/api/coverage/gaps", json=_gaps((0, 0, 10, 10), [
            ("OLD", 1), ("NEW", 1),
        ])).json()
        assert [t["product_id"] for t in body["no_footprint_tiles"]] == ["OLD"]
        assert body["covered_area"] == 25 and body["gap_area"] == 75
