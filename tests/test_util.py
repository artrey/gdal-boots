import pytest

from gdal_boots.gdal import check_maximum_one_value_not_none


@pytest.mark.parametrize(
    "values,expected_result",
    [
        ([], True),
        ([1], True),
        ([None], True),
        ([None, 1], True),
        ([1, None], True),
        ([0, None], True),
        ([None, None], True),
        ([0, 0], False),
        ([{}, 0], False),
        ([{}, None], True),
        ([{}, []], False),
        ([10, [], None], False),
        ([10, None, None], True),
        ([None, 10, None, None], True),
        ([None, 10, None, 5], False),
    ],
)
def test_check_maximum_one_variable_not_none(values: list, expected_result: bool):
    assert check_maximum_one_value_not_none(*values) == expected_result
