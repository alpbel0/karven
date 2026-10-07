"""Study runner: method registry, cell lists, records, resumable chunks."""

from __future__ import annotations

import gzip
import json

import numpy as np
import pytest

from app.analysis.calib.dgp import STRUCTURES, Cell
from app.analysis.calib.study import (
    FALSE_SUPPORT_STRUCTURES,
    POWER_STRUCTURES,
    ManifestMismatch,
    candidate_methods,
    run_replicates,
    run_study,
    select_methods,
    tuning2_cells,
    tuning_cells,
    validation_cells,
)


def test_candidate_methods_follow_the_protocol():
    names = [m.name for m in candidate_methods()]
    assert len(names) == len(set(names))
    assert {"hac_rule", "hac_24_bonf", "shift", "stationary_pw", "chain_12", "chain_24"} <= set(
        names
    )
    pairwise_only = {m.name for m in candidate_methods() if m.max_k == 1}
    assert pairwise_only == {
        "shift",
        "stationary_12",
        "stationary_18",
        "stationary_24",
        "stationary_pw",
    }


def test_cell_lists_are_complete_and_unique():
    tuning = tuning_cells()
    assert len({c.id for c in tuning}) == len(tuning)
    structures = {c.structure for c in tuning}
    assert set(FALSE_SUPPORT_STRUCTURES) <= structures and set(POWER_STRUCTURES) <= structures
    assert {c.layer for c in tuning} == {"stat", "e2e"}
    assert any(c.gap for c in tuning) and any(c.nominal for c in tuning)
    validation = validation_cells()
    assert {"arma", "ar_neg", "hetero", "semisynth"} <= {c.family for c in validation}
    assert {"seasonal_indep", "sar12_08", "sar12_05", "iid"} <= {c.family for c in validation}
    # region sizes (the production recent window has 117 months) and the modulo-12 controls
    assert {c.n for c in validation} == {100, 250, 111, 245}
    assert {c.layer for c in validation} == {"stat"}
    assert len(validation) == (14 * 2 + 3 * 2) * len(FALSE_SUPPORT_STRUCTURES)
    # validation cells score false support only: every structure there is a null of some kind
    assert all(not STRUCTURES[c.structure].true_support for c in validation)


def test_records_hold_every_method_window_and_the_gate_features():
    cell = Cell("stat", "iid", "k1_real07", 120)
    records = run_replicates(cell, candidate_methods(), range(2), 1001)
    assert [r["rep"] for r in records] == [0, 1]
    record = records[0]
    assert record["true_support"] is True and record["k"] == 1 and record["cell"] == cell.id
    assert len(record["m"]) == len(candidate_methods())
    window = record["m"]["hac_rule"]["w"]["all_years"]
    assert window["r"] in {"S", "U", "I"} and set(window["f"]) >= {"n", "rho1", "var_ratio"}
    assert window["r"] == "S"  # a strong real effect is found
    assert record["m"]["chain_12"]["w"]["all_years"]["l"] == [1]


def test_pairwise_methods_are_skipped_for_several_drivers():
    cell = Cell("stat", "iid", "k2_real_real07", 120)
    record = run_replicates(cell, candidate_methods(), range(1), 1001)[0]
    assert "shift" not in record["m"] and "stationary_12" not in record["m"]
    assert "chain_12" in record["m"] and "hac_rule" in record["m"]


def test_replicates_are_reproducible_for_a_seed():
    cell = Cell("stat", "ar05", "k1_null", 120)
    methods = [m for m in candidate_methods() if m.name in ("hac_rule", "shift", "chain_12")]
    first = run_replicates(cell, methods, range(3), 1001)
    again = run_replicates(cell, methods, range(3), 1001)
    other = run_replicates(cell, methods, range(3), 1002)
    assert first == again and first != other


