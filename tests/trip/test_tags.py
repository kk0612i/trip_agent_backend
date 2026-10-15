"""标签匹配测试：同一维度内任一满足，不同维度之间同时满足。"""

import pytest

from app.agent.trip.planning.tags import tags_match


@pytest.mark.parametrize(
    ("required", "tags", "expected"),
    [
        ([], ["temple", "outdoor"], True),
        (["museum", "art_gallery"], ["museum", "culture_art", "indoor"], True),
        (["museum", "art_gallery"], ["art_gallery", "culture_art", "indoor"], True),
        (["museum", "art_gallery"], ["temple", "religion", "outdoor"], False),
        (["outdoor"], ["park", "nature", "outdoor"], True),
        (["outdoor", "temple"], ["temple", "religion", "outdoor"], True),
        (["outdoor", "temple"], ["temple", "religion", "indoor"], False),
        (["outdoor", "temple"], ["park", "nature", "outdoor"], False),
        (["indoor", "outdoor"], ["museum", "indoor"], True),
    ],
)
def test_tags_match(required: list[str], tags: list[str], expected: bool) -> None:
    """类型标签之间是“或”，类型与室内外之间是“且”。"""
    assert tags_match(required, tags) is expected
