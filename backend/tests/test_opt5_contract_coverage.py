import pytest
from scripts.contract_results import coverage


def reports(nodes):
    return [{"nodeid": node, "when": phase, "outcome": "passed"}
            for node in nodes for phase in ("setup", "call", "teardown")]


def test_one_pass_is_only_a_subset():
    result = coverage(["a", "b"], ["a"], reports(["a"]), 0)
    assert result["passed"] and not result["complete"] and result["scope"] == "subset"
    assert result["missing"] == ["b"]


def test_exact_complete_identity_set():
    result = coverage(["a", "b"], ["b", "a"], reports(["b", "a"]), 0)
    assert result["passed"] and result["complete"] and result["scope"] == "full"


@pytest.mark.parametrize("case", ["skip", "teardown", "missing-call", "extra", "duplicate", "exit5", "empty"])
def test_bad_run_cannot_be_full_or_subset_pass(case):
    selected, rows, code = ["a", "b"], reports(["a", "b"]), 0
    if case == "skip":
        rows[1]["outcome"] = "skipped"
    elif case == "teardown":
        rows[2]["outcome"] = "failed"
    elif case == "missing-call":
        rows.pop(1)
    elif case == "extra":
        selected += ["c"]
        rows += reports(["c"])
    elif case == "duplicate":
        selected += ["a"]
    elif case == "exit5":
        code = 5
    elif case == "empty":
        selected, rows = [], []
    result = coverage(["a", "b"], selected, rows, code)
    assert not result["complete"] and not result["passed"]
