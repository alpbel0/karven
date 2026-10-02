"""Unit tests for the secimdagitimapp election connector (no network; fixtures).

Fixtures are real reports trimmed to a few rows (captured live 2026-10-01/02)
for the four distinct report shapes: province/district (table 1), candidates
(table 4), seat counts (table 5) and the national summary (table 7).
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.connectors.base import (
    NOT_FOUND,
    ROLE_GEO,
    ROLE_OTHER,
    ROLE_TIME,
    SOURCE_ERROR,
    ConnectorError,
)
from app.connectors.tuik import __main__ as tuik_cli
from app.connectors.tuik.__main__ import build_parser
from app.connectors.tuik.secim import (
    DIM_ADAY,
    DIM_CEVRE,
    DIM_ILCE,
    DIM_MEASURE,
    DIM_PARTI,
    DIM_REGION,
    DIM_YERLESIM,
    M_ELECTED,
    M_KAYITLI,
    M_MILLETVEKILI,
    M_SANDIK,
    M_VOTE_SHARE,
    M_VOTES,
    TABLE_LIST_HEADER,
    TABLE_SPEC_BY_INDEX,
    TOTAL_CODE,
    YEAR_RADIOS,
    ReportPlan,
    SecimConnector,
    WalkResult,
    _discovery_elections,
    _election_sort_key,
    _visible_year_radios,
    distinct_series_count,
    election_date,
    interpret_candidates,
    interpret_generic,
    interpret_national,
    interpret_report,
    interpret_seats,
    walk_form,
)
from app.connectors.tuik.zk import ListItem, ReportGrid, parse_report_grid

FIXTURES = Path(__file__).parent / "fixtures" / "tuik" / "secim"

CEVRE = TABLE_SPEC_BY_INDEX[1]
ADAY = TABLE_SPEC_BY_INDEX[4]
SEATS = TABLE_SPEC_BY_INDEX[5]
NATIONAL = TABLE_SPEC_BY_INDEX[7]

# Parties present in the trimmed candidate fixture (Adana, 2023).
PARTIES = (
    "AK PARTİ",
    "CHP",
    "HAK-PAR",
    "İYİ PARTİ",
    "TKP",
    "AB",
    "SOL PARTİ",
    "TKH",
    "YEŞİL SOL PARTİ",
)


def _grid(table: int):
    return parse_report_grid((FIXTURES / f"table{table}.report.html").read_bytes())


def _series(series, **codes):
    for entry in series:
        if all(entry.codes.get(dimension) == value for dimension, value in codes.items()):
            return entry
    raise AssertionError(f"no series matching {codes}")


# --- election dates -------------------------------------------------------


def test_election_date_from_report_title() -> None:
    grid = _grid(1)
    assert grid.title.startswith("Seçim çevresi ve ilçelere göre 14 Mayıs 2023")
    assert election_date(grid.title) == date(2023, 5, 14)
    assert election_date("2023 Milletvekili Genel Seçimi (14 Mayıs)") == date(2023, 5, 14)
    assert election_date("2015 seçimi (1 Kasım)") == date(2015, 11, 1)
    assert election_date("2015 seçimi (7 Haziran)") == date(2015, 6, 7)
    assert election_date("1991 seçimi") == date(1991, 10, 20)


def test_election_date_resolves_every_label_style() -> None:
    # Radio labels ("… seçimi") and list labels ("YYYY (Gün Ay)").
    assert election_date("2015 seçimi (1 Kasım)") == date(2015, 11, 1)
    assert election_date("2015 seçimi (7 Haziran)") == date(2015, 6, 7)
    assert election_date("2015 (1 Kasım)") == date(2015, 11, 1)
    assert election_date("2015 (7 Haziran)") == date(2015, 6, 7)
    assert election_date("1991 seçimi") == date(1991, 10, 20)
    assert election_date("1991") == date(1991, 10, 20)
    assert election_date("2023-05-14") == date(2023, 5, 14)


def test_election_date_never_invents_a_date() -> None:
    # 2015 had two elections: a bare year must not collapse them to Jan 1.
    assert election_date("2015") is None
    assert election_date("") is None
    assert election_date("bilinmeyen dönem") is None


def test_report_plan_periods_distinguish_two_elections_in_a_year() -> None:
    november = ReportPlan(ADAY.code, ADAY, "2015 seçimi (1 Kasım)")
    june = ReportPlan(ADAY.code, ADAY, "2015 seçimi (7 Haziran)")
    assert november.period == date(2015, 11, 1)
    assert june.period == date(2015, 6, 7)
    assert november.period != june.period


def test_build_report_rejects_an_ambiguous_election() -> None:
    plan = ReportPlan(ADAY.code, ADAY, "2015")
    assert plan.period is None
    connector = SecimConnector(store=None)
    with pytest.raises(ConnectorError) as excinfo:
        connector.build_report(plan)
    assert "2015" in str(excinfo.value)


def test_build_report_warns_when_the_title_date_disagrees(monkeypatch, caplog) -> None:
    import app.connectors.tuik.secim as secim

    def fake_walk_form(client, spec, *, election=None, mode=M_VOTES, generate=False, budget=250):
        return WalkResult(grid=_grid(1), years=[election] if election else [])

    monkeypatch.setattr(secim, "walk_form", fake_walk_form)
    connector = SecimConnector(
        store=None, client_factory=lambda dataset: SimpleNamespace(close=lambda: None)
    )
    plan = ReportPlan(CEVRE.code, CEVRE, "2018")
    with caplog.at_level(logging.WARNING, logger="app.connectors.tuik.secim"):
        series = connector.build_report(plan)
    assert series
    assert any("report title" in record.getMessage() for record in caplog.records)

    offered = [
        "2023",
        "2018",
        "2015 seçimi (7 Haziran)",
        "2015 seçimi (1 Kasım)",
        "2011 seçimi",
    ]
    assert _discovery_elections(CEVRE, 2, offered) == ["2023", "2018"]
    assert _discovery_elections(CEVRE, None, offered) == ["2023"]
    assert _discovery_elections(CEVRE, 99, offered) == [
        "2023",
        "2018",
        "2015 seçimi (1 Kasım)",
        "2015 seçimi (7 Haziran)",
        "2011 seçimi",
    ]
    assert _election_sort_key("2023") > _election_sort_key("2018")


def test_discovery_elections_never_plans_unavailable_elections() -> None:
    offered = [
        "2023",
        "2018",
        "2015 seçimi (7 Haziran)",
        "2015 seçimi (1 Kasım)",
        "2011 seçimi",
    ]
    plans = _discovery_elections(CEVRE, 99, offered)
    assert "2007 seçimi" not in plans
    assert "2015 seçimi (7 Haziran)" in plans


def test_dataset_meta_time_period_is_the_tables_own_elections() -> None:
    result = WalkResult()
    for label in (
        "2023",
        "2018",
        "2015 seçimi (7 Haziran)",
        "2015 seçimi (1 Kasım)",
        "2011 seçimi",
    ):
        result.years.append(label)
    meta = SecimConnector(store=None)._dataset_meta(TABLE_SPEC_BY_INDEX[6], result)
    time_dimension = next(d for d in meta.dimensions if d.code == "TIME_PERIOD")
    assert [code.code for code in time_dimension.codes] == [
        "2011-06-12",
        "2015-06-07",
        "2015-11-01",
        "2018-06-24",
        "2023-05-14",
    ]
    assert meta.attributes["elections"] == list(result.years)


def test_visible_year_radios_captures_only_the_offered_elections() -> None:
    offered = {
        "2023",
        "2018",
        "2015 seçimi (7 Haziran)",
        "2015 seçimi (1 Kasım)",
        "2011 seçimi",
    }
    client = SimpleNamespace(
        radios={label: SimpleNamespace(id=f"r{i}") for i, label in enumerate(YEAR_RADIOS)},
        _visible={f"r{i}": label in offered for i, label in enumerate(YEAR_RADIOS)},
    )
    assert _visible_year_radios(client) == [label for label in YEAR_RADIOS if label in offered]


class _UnavailableElectionClient:
    """A form whose year radio exists but whose server rejects the report."""

    listboxes = {"tbl"}
    radios = {"2023": SimpleNamespace(id="r0")}
    _visible = {"r0": True}

    def bootstrap(self) -> None:
        pass

    def list_header(self, box_id: str) -> str:
        return TABLE_LIST_HEADER if box_id == "tbl" else ""

    def list_items(self, box_id: str) -> list[ListItem]:
        return [ListItem(id="i0", label=NATIONAL.table_label, cells=())]

    def select_items(self, box_id: str, item_ids: list[str]) -> None:
        pass

    def check_radio(self, label: str) -> None:
        pass

    def visible_listboxes(self) -> list[str]:
        return []

    def generate_report(self) -> tuple[bytes, str]:
        raise ConnectorError(SOURCE_ERROR, "HATALI==> Seçim yılı seçiniz !!!")

    def take_raw_keys(self) -> list[str]:
        return []


def test_walk_form_reports_an_unavailable_election_as_not_found() -> None:
    with pytest.raises(ConnectorError) as excinfo:
        walk_form(_UnavailableElectionClient(), NATIONAL, election="2023", generate=True)
    assert excinfo.value.kind == NOT_FOUND


# --- table 1: province / district matrix ----------------------------------


def test_interpret_generic_table1_carries_geography_and_reads_measures() -> None:
    series, uninterpreted = interpret_generic(_grid(1), CEVRE, period=election_date("2023"))
    assert uninterpreted == []
    assert len(series) == 136

    party = _series(
        series,
        **{
            DIM_CEVRE: "Adana",
            DIM_ILCE: TOTAL_CODE,
            DIM_YERLESIM: TOTAL_CODE,
            DIM_PARTI: "AK PARTİ",
            DIM_MEASURE: M_VOTES,
        },
    )
    assert party.points == [(date(2023, 5, 14), Decimal("412978"))]

    sandik = _series(
        series,
        **{
            DIM_CEVRE: "Adana",
            DIM_ILCE: TOTAL_CODE,
            DIM_YERLESIM: TOTAL_CODE,
            DIM_PARTI: TOTAL_CODE,
            DIM_MEASURE: M_SANDIK,
        },
    )
    assert sandik.points == [(date(2023, 5, 14), Decimal("4785"))]

    # The district name is carried into the following "İl/İlçe merkezi" row.
    seyhan = _series(
        series,
        **{
            DIM_CEVRE: "Adana",
            DIM_ILCE: "Seyhan",
            DIM_YERLESIM: TOTAL_CODE,
            DIM_PARTI: TOTAL_CODE,
            DIM_MEASURE: M_SANDIK,
        },
    )
    assert seyhan.points == [(date(2023, 5, 14), Decimal("1565"))]


# --- table 4: candidates --------------------------------------------------


def test_interpret_candidates_reads_party_headers_and_elected_flag() -> None:
    series, uninterpreted = interpret_candidates(
        _grid(4), ADAY, period=election_date("2023"), parties=PARTIES
    )
    assert uninterpreted == []
    assert distinct_series_count(series) == 15

    elected = _series(
        series,
        **{
            DIM_CEVRE: "ADANA",
            DIM_PARTI: "AK PARTİ",
            DIM_ADAY: "Ömer Çelik",
            DIM_MEASURE: M_ELECTED,
        },
    )
    assert elected.points == [(date(2023, 5, 14), Decimal(1))]

    not_elected = _series(
        series,
        **{
            DIM_CEVRE: "ADANA",
            DIM_PARTI: "AK PARTİ",
            DIM_ADAY: "Mustafa Yıldız",
            DIM_MEASURE: M_ELECTED,
        },
    )
    assert not_elected.points == [(date(2023, 5, 14), Decimal(0))]


def test_interpret_candidates_without_party_list_keeps_elected_only() -> None:
    series, _ = interpret_candidates(_grid(4), ADAY, period=election_date("2023"), parties=())
    assert all(entry.points[0][1] == 1 for entry in series)


def test_interpret_candidates_reads_abbreviated_party_headers() -> None:
    """1991 labels parties by abbreviation ("ANAP") that the form list lacks."""
    grid = ReportGrid(
        rows=[
            ["Milletvekili Genel Seçimine katılan adaylar", "", ""],
            ["(*) Kazandı", "", ""],
            ["1991", "", "Adana"],
            ["", "", "1 nolu seçim çevresi"],
            ["", "", "ANAP"],
            ["", "", "Ersin Koçak"],
            ["", "*", "Elected Person"],
            ["", "", "DYP"],
            ["", "", "M.Halit Dağlı"],
        ],
        encoding="utf-8",
        title="Milletvekili Genel Seçimine katılan adaylar",
    )
    series, _ = interpret_candidates(
        grid,
        ADAY,
        period=date(1991, 10, 20),
        parties=("ANAVATAN PARTİSİ", "DOĞRU YOL PARTİSİ"),
    )
    elected = _series(
        series,
        **{DIM_PARTI: "ANAP", DIM_ADAY: "Elected Person", DIM_MEASURE: M_ELECTED},
    )
    assert elected.points == [(date(1991, 10, 20), Decimal(1))]
    candidate = _series(
        series,
        **{DIM_PARTI: "ANAP", DIM_ADAY: "Ersin Koçak", DIM_MEASURE: M_ELECTED},
    )
    assert candidate.points == [(date(1991, 10, 20), Decimal(0))]


# --- table 5: seat counts -------------------------------------------------


def test_interpret_seats_table5_reads_party_rows() -> None:
    series, uninterpreted = interpret_seats(_grid(5), SEATS, period=election_date("2023"))
    assert uninterpreted == []
    assert len(series) == 5
    akp = _series(series, **{DIM_PARTI: "AK PARTİ", DIM_MEASURE: M_MILLETVEKILI})
    assert akp.codes[DIM_CEVRE] == "ADANA"
    assert akp.points == [(date(2023, 5, 14), Decimal("5"))]
    chp = _series(series, **{DIM_PARTI: "CHP", DIM_MEASURE: M_MILLETVEKILI})
    assert chp.points == [(date(2023, 5, 14), Decimal("5"))]


def test_interpret_seats_reads_the_2015_month_column() -> None:
    """From 2015 the seats report adds a month column; the party shifts right."""
    grid = ReportGrid(
        rows=[
            ["Seçim yılına göre siyasi parti ve bağımsızların çıkardığı madde", "", "", "", ""],
            ["", "", "", "Parti Adı", "Milletvekili sayısı"],
            ["2015", "(1 Kasım)", "Adana", "AK PARTİ", "11"],
            ["", "", "", "CHP", "8"],
        ],
        encoding="utf-8",
        title="Seçim yılına göre siyasi parti ve bağımsızların çıkardığı madde",
    )
    series, uninterpreted = interpret_seats(grid, SEATS, period=date(2015, 11, 1))
    assert uninterpreted == []
    akp = _series(series, **{DIM_PARTI: "AK PARTİ", DIM_MEASURE: M_MILLETVEKILI})
    assert akp.codes[DIM_CEVRE] == "Adana"
    assert akp.points == [(date(2015, 11, 1), Decimal("11"))]
    chp = _series(series, **{DIM_PARTI: "CHP", DIM_MEASURE: M_MILLETVEKILI})
    assert chp.points == [(date(2015, 11, 1), Decimal("8"))]


# --- table 7: national summary --------------------------------------------


def test_interpret_national_table7_counts_and_percent_rows() -> None:
    series, uninterpreted = interpret_national(_grid(7), NATIONAL, period=election_date("2023"))
    assert uninterpreted == []
    assert len(series) == 54

    registered = _series(
        series, **{DIM_REGION: TOTAL_CODE, DIM_PARTI: TOTAL_CODE, DIM_MEASURE: M_KAYITLI}
    )
    assert registered.points == [(date(2023, 5, 14), Decimal("64145504"))]

    votes = _series(series, **{DIM_REGION: TOTAL_CODE, DIM_PARTI: "AK PARTİ", DIM_MEASURE: M_VOTES})
    assert votes.points == [(date(2023, 5, 14), Decimal("19392462"))]

    share = _series(
        series, **{DIM_REGION: TOTAL_CODE, DIM_PARTI: "AK PARTİ", DIM_MEASURE: M_VOTE_SHARE}
    )
    assert share.points == [(date(2023, 5, 14), Decimal("35.6"))]


def test_interpret_report_dispatches_to_the_right_interpreter() -> None:
    series = interpret_report(_grid(5), SEATS, period=election_date("2023"))
    assert series and all(entry.dataset == "TUIK_SECIM_MILLETVEKILI_SAYISI" for entry in series)


# --- dataset metadata -----------------------------------------------------


def test_dataset_meta_shape() -> None:
    result = WalkResult()
    result.add(DIM_CEVRE, "Adana")
    result.add_total(DIM_CEVRE)
    result.add(DIM_ILCE, "Seyhan")
    result.add_total(DIM_ILCE)
    result.add(DIM_YERLESIM, "İl/İlçe merkezi")
    result.add_total(DIM_YERLESIM)
    meta = SecimConnector(store=None)._dataset_meta(CEVRE, result)
    assert meta.external_code == "TUIK_SECIM_CEVRE_ILCE"
    assert meta.attributes["channel"] == "secimdagitimapp"
    assert meta.attributes["default_frequency"] == "irregular"
    dimensions = {dimension.code: dimension for dimension in meta.dimensions}
    assert set(dimensions) == {
        "TIME_PERIOD",
        DIM_CEVRE,
        DIM_ILCE,
        DIM_YERLESIM,
        DIM_PARTI,
        DIM_MEASURE,
    }
    assert dimensions["TIME_PERIOD"].role == ROLE_TIME
    assert dimensions[DIM_CEVRE].role == ROLE_GEO
    assert dimensions[DIM_CEVRE].codes[0].code == TOTAL_CODE
    assert dimensions[DIM_PARTI].role == ROLE_OTHER
    assert dimensions[DIM_PARTI].attributes["source"] == "report"
    assert (meta.coverage_start, meta.coverage_end) == (date(1950, 5, 14), date(2023, 5, 14))


# --- CLI ------------------------------------------------------------------


def test_secim_parser_flags() -> None:
    catalog = build_parser().parse_args(["secim-catalog", "--dry-run", "--table", "1"])
    assert catalog.command == "secim-catalog"
    assert catalog.dry_run is True
    assert catalog.table == 1
    discover = build_parser().parse_args(
        ["secim-discover", "--dry-run", "--table", "4", "--elections", "2"]
    )
    assert discover.command == "secim-discover"
    assert discover.table == 4
    assert discover.elections == 2


def test_secim_routes_to_run_secim(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(args, dry_run):
        calls.append(args.command)
        return 0

    monkeypatch.setattr(tuik_cli, "_run_secim", fake_run)
    assert tuik_cli.main(["secim-catalog", "--dry-run"]) == 0
    assert tuik_cli.main(["fetch", "--dataset", "TUIK_SECIM_CEVRE", "--dry-run"]) == 0
    assert calls == ["secim-catalog", "fetch"]


def test_record_report_registers_every_series_codes(monkeypatch) -> None:
    """The shared loader registers each series' codes and writes observations."""
    from types import SimpleNamespace

    from app.connectors.tuik import __main__ as cli

    calls: list[dict[str, str]] = []

    def fake_upsert(session, dataset, codes):
        calls.append(dict(codes))
        return {"PARTI": 1}

    monkeypatch.setattr(cli, "upsert_discovered_codes", fake_upsert)
    monkeypatch.setattr(cli, "_load_dataset", lambda *a, **k: SimpleNamespace(id=1))
    monkeypatch.setattr(cli, "_record_headline", lambda *a, **k: 0)
    series = [
        SimpleNamespace(codes={DIM_PARTI: "AK PARTİ"}),
        SimpleNamespace(codes={DIM_PARTI: "CHP"}),
    ]
    plan = SimpleNamespace(dataset_code="TUIK_SECIM_CEVRE_ILCE")
    _, added = cli._record_report(None, None, plan, series, 1)
    assert [c[DIM_PARTI] for c in calls] == ["AK PARTİ", "CHP"]
    assert added == {"PARTI": 2}
