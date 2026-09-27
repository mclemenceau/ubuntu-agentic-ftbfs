import pytest

from ftbfs.core.expr import ExprError, compile_expr, evaluate

ENV = {
    "triage": {"status": "ok", "fixable": "yes", "confidence": 0.9,
               "tags": ["cmake"]},
    "item": {"arch": "amd64"},
}


@pytest.mark.parametrize("src, expected", [
    ("triage.fixable != 'no'", True),
    ("triage.fixable == 'yes' and item.arch == 'amd64'", True),
    ("triage.confidence >= 0.95", False),
    ("'cmake' in triage.tags", True),
    ("item.arch in ['arm64', 'armhf']", False),
    ("not triage.missing", True),
    ("triage.missing is None", True),
    ("verify.status == 'ok'", False),  # absent stage -> None
    ("triage['fixable'] == 'yes'", True),
    ("triage.missing < 3", False),  # None < 3 is just false
    ("triage.status == 'ok' or item.arch == 'x'", True),
])
def test_evaluate(src, expected):
    assert evaluate(src, ENV) is expected


@pytest.mark.parametrize("src", [
    "__import__('os').system('true')",
    "triage.fixable.upper() == 'YES'",
    "[x for x in triage.tags]",
    "lambda: 1",
    "triage[item.arch]",
    "1 +",
])
def test_rejects_unsafe_or_invalid(src):
    with pytest.raises(ExprError):
        compile_expr(src)
