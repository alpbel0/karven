"""The experimental delivery defaults and the consumer-side rule for unreliable support."""

from __future__ import annotations

from app.analysis.__main__ import build_parser
from app.analysis.runner import EXPERIMENTAL_GATE, EXPERIMENTAL_METHOD, experimental_config
from app.graph.models import Hypothesis, RelationRecord, SeriesRef


def test_the_experimental_method_is_unvalidated_by_construction():
    assert EXPERIMENTAL_METHOD.calibrated is False
    assert EXPERIMENTAL_METHOD.block == 24 and EXPERIMENTAL_METHOD.replicates == 499
    config = experimental_config()
    assert config.gate is EXPERIMENTAL_GATE
    assert EXPERIMENTAL_GATE.n_min == 100  # the smallest size the tuning data cover
    assert EXPERIMENTAL_GATE.rho_seasonal_max == 0.3 and EXPERIMENTAL_GATE.rho_seasonal24_max == 0.2


def test_the_command_line_defaults_to_the_experimental_method():
    parser = build_parser()
    args = parser.parse_args(["run", "--target", "a|b", "--driver", "c|d:positive"])
    assert args.method == "experimental"
    hac = parser.parse_args(
        ["run", "--target", "a|b", "--driver", "c|d:positive", "--method", "hac"]
    )
    assert hac.method == "hac"


def _relation(status: str, reliable: bool | None) -> RelationRecord:
    target, driver = SeriesRef("tcmb", "T"), SeriesRef("tcmb", "D")
    return RelationRecord(
        key="k",
        target=target,
        drivers=(driver,),
        hypothesis=Hypothesis("m", {driver.key: "positive"}, 0, 1, "difference"),
        status=status,
        reliable=reliable,
    )


def test_unreliable_support_is_not_established_support():
    assert _relation("supported", True).is_reliable_support is True
    assert _relation("supported", False).is_reliable_support is False
    assert _relation("supported", None).is_reliable_support is False  # a fresh hypothesis
    assert _relation("unsupported", True).is_reliable_support is False
    assert _relation("periods_differ", True).is_reliable_support is False
    assert _relation("insufficient_data", False).is_reliable_support is False
