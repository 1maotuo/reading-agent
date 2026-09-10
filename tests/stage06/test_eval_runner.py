from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "evals" / "stage06" / "run_eval.py"
CASES_PATH = ROOT / "evals" / "stage06" / "cases.json"


def _load_runner():
    spec = importlib.util.spec_from_file_location("stage06_eval_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_bounded_unique_and_has_hard_gates() -> None:
    manifest = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    ids = [item["id"] for item in cases]
    assert 20 <= len(cases) <= 30
    assert len(ids) == len(set(ids))
    assert {item["severity"] for item in cases} == {"P0", "P1"}
    assert manifest["thresholds"] == {"p0_pass_rate": 1.0, "p1_pass_rate": 1.0}


def test_deterministic_eval_produces_complete_passing_report(tmp_path: Path) -> None:
    runner = _load_runner()
    output = tmp_path / "report.json"
    report = runner.run_evaluation(output)
    assert report["status"] == "passed"
    assert report["case_count"] == 30
    assert report["missing_case_ids"] == []
    assert report["extra_case_ids"] == []
    assert report["summary"]["passed"] == 30
    assert report["summary"]["failed"] == 0
    assert output.exists()
