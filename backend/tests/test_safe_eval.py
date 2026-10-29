import pytest

from backend.app.physics.safe_eval import MissingInput, UnsafeExpression, safe_eval


def test_arithmetic_comparisons_and_whitelisted_functions():
    names = {"battery_v": 24.3, "battery_i": 16.0, "nominal_v": 24}
    assert safe_eval("battery_v * battery_i", names) == pytest.approx(388.8)
    assert safe_eval("0.80 * nominal_v <= battery_v <= 1.25 * nominal_v", names) is True
    assert safe_eval("abs(-2) + max(1, 3) - min(4, 5) + sqrt(16)", {}) == 5
    assert safe_eval("not (battery_i > 20 or battery_v < 10)", names) is True
    assert safe_eval("-battery_i", names) == -16.0


def test_delta_reads_the_change_of_a_named_input():
    assert safe_eval("not (delta(load) > 0 and delta(hz) > 0.3)", {}, deltas={"load": 5.0, "hz": 0.5}) is False
    with pytest.raises(UnsafeExpression):
        safe_eval("delta(1 + 2)", {}, deltas={})                       # delta takes an input name only


@pytest.mark.parametrize("expr", [
    "__import__('os').system('dir')",          # call outside the whitelist
    "battery_v.__class__",                     # attribute access
    "names[0]",                                # subscript
    "(lambda: 1)()",                           # lambda
    "[x for x in (1, 2)]",                     # comprehension
    "open('secrets')",                         # builtin not whitelisted
    "2 ** 1000",                               # runaway power
    "'text'",                                  # strings are not values
    "max(*[1, 2])",                            # starred arguments
])
def test_anything_outside_the_whitelist_is_rejected(expr):
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"battery_v": 1.0, "names": [1]})


def test_missing_names_are_reported_not_guessed():
    with pytest.raises(MissingInput) as err:
        safe_eval("battery_v * battery_i", {"battery_v": 24.0})
    assert err.value.name == "battery_i"


def test_division_by_zero_is_a_missing_value_not_a_crash():
    with pytest.raises(MissingInput):
        safe_eval("output_w / eta", {"output_w": 300.0, "eta": 0.0})
