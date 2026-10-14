"""景点属性补全的模型结构化输出。"""

from pydantic import BaseModel, Field

from app.schemas.trip_plan_schema import IndoorType, Subtype


class PoiEnrichItem(BaseModel):
    """单个景点的属性判断。"""

    poi_id: str = Field(description="输入中给出的景点标识，原样返回")
    subtype: Subtype = Field(description="细分体验类，必须来自词表")
    indoor: IndoorType = Field(description="室内、户外或室内外结合")
    duration_minutes: int = Field(ge=20, le=360, description="普通游客建议游玩分钟数，不含往返交通和用餐")


class PoiEnrichResult(BaseModel):
    """一批景点的属性判断。"""

    items: list[PoiEnrichItem] = Field(description="逐个景点的判断结果")