def test_run_study_writes_resumable_chunks(tmp_path):
    cells = [Cell("stat", "iid", "k1_null", 60)]
    run_study(cells, reps=4, chunk=2, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule")
    files = sorted(tmp_path.glob("*.jsonl.gz"))
    assert len(files) == 2
    stamp = [f.stat().st_mtime_ns for f in files]
    run_study(cells, reps=4, chunk=2, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule")
    assert [f.stat().st_mtime_ns for f in sorted(tmp_path.glob("*.jsonl.gz"))] == stamp  # skipped
    with gzip.open(files[0], "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert len(rows) == 2 and set(rows[0]["m"]) == {"hac_rule", "hac_rule_bonf"}


def test_select_methods_by_exact_name_or_substring():
    assert [m.name for m in select_methods(names=("shift", "chain_12"))] == ["shift", "chain_12"]
    assert {m.name for m in select_methods("hac_24")} == {"hac_24", "hac_24_bonf"}
    with pytest.raises(ValueError):
        select_methods(names=("nonexistent",))
    names = [m.name for m in candidate_methods()]
    assert {"hac_rule", "hac_default"} <= set(names)  # the rule of thumb and the engine default


def test_the_rule_and_the_default_bandwidths_differ_for_annual_changes():
    cell = Cell("e2e", "iid", "k1_null", 120, "annual_pct_change")
    methods = [m for m in candidate_methods() if m.name in ("hac_rule", "hac_default", "hac_11")]
    record = run_replicates(cell, methods, range(1), 1001)[0]
    assert set(record["m"]) == {"hac_rule", "hac_default", "hac_11"}  # all three run


def test_a_directory_of_another_run_is_refused(tmp_path):
    cells = [Cell("stat", "iid", "k1_null", 60)]
    run_study(cells, reps=2, chunk=2, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf")
    with pytest.raises(ManifestMismatch):
        run_study(
            cells, reps=2, chunk=2, seed_base=1002, out=tmp_path, jobs=1, only="hac_rule_bonf"
        )
    with pytest.raises(ManifestMismatch):  # a different chunk size would overlap the old chunks
        run_study(
            cells, reps=2, chunk=1, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf"
        )


def test_a_failing_method_is_recorded_as_failed_and_the_run_continues(monkeypatch):
    from app.analysis.calib import study

    original = study.run_relation_test

    def flaky(target, drivers, spec, **kwargs):
        if type(kwargs["method"]).__name__ == "CircularShift":
            raise np.linalg.LinAlgError("boom")
        return original(target, drivers, spec, **kwargs)

    monkeypatch.setattr(study, "run_relation_test", flaky)
    cell = Cell("stat", "iid", "k1_null", 60)
    methods = [m for m in candidate_methods() if m.name in ("shift", "hac_rule")]
    record = run_replicates(cell, methods, range(1), 1001)[0]
    assert record["m"]["shift"]["status"] == "failed"
    assert record["m"]["shift"]["error"].startswith("LinAlgError")
    assert record["m"]["hac_rule"]["status"] in ("supported", "unsupported", "insufficient_data")


def test_validation_runs_only_the_frozen_method(tmp_path):
    from app.analysis.calib.study import main

    with pytest.raises(SystemExit):
        main(["run", "--stage", "validate", "--out", str(tmp_path)])  # no --freeze
    freeze = tmp_path / "freeze.json"
    freeze.write_text(json.dumps({"methods": ["hac_default"], "budget_reps": 2}), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--stage",
                "validate",
                "--freeze",
                str(freeze),
                "--out",
                str(tmp_path),
                "--only-method",
                "hac",
            ]
        )  # no filters in validation


def test_the_family_filter_selects_new_families_only(tmp_path):
    from app.analysis.calib.study import main

    main(
        [
            "run",
            "--stage",
            "tune",
            "--reps",
            "2",
            "--chunk",
            "2",
            "--jobs",
            "1",
            "--out",
            str(tmp_path),
            "--family",
            "sar12_08",
            "--only-cell",
            "k1_null/n60",
            "--only-method",
            "hac_rule_bonf",
        ]
    )
    names = [p.name for p in tmp_path.glob("*.jsonl.gz")]
    assert len(names) == 1 and "sar12_08" in names[0]


def test_tuning2_cells_follow_the_preregistration():
    cells = tuning2_cells()
    assert len({c.id for c in cells}) == len(cells)
    assert {c.n for c in cells} == {100, 250}
    families = {c.family for c in cells}
    assert families == {
        "iid",
        "ar05",
        "ma11",
        "sar12_05",
        "seasonal_weak",
        "seasonal_indep",
        "seasonal_common",
        "sar12_08",
    }
    power = {c.structure for c in cells if STRUCTURES[c.structure].true_support}
    assert power == set(POWER_STRUCTURES)
    # power cells exist for the region families only
    assert {c.family for c in cells if STRUCTURES[c.structure].true_support} == {
        "iid",
        "ar05",
        "ma11",
        "sar12_05",
        "seasonal_weak",
    }
    assert len(cells) == (8 * 9 + 5 * 8) * 2


def test_run_study_can_extend_a_run_from_a_start_replicate(tmp_path):
    cells = [Cell("stat", "iid", "k1_null", 100)]
    run_study(
        cells,
        reps=6,
        chunk=2,
        seed_base=1001,
        out=tmp_path,
        jobs=1,
        only="hac_rule_bonf",
        start_rep=4,
    )
    files = sorted(p.name for p in tmp_path.glob("*.jsonl.gz"))
    assert len(files) == 1 and files[0].endswith(".4-6.jsonl.gz")
    with gzip.open(tmp_path / files[0], "rt", encoding="utf-8") as handle:
        assert [json.loads(line)["rep"] for line in handle] == [4, 5]
    manifest = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["start_rep"] == 4 and manifest["protocol"] == "1.5"
