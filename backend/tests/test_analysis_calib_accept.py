"""Acceptance analysis: Clopper-Pearson, cells, gate masks, tuning, composites, validation."""

from __future__ import annotations

import gzip
import json
import math

import pytest

from app.analysis.calib import accept
from app.analysis.calib.accept import (
    build_composites,
    cell_table,
    clopper_pearson_upper,
    covers_all_driver_counts,
    evaluate_gate,
    family_rank,
    in_region,
    load_records,
    parse_cell,
    pick_method,
    qualifies,
    reliable_mask,
    tune_gate,
    validate,
    with_composites,
)
from app.analysis.calib.dgp import Cell
from app.analysis.calib.study import run_study
from app.analysis.gate import GateThresholds


def test_clopper_pearson_upper_known_values():
    assert math.isclose(clopper_pearson_upper(0, 100), 1 - 0.05 ** (1 / 100), rel_tol=1e-9)
    assert math.isclose(clopper_pearson_upper(0, 1500), 1 - 0.05 ** (1 / 1500), rel_tol=1e-9)
    assert 0.05 < clopper_pearson_upper(5, 100) < 0.11
    assert clopper_pearson_upper(10, 10) == 1.0 and clopper_pearson_upper(0, 0) == 1.0
    assert clopper_pearson_upper(40, 1500) < clopper_pearson_upper(60, 1500)


def test_parse_cell_ids():
    plain = parse_cell("stat/ar05/k2_real_null/n100")
    assert plain["layer"] == "stat" and plain["family"] == "ar05" and plain["n"] == 100
    assert plain["transform"] == "" and not plain["gap"] and not plain["nominal"]
    e2e = parse_cell("e2e/iid/k1_null/n250/annual_pct_change/gap")
    assert e2e["transform"] == "annual_pct_change" and e2e["gap"] and not e2e["nominal"]
    assert parse_cell("e2e/iid/k1_null/n100/difference/nominal")["nominal"] is True


def test_family_rank_prefers_the_simplest_family():
    names = ["chain_12", "stationary_pw", "shift", "hac_24_bonf"]
    assert sorted(names, key=family_rank) == ["hac_24_bonf", "shift", "stationary_pw", "chain_12"]
    assert family_rank("shift+chain_12") == 1  # a composite ranks by its pairwise part


def test_pick_method_treats_close_powers_as_ties_and_prefers_the_simplest():
    def item(power):
        return {"stats": {"mean_power": power}}

    candidates = {"chain_12": item(0.80), "hac_default": item(0.795), "shift+chain_12": item(0.70)}
    assert pick_method(candidates) == "hac_default"  # within 0.01 of the best: the simplest wins
    clear = {"chain_12": item(0.80), "hac_default": item(0.60)}
    assert pick_method(clear) == "chain_12"  # a clear winner is not overridden by simplicity
    assert pick_method({}) is None


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    out = tmp_path_factory.mktemp("tiny")
    cells = [
        Cell("stat", "iid", "k1_null", 100),
        Cell("stat", "iid", "k1_real07", 100),
        Cell("stat", "ar09", "k1_null", 60),
    ]
    run_study(cells, reps=30, chunk=30, seed_base=1001, out=out, jobs=1, only="hac_rule")
    return load_records(out)


def test_load_records_flattens_to_cell_replicate_method_rows(tiny):
    assert set(tiny["method"]) == {"hac_rule", "hac_rule_bonf"}
    assert len(tiny) == 3 * 30 * 2
    columns = {"r1", "r2", "p1", "n1", "rho11", "var_ratio2", "family", "n", "true_support"}
    assert columns <= set(tiny.columns)
    assert set(tiny["r1"]) <= {"S", "U", "I"}
    region = in_region(tiny)
    assert set(tiny[region]["family"]) == {"iid"} and set(tiny[region]["n"]) == {100}
    assert tiny.attrs["failed"] == {}


