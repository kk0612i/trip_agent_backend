"""标签词表的中文名称、搜索关键词、标签匹配和按规则推断景点属性。"""

from app.schemas.trip_plan_schema import SUBTYPE_MAIN_CATEGORY, AmapPoi, PoiProfile

# 标签中文名，用于提示词和回复展示。
TAG_LABELS: dict[str, str] = {
    "history": "历史古迹", "culture_art": "文化艺术", "religion": "宗教寺观", "nature": "自然风光",
    "leisure": "休闲公园", "entertainment": "主题娱乐", "urban": "城市地标",
    "museum": "博物馆", "art_gallery": "美术馆", "exhibition": "展览馆", "memorial": "纪念馆",
    "former_residence": "名人故居", "ancient_architecture": "古建筑", "historic_site": "历史遗址",
    "ancient_town": "古镇古村", "historic_street": "历史街区", "temple": "寺庙道观", "church": "教堂",
    "mountain": "山岳", "lake_river": "湖泊河流", "seaside": "海滨", "forest_wetland": "森林湿地",
    "botanical_garden": "植物园", "garden": "园林", "urban_park": "城市公园", "square": "广场",
    "zoo_aquarium": "动物园水族馆", "theme_park": "主题乐园", "landmark_building": "地标建筑",
    "commercial_street": "商业街", "university": "高校", "other": "其他",
    "indoor": "室内", "outdoor": "户外",
}

INDOOR_LABELS: dict[str, str] = {"indoor": "室内", "outdoor": "户外", "mixed": "室内外结合"}

# 偏好或替换目标对应的追加搜索词，拼在城市名后面。
TAG_SEARCH_KEYWORDS: dict[str, str] = {
    "history": "历史古迹", "culture_art": "博物馆", "religion": "寺庙", "nature": "自然风光",
    "leisure": "公园", "entertainment": "游乐园", "urban": "地标",
    "museum": "博物馆", "art_gallery": "美术馆", "exhibition": "展览馆", "memorial": "纪念馆",
    "former_residence": "故居", "ancient_architecture": "古建筑", "historic_site": "遗址",
    "ancient_town": "古镇", "historic_street": "历史街区", "temple": "寺庙", "church": "教堂",
    "mountain": "登山", "lake_river": "湖", "seaside": "海滩", "forest_wetland": "湿地公园",
    "botanical_garden": "植物园", "garden": "园林", "urban_park": "公园", "square": "广场",
    "zoo_aquarium": "动物园", "theme_park": "游乐园", "landmark_building": "地标建筑",
    "commercial_street": "步行街", "university": "大学", "indoor": "室内景点",
}


def item_tags(subtype: str, main_category: str, indoor: str) -> list[str]:
    """返回景点可被选择器和替换目标匹配的标签。

    室内外结合的景点下雨时同样受影响，因此归入“户外”标签，不归入“室内”。

    Args:
        subtype: 细分体验类。
        main_category: 主体验类。
        indoor: 室内外属性。

    Returns:
        标签列表。
    """
    return [subtype, main_category, "indoor" if indoor == "indoor" else "outdoor"]


def tags_match(required: list[str], tags: list[str]) -> bool:
    """判断景点标签是否包含全部要求的标签。"""
    return set(required).issubset(tags)


# 规则兜底：按名称和高德分类中的关键词推断（细分体验，室内外，建议分钟数），按顺序优先匹配。
_RULES: list[tuple[tuple[str, ...], str, str, int]] = [
    (("故居",), "former_residence", "indoor", 60),
    (("纪念馆", "烈士陵园", "纪念碑"), "memorial", "mixed", 90),
    (("美术馆", "艺术馆", "画院"), "art_gallery", "indoor", 90),
    (("博物馆", "博物院", "陈列馆"), "museum", "indoor", 120),
    (("展览馆", "科技馆", "规划馆", "展示馆"), "exhibition", "indoor", 90),
    (("海洋馆", "水族馆", "海洋公园", "动物园", "野生动物"), "zoo_aquarium", "mixed", 180),
    (("乐园", "欢乐谷", "游乐", "迪士尼", "影视城"), "theme_park", "outdoor", 300),
    (("植物园",), "botanical_garden", "outdoor", 120),
    (("教堂",), "church", "indoor", 45),
    (("寺", "庙", "禅院", "庵", "道观", "宫观", "祠"), "temple", "mixed", 90),
    (("古镇", "古村", "古城"), "ancient_town", "outdoor", 180),
    (("遗址", "古迹", "城墙"), "historic_site", "outdoor", 90),
    (("塔", "楼", "阁", "宫", "府"), "ancient_architecture", "mixed", 90),
    (("大学", "学院"), "university", "outdoor", 60),
    (("湿地", "森林"), "forest_wetland", "outdoor", 150),
    (("海滩", "海滨", "沙滩", "海湾", "海岸"), "seaside", "outdoor", 150),
    (("公园",), "urban_park", "outdoor", 90),
    (("山",), "mountain", "outdoor", 180),
    (("湖", "江", "河", "溪", "潭", "瀑布"), "lake_river", "outdoor", 120),
    (("园林", "花园", "园"), "garden", "outdoor", 90),
    (("广场",), "square", "outdoor", 45),
    (("步行街", "商业街", "夜市"), "commercial_street", "outdoor", 90),
    (("老街", "历史街区", "街区", "胡同", "巷", "街"), "historic_street", "outdoor", 90),
    (("大厦", "电视塔", "中心", "观光"), "landmark_building", "indoor", 60),
]


def rule_profile(poi: AmapPoi) -> PoiProfile:
    """模型不可用或输出缺失时，按名称和高德分类推断景点属性。

    Args:
        poi: 高德景点。

    Returns:
        来源为 rule 的景点属性。
    """
    text = f"{poi.name} {poi.amap_type}"
    for keywords, subtype, indoor, minutes in _RULES:
        if any(keyword in text for keyword in keywords):
            return PoiProfile(
                poi_id=poi.poi_id, subtype=subtype, indoor=indoor, duration_minutes=minutes, source="rule",
            )
    return PoiProfile(poi_id=poi.poi_id, subtype="other", indoor="mixed", duration_minutes=90, source="rule")


def main_category_of(subtype: str) -> str:
    """返回细分体验对应的主体验类。"""
    return SUBTYPE_MAIN_CATEGORY[subtype]
