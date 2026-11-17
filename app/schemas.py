"""Pydantic 请求/响应模型。

字段使用英文命名，中文说明经 FastAPI 自动呈现在 /docs 的 Schema 中。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# 阻断原因枚举
REASON_MISSING = "missing"
REASON_SUPERSEDED = "superseded"
REASON_CYCLE = "cycle"

BLOCKER_LABELS = {
    REASON_MISSING: "父项缺失（引用的成果或修订从未提交）",
    REASON_SUPERSEDED: "父项已非现役（该成果存在更高已提交修订）",
    REASON_CYCLE: "循环引用（谱系链路成环）",
}


class Footprint(BaseModel):
    """整数像素足迹矩形（左上、右下，右下边界不含）。"""

    left: int = Field(..., description="左边界像素坐标（含）")
    top: int = Field(..., description="上边界像素坐标（含）")
    right: int = Field(..., description="右边界像素坐标（不含），必须严格大于 left")
    bottom: int = Field(..., description="下边界像素坐标（不含），必须严格大于 top")

    @model_validator(mode="after")
    def _check_positive_extent(self) -> "Footprint":
        if self.right <= self.left:
            raise ValueError("足迹 right 必须严格大于 left（右下边界不含）")
        if self.bottom <= self.top:
            raise ValueError("足迹 bottom 必须严格大于 top（右下边界不含）")
        return self

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.right, self.bottom


class ManifestItem(BaseModel):
    """清单中的单个条目：原始瓦片或一级/多级拼图。"""

    product_id: str = Field(..., min_length=1, description="成果号，如 R-100 / M-200")
    revision: int = Field(..., ge=1, description="修订号，从 1 开始；同成果号下取最高修订为现役")
    summary: str = Field(..., min_length=1, description="内容摘要")
    parent_product_id: Optional[str] = Field(
        default=None, description="固定引用的父成果号；原始瓦片留空，拼图必填"
    )
    parent_revision: Optional[int] = Field(
        default=None, ge=1, description="固定引用的父修订号；原始瓦片留空，拼图必填"
    )
    footprint: Optional[Footprint] = Field(
        default=None,
        description=(
            "可选的整数像素足迹（left/top/right/bottom，右下边界不含）；"
            "旧清单不带足迹仍可提交与追溯"
        ),
    )

    @model_validator(mode="after")
    def _check_parent_pair(self) -> "ManifestItem":
        # 父成果号与父修订号必须同时出现或同时为空
        if (self.parent_product_id is None) != (self.parent_revision is None):
            raise ValueError("parent_product_id 与 parent_revision 必须同时提供或同时为空")
        return self

    def key(self) -> tuple[str, int]:
        return self.product_id, self.revision


class BatchSubmission(BaseModel):
    """分批提交的一整批 JSON 清单。"""

    items: list[ManifestItem] = Field(..., min_length=1, description="本批清单条目，至少 1 条")


class BatchConflictDetail(BaseModel):
    """同号同修订但清单不同的冲突条目。"""

    product_id: str
    revision: int
    submitted: ManifestItem
    existing: ManifestItem


class BatchResponse(BaseModel):
    """提交批次处理结果。"""

    batch_id: int = Field(..., description="批次号")
    status: Literal["accepted", "duplicate", "rejected"] = Field(
        ..., description="accepted=已入库；duplicate=完全重复提交，幂等返回；rejected=整批拒收，零写入"
    )
    item_count: int = Field(..., description="本批条目数（去重前）")
    inserted_count: int = Field(..., description="实际新写入的条目数")
    conflicts: list[BatchConflictDetail] = Field(
        default_factory=list, description="拒收时的冲突明细（清单内容不一致）"
    )
    message: str


class Blocker(BaseModel):
    """导致拼图不可交付的一条引用路径问题。"""

    reason: Literal["missing", "superseded", "cycle"] = Field(..., description="阻断类型")
    reason_label: str = Field(..., description="阻断类型的中文说明")
    path: list[str] = Field(
        ...,
        description=(
            "引用路径，元素形如 '成果号@修订号'；"
            "沿当前成果逐级向上追溯，最后一个元素即出问题的引用节点"
        ),
    )
    detail: str = Field(..., description="问题的具体说明")


class ProductStatus(BaseModel):
    """单个现役成果（或指定修订）的可交付状态。"""

    product_id: str
    revision: int = Field(..., description="实际评估的修订号；不指定时为现役修订")
    is_active: bool = Field(..., description="该修订是否为其成果号下的最高已提交修订")
    kind: Literal["tile", "mosaic"] = Field(..., description="tile=原始瓦片（无父项），mosaic=拼图（有父项）")
    summary: str
    parent_product_id: Optional[str]
    parent_revision: Optional[int]
    deliverable: bool = Field(..., description="true 仅当整条谱系有效（无缺失/退役/环）")
    blockers: list[Blocker] = Field(default_factory=list, description="受阻引用路径；可交付时为空")


class RevisionHistoryItem(BaseModel):
    """成果修订历史中的一条。"""

    revision: int
    summary: str
    parent_product_id: Optional[str]
    parent_revision: Optional[int]
    footprint: Optional[Footprint] = Field(
        default=None, description="提交时登记的像素足迹；旧清单无足迹时为 null"
    )
    is_active: bool
    batch_id: int
    submitted_at: str


class RevisionHistoryResponse(BaseModel):
    product_id: str
    active_revision: Optional[int]
    revisions: list[RevisionHistoryItem]


class BatchRecord(BaseModel):
    """批次记录（用于追溯与提交核对）。"""

    batch_id: int
    status: Literal["accepted", "duplicate", "rejected"]
    item_count: int
    inserted_count: int
    note: Optional[str]
    submitted_at: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    database: str


# ---------------------------------------------------------------------------
# 覆盖核验（缺口矩形）
# ---------------------------------------------------------------------------

class CoverageTileRef(BaseModel):
    """核验请求中指定的一个成果号 + 修订号。"""

    product_id: str = Field(..., min_length=1, description="成果号，如 R-100")
    revision: int = Field(..., ge=1, description="指定的修订号")


class CoverageRequest(BaseModel):
    """覆盖核验请求：目标矩形 + 至多 64 个指定瓦片。"""

    target: Footprint = Field(..., description="目标画幅矩形（像素坐标，右下边界不含）")
    tiles: list[CoverageTileRef] = Field(
        default_factory=list,
        max_length=64,
        description="至多 64 个指定（成果号, 修订号）瓦片；重复指定按一个处理",
    )


class GapRect(BaseModel):
    """目标内的一个缺口矩形（互不重叠）。"""

    left: int
    top: int
    right: int
    bottom: int
    area: int = Field(..., description="缺口面积（像素数，等于 (right-left)*(bottom-top)）")


class CoverageTileIssue(BaseModel):
    """未实际贡献覆盖的指定瓦片及其原因。"""

    product_id: str
    revision: int
    reason: Literal["missing", "superseded", "no_footprint"] = Field(
        ...,
        description=(
            "missing=成果或修订从未提交；superseded=该修订已非现役（同成果号有更高修订）；"
            "no_footprint=现役有效但提交清单未带足迹"
        ),
    )
    detail: str = Field(..., description="问题的具体说明")


class CoverageContributingTile(BaseModel):
    """实际贡献覆盖的指定瓦片（足迹已按目标矩形裁剪）。"""

    product_id: str
    revision: int
    footprint: Footprint = Field(..., description="库内登记的原始足迹")
    clipped_footprint: Optional[Footprint] = Field(
        ..., description="与目标矩形求交后的实际覆盖矩形；足迹完全在目标外时为 null"
    )
    applied_area: int = Field(
        ..., description="目标内实际覆盖面积（裁剪后，不含与其他瓦片重叠的重复计数由缺口算法处理）"
    )
    is_active: bool = Field(..., description="该修订是否为其成果号下的现役修订（参与覆盖者恒为 true）")


class CoverageResponse(BaseModel):
    """覆盖核验结果。"""

    target: Footprint
    target_area: int = Field(..., description="目标矩形总面积（像素数）")
    covered_area: int = Field(..., description="目标内被覆盖的总面积（重叠不重复计数）")
    gap_area: int = Field(..., description="目标内缺口总面积（像素数），满足 target_area=covered_area+gap_area")
    gaps: list[GapRect] = Field(
        default_factory=list,
        description="目标内互不重叠的缺口矩形（坐标按 top、left 排序）；完全覆盖时为空",
    )
    contributing_tiles: list[CoverageContributingTile] = Field(
        default_factory=list, description="实际贡献覆盖的瓦片（现役且带足迹）"
    )
    missing_tiles: list[CoverageTileIssue] = Field(
        default_factory=list, description="成果或修订从未提交的指定瓦片"
    )
    superseded_tiles: list[CoverageTileIssue] = Field(
        default_factory=list, description="已非现役修订的指定瓦片（新修订提交后旧修订不再贡献覆盖）"
    )
    no_footprint_tiles: list[CoverageTileIssue] = Field(
        default_factory=list, description="现役有效但清单未带足迹的指定瓦片"
    )