def test_gate_masks_and_cell_table(tiny):
    method = tiny[tiny["method"] == "hac_rule"]
    open_gate = reliable_mask(method, None)
    shut = GateThresholds(
        n_min=10_000, rho1_max=9, rho_seasonal_max=9, var_ratio_max=1e9, mean_shift_max=1e9
    )
    assert open_gate.sum() > 0 and reliable_mask(method, shut).sum() == 0
    table = cell_table(method, None)
    assert set(table.index) == {
        "stat/iid/k1_null/n100",
        "stat/iid/k1_real07/n100",
        "stat/ar09/k1_null/n60",
    }
    real = table.loc["stat/iid/k1_real07/n100"]
    assert real["power"] >= 25 and real["false_support"] == 0  # a strong effect is found
    assert table.loc["stat/iid/k1_null/n100"]["power"] == 0  # no truth to support in a null
    assert table.loc["stat/ar09/k1_null/n60"]["decided"] == 30  # n=60 decides for one driver


def test_evaluate_gate_and_tuning_on_the_tiny_study(tiny):
    method = tiny[tiny["method"] == "hac_rule"]
    stats = evaluate_gate(method, GateThresholds(36, 1.01, 1.01, float("inf"), float("inf")))
    assert set(stats) == {
        "gated_ok_fraction",
        "cells_ok_fraction",
        "reliable_rate",
        "strong_power",
        "mean_power",
        "worst_fs",
    }
    assert 0.0 <= stats["reliable_rate"] <= 1.0 and stats["strong_power"] > 0.5
    good = {
        "cells_ok_fraction": 1.0,
        "gated_ok_fraction": 1.0,
        "reliable_rate": 0.7,
        "strong_power": 0.6,
    }
    assert qualifies(good)
    assert not qualifies({**good, "cells_ok_fraction": 0.97})  # v1.5: EVERY cell must be ok
    assert not qualifies(
        {**good, "gated_ok_fraction": 0.5}
    )  # the gate must reject persistent seasonality
    assert not qualifies({**good, "strong_power": 0.4})
    gate, found = tune_gate(method)
    # only 30 reps: too few reliable replicates per null cell to certify anything
    assert gate is None and found is None


def test_eligibility_needs_k1_k2_and_k3_cells(tiny):
    assert covers_all_driver_counts(tiny[tiny["method"] == "hac_rule"]) is False  # only k = 1 here


def test_composites_and_eligibility_on_a_study_with_all_driver_counts(tmp_path):
    cells = [
        Cell("stat", "iid", "k1_null", 100),
        Cell("stat", "iid", "k2_real_null", 100),
        Cell("stat", "iid", "k3_real_null_null", 100),
    ]
    run_study(
        cells,
        reps=4,
        chunk=4,
        seed_base=1001,
        out=tmp_path,
        jobs=1,
        names=("hac_rule", "shift", "chain_12"),
    )
    frame = load_records(tmp_path)
    assert "shift" in set(frame["method"]) and "chain_12" in set(frame["method"])
    assert covers_all_driver_counts(frame[frame["method"] == "hac_rule"]) is True
    assert covers_all_driver_counts(frame[frame["method"] == "shift"]) is False  # pairwise only
    composites = build_composites(frame)
    assert set(composites["method"]) == {"shift+chain_12"}
    composite = composites[composites["method"] == "shift+chain_12"]
    assert set(composite[composite["k"] == 1]["method"]) == {"shift+chain_12"}
    # k = 1 rows come from the pairwise method, k >= 2 rows from the whole-chain bootstrap
    shift_rows = frame[(frame["method"] == "shift") & (frame["k"] == 1)]
    assert list(composite[composite["k"] == 1]["p1"]) == list(shift_rows["p1"])
    both = with_composites(frame)
    assert covers_all_driver_counts(both[both["method"] == "shift+chain_12"]) is True


