"""Pytest identity evidence and complete/subset validation for the live runner."""
import json
import os
from pathlib import Path


def coverage(expected, selected, reports, exitstatus):
    expected, selected = set(expected), list(selected)
    actual = set(selected)
    passed = {r["nodeid"] for r in reports if r["when"] == "call" and r["outcome"] == "passed"}
    terminal = {(r["nodeid"], r["when"]) for r in reports}
    clean = (exitstatus == 0 and bool(actual) and len(actual) == len(selected)
             and all(r["outcome"] == "passed" for r in reports)
             and passed == actual and all((node, phase) in terminal
                 for node in actual for phase in ("setup", "call", "teardown")))
    missing, extra = expected - actual, actual - expected
    return {"scope": "full" if actual == expected and expected else "subset",
            "passed": clean and not extra, "complete": clean and bool(expected) and not missing and not extra,
            "expected_count": len(expected), "selected_count": len(selected),
            "missing": sorted(missing), "extra": sorted(extra)}


def pytest_collection_finish(session):
    session.config._contract_selected = [item.nodeid for item in session.items]
    _reports.clear()


def pytest_runtest_logreport(report):
    # pytest report hooks lack config; retain only this process's live run.
    _reports.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome})


_reports = []


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("CONTRACT_RESULT_JSON")
    if not target:
        return
    manifest = Path(__file__).parents[1] / "tests/contract/expected-nodeids.json"
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    selected = getattr(session.config, "_contract_selected", [])
    data = {"selected": selected, "reports": _reports, "exitstatus": int(exitstatus),
            **coverage(expected, selected, _reports, int(exitstatus))}
    Path(target).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
