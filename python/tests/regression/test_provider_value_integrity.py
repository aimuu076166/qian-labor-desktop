import math

import pytest

from qian_labor.ai.schemas import same_json_value


@pytest.mark.parametrize("left,right,equal", [
    (True, 1, False), (False, 0, False), (1, 1.0, True),
    (9007199254740993, 9007199254740992.0, False),
    ({"x": [True]}, {"x": [1]}, False),
    ({"x": [1, None], "y": "same"}, {"y": "same", "x": [1.0, None]}, True),
    ([1, 2], [2, 1], False), ([], {}, False), ("1", 1, False),
    (None, None, True), (True, True, True), (False, True, False),
    (math.nan, math.nan, False), (math.inf, math.inf, False),
])
def test_alias_equality_preserves_json_types(left, right, equal):
    assert same_json_value(left, right) is equal