def test_duplicate_records_are_refused(tmp_path):
    cells = [Cell("stat", "iid", "k1_null", 60)]
    run_study(cells, reps=2, chunk=2, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf")
    chunk = next(tmp_path.glob("*.jsonl.gz"))
    (tmp_path / "copy.jsonl.gz").write_bytes(chunk.read_bytes())
    with pytest.raises(ValueError, match="duplicate"):
        load_records(tmp_path)


def test_failed_calls_are_counted_and_do_not_enter_the_tables(tmp_path):
    record = {
        "cell": "stat/iid/k1_null/n60",
        "rep": 0,
        "k": 1,
        "true_support": False,
        "m": {"hac_rule": {"status": "failed", "error": "LinAlgError: x"}},
    }
    with gzip.open(tmp_path / "x.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + chr(10))
    frame = load_records(tmp_path)
    assert frame.empty and frame.attrs["failed"] == {("stat/iid/k1_null/n60", "hac_rule"): 1}


def test_validate_accepts_only_when_every_cell_passes(tiny, tmp_path, monkeypatch):
    cells = [
        Cell("stat", "iid", "k1_null", 100),
        Cell("stat", "iid", "k1_reversed", 100),
    ]
    run_study(
        cells,
        reps=40,
        chunk=40,
        seed_base=9001,
        out=tmp_path,
        jobs=1,
        stage="validate",
        names=("hac_default_bonf",),
    )
    freeze = {
        "method": "hac_default_bonf",
        "gate": None,
        "budget_reps": 40,
        "cells": [c.id for c in cells],
    }
    strict = validate(tmp_path, freeze)
    assert strict["verdict"] == "REJECTED"  # 40 reliable replicates are far below 1500
    assert set(strict["cells_without_enough_reliable"]) == {c.id for c in cells}

    monkeypatch.setattr(accept, "MIN_RELIABLE_VALIDATION", 10)
    monkeypatch.setattr(accept, "CEILING", 0.25)
    relaxed = validate(tmp_path, freeze)
    assert relaxed["verdict"] == "ACCEPTED" and relaxed["cells_failing"] == []

    # a cell that is expected but was never run: rejected, whatever the others say
    missing = validate(tmp_path, {**freeze, "cells": [*freeze["cells"], "stat/iid/k1_null/n250"]})
    assert missing["verdict"] == "REJECTED" and missing["missing_cells"] == [
        "stat/iid/k1_null/n250"
    ]
    # a budget that does not match the replicates found: rejected (incomplete)
    assert validate(tmp_path, {**freeze, "budget_reps": 50})["verdict"] == "REJECTED"


def test_cell_kinds_follow_the_protocol():
    from app.analysis.calib.accept import cell_kind

    assert cell_kind("stat/iid/k1_null/n100") == "acceptance"
    assert cell_kind("stat/sar12_05/k2_real_null/n250") == "acceptance"
    assert cell_kind("stat/hetero/k1_null/n100") == "acceptance"  # held-out family
    assert cell_kind("stat/seasonal_indep/k1_null/n100") == "gated"
    assert cell_kind("stat/sar12_08/k1_null/n250") == "gated"
    assert cell_kind("stat/ar09/k1_null/n100") == "report"


def test_validate_judges_gated_cells_by_leak_and_reliable_share(tmp_path, monkeypatch):
    cells = [Cell("stat", "seasonal_indep", "k1_null", 250)]
    run_study(
        cells,
        reps=40,
        chunk=40,
        seed_base=9001,
        out=tmp_path,
        jobs=1,
        stage="validate",
        names=("hac_default",),
    )
    base = {"method": "hac_default", "budget_reps": 40, "cells": [c.id for c in cells]}
    monkeypatch.setattr(accept, "LEAK_CEILING", 0.2)
    monkeypatch.setattr(accept, "GATED_RELIABLE_CAP", 0.2)
    shut = {
        "n_min": 10_000,
        "rho1_max": 9,
        "rho_seasonal_max": 9,
        "var_ratio_max": 1e9,
        "mean_shift_max": 1e9,
    }
    rejected_by_gate = validate(tmp_path, {**base, "gate": shut})
    assert (
        rejected_by_gate["verdict"] == "ACCEPTED"
    )  # the gate rejects everything: no leak, no reliable
    cell = rejected_by_gate["cells"][cells[0].id]
    assert cell["kind"] == "gated" and cell["reliable"] == 0 and cell["false_support"] == 0

    open_gate = {
        "n_min": 0,
        "rho1_max": 9,
        "rho_seasonal_max": 9,
        "var_ratio_max": 1e9,
        "mean_shift_max": 1e9,
    }
    leaking = validate(tmp_path, {**base, "gate": open_gate})
    assert leaking["verdict"] == "REJECTED"  # an open gate lets the seasonal windows through
    assert leaking["cells"][cells[0].id]["reliable"] > 20


def test_the_fast_gate_scan_equals_the_reference_evaluation(tmp_path):
    from app.analysis.calib.accept import PreparedMethod

    cells = [
        Cell("stat", "iid", "k1_null", 100),
        Cell("stat", "iid", "k1_real07", 100),
        Cell("stat", "iid", "k1_reversed", 100),
        Cell("stat", "seasonal_indep", "k1_null", 100),
        Cell("stat", "seasonal_indep", "k1_reversed", 100),
    ]
    run_study(cells, reps=60, chunk=60, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf")
    frame = load_records(tmp_path)
    method = frame[frame["method"] == "hac_rule_bonf"]
    prepared = PreparedMethod(method)
    inf = float("inf")
    gates = [
        GateThresholds(0, 9, 9, inf, inf),
        GateThresholds(36, 0.5, 0.4, 3.0, 1.0),
        GateThresholds(120, 0.3, 0.3, 2.0, 0.5),
        GateThresholds(60, 0.9, 0.35, 4.0, 2.0),
        GateThresholds(10_000, 9, 9, inf, inf),
    ]
    for gate in gates:
        slow = evaluate_gate(method, gate)
        fast = prepared.stats(gate)
        assert set(slow) == set(fast)
        for key in slow:
            assert math.isclose(slow[key], fast[key], rel_tol=1e-9, abs_tol=1e-12), (gate, key)


def test_tune_reports_both_passes_and_the_sar12_05_fallback(tmp_path):
    from app.analysis.calib.accept import tune

    cells = [
        Cell("stat", "iid", "k1_null", 100),
        Cell("stat", "sar12_05", "k1_null", 100),
        Cell("stat", "seasonal_indep", "k1_null", 100),
    ]
    run_study(cells, reps=10, chunk=10, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf")
    report = tune([tmp_path])
    assert report["selected"] is None  # far too few replicates and no k = 2, 3 cells
    assert set(report) >= {"pass1", "pass2_sar12_05_gated", "sar12_05_moved", "final"}
    assert "sar12_05" in report["pass2_sar12_05_gated"]["gated_families"]
    assert "sar12_05" not in report["pass2_sar12_05_gated"]["region_families"]
    assert report["sar12_05_moved"] is False


def test_attach_features_keeps_existing_values_and_fills_only_the_missing(tmp_path):
    import numpy as np

    from app.analysis.calib.accept import attach_features

    cells = [Cell("stat", "iid", "k1_null", 100)]
    full = tmp_path / "full"
    run_study(cells, reps=6, chunk=6, seed_base=1001, out=full, jobs=1, only="hac_rule_bonf")
    frame = load_records(full)
    expected = frame["rho_seasonal241"].to_numpy().copy()
    assert not np.isnan(expected).any()  # new records carry the feature natively
    old = frame.copy()
    old.loc[old["rep"] < 3, ["rho_seasonal241", "rho_seasonal242"]] = np.nan  # an "old" record
    attached = attach_features(old, full)  # the feature re-run is the same data
    assert np.allclose(attached["rho_seasonal241"].to_numpy(), expected)  # filled and kept
    tampered = old.copy()
    tampered.loc[:, "rho11"] = tampered["rho11"] + 0.5  # not the same data
    with pytest.raises(ValueError, match="regenerated data differ"):
        attach_features(tampered, full)


def test_the_report_tables_are_built_from_records(tmp_path):
    from app.analysis.calib.report import build_tables
    from app.analysis.gate import GateThresholds

    cells = [Cell("stat", "iid", "k1_null", 100), Cell("stat", "iid", "k1_real07", 100)]
    run_study(cells, reps=30, chunk=30, seed_base=1001, out=tmp_path, jobs=1, only="hac_rule_bonf")
    frame = with_composites(load_records(tmp_path))
    gate = GateThresholds(100, 1.01, 0.3, float("inf"), float("inf"), rho_seasonal24_max=0.2)
    text = build_tables(frame, "hac_rule_bonf", gate)
    for heading in (
        "## Yapısal olarak karar verilemeyen hücreler",
        "## Bölge ailelerinde yanlış destekleme",
        "## Kapının reddetmesi gereken aileler",
        "## Güvenilir işaretleme payı",
        "## Güç (gerçek ilişki",
    ):
        assert heading in text
    assert "| iid | 100 |" in text and "k1_real07" in text
