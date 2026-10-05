"""Unit tests for Jev dataset tagging (Task 2.3; no database, no network)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.catalog import tag
from app.catalog import tag_jev as jev
from app.catalog.enrich_rules import DatasetView
from app.catalog.tree import Concept, load_tree
from app.data.models import Dataset

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class _FakeJev:
    """Scripted noul answers keyed by question id; every call is recorded."""

    def __init__(
        self, branch: dict[str, float] | None = None, leaf: dict[str, float] | None = None
    ) -> None:
        self.branch = branch or {}
        self.leaf = leaf or {}
        self.calls: list[list[str]] = []

    def decide(self, state, questions, *, prompt_ref=None):  # noqa: ANN001
        self.calls.append(sorted(questions))
        answers: dict[str, dict] = {}
        for question_id in questions:
            if question_id in self.branch:
                score = self.branch[question_id]
            elif question_id in self.leaf:
                score = self.leaf[question_id]
            else:
                score = 0.0
            answers[question_id] = {"type": "noul", "noul": score}
        return {"answers": answers}


def _dataset(*, name: str = "Konut Fiyat Endeksi", cok: bool = False, attributes=None) -> Dataset:
    dataset = Dataset(institution_id=1, external_code="X", name=name)
    dataset.cok_konulu_derleme = cok
    dataset.source_category = "Kategori"
    dataset.attributes = attributes or {}
    return dataset


def _view() -> DatasetView:
    return DatasetView(
        institution_code="tuik",
        institution_name="TÜİK",
        external_code="X",
        name="Konut Fiyat Endeksi",
    )


BRANCH_BODIES = {"branch": "DAL {ad}: {tanim}{degildir}", "leaf": "YAPRAK {ad}: {tanim}{degildir}"}
REFS = {"branch": None, "leaf": None}


# --------------------------------------------------------------------------- #
# Decision truth table
# --------------------------------------------------------------------------- #


def test_one_two_three_candidates_are_all_accepted() -> None:
    tree = load_tree()
    leaves = tree.branch_leaves["konut_insaat_gayrimenkul"]
    for count in (1, 2, 3):
        scores = {leaf: 0.9 - index * 0.1 for index, leaf in enumerate(leaves[:count])}
        decision = tag.decide_tags(
            branch_scores={"konut_insaat_gayrimenkul": 0.9},
            leaf_scores=scores,
            cok_konulu=False,
            tree=tree,
        )
        assert len(decision.rows) == count
        assert all(row.status == "accepted" and row.level == "leaf" for row in decision.rows)
        assert all(row.confidence == scores[row.tag_id] for row in decision.rows)


def test_more_than_three_candidates_cut_are_rejected_with_rank() -> None:
    tree = load_tree()
    leaves = tree.branch_leaves["konut_insaat_gayrimenkul"]
    scores = {leaf: 0.9 for leaf in leaves[:5]}
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.9},
        leaf_scores=scores,
        cok_konulu=False,
        tree=tree,
    )
    accepted = [row for row in decision.rows if row.status == "accepted"]
    rejected = [row for row in decision.rows if row.status == "rejected"]
    assert len(accepted) == 3
    assert [row.tag_id for row in accepted] == list(leaves[:3])  # ties by tree order
    assert [row.note for row in rejected] == ["kesildi: 4. sıra", "kesildi: 5. sıra"]
    assert all(row.confidence == 0.9 for row in rejected)


def test_zero_candidates_no_gap_reviews_best_leaf() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.5},
        leaf_scores={"konut_fiyatlari": 0.55},
        cok_konulu=False,
        tree=tree,
    )
    assert [row.status for row in decision.rows] == ["review"]
    assert decision.rows[0].note == "yaprak yok"
    assert "bosluk" not in decision.reasons


def test_zero_candidates_below_branch_pass_flags_gap() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.3},
        leaf_scores={"konut_fiyatlari": 0.3},
        cok_konulu=False,
        tree=tree,
    )
    assert decision.rows[0].note == "boşluk: en yüksek yaprak 0.30"
    assert "bosluk" in decision.reasons


def test_zero_candidates_with_high_branch_flags_dal_yaprak_yok() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.8},
        leaf_scores={"konut_fiyatlari": 0.5},
        cok_konulu=False,
        tree=tree,
    )
    branch_rows = [row for row in decision.rows if row.level == "branch"]
    assert len(branch_rows) == 1
    assert branch_rows[0].tag_id == "konut_insaat_gayrimenkul"
    assert branch_rows[0].note == "dal yüksek, yaprak yok"
    assert "dal_yaprak_yok" in decision.reasons


def test_cok_konulu_takes_branch_tags_only() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={
            "para_kredi_bankacilik": 0.7,
            "fiyatlar_enflasyon": 0.65,
            "doviz_kurlari": 0.3,
        },
        leaf_scores={},
        cok_konulu=True,
        tree=tree,
    )
    assert [row.tag_id for row in decision.rows] == [
        "para_kredi_bankacilik",
        "fiyatlar_enflasyon",
    ]
    assert all(row.level == "branch" and row.status == "accepted" for row in decision.rows)


def test_cok_konulu_without_branch_reviews_best_branch() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"fiyatlar_enflasyon": 0.5},
        leaf_scores={},
        cok_konulu=True,
        tree=tree,
    )
    assert decision.rows[0].level == "branch"
    assert decision.rows[0].note == "yaprak yok"
    assert "bosluk" not in decision.reasons


def test_category_hint_mismatch_moves_rows_to_review() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.9},
        leaf_scores={"konut_fiyatlari": 0.9},
        cok_konulu=False,
        hint_branches=frozenset({"fiyatlar_enflasyon"}),
        tree=tree,
    )
    assert decision.rows[0].status == "review"
    assert decision.rows[0].note == "kategori ipucu uyuşmuyor: ipucu=fiyatlar_enflasyon"
    assert "kategori_uyusmazligi" in decision.reasons


def test_category_hint_match_leaves_rows_accepted() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.9},
        leaf_scores={"konut_fiyatlari": 0.9},
        cok_konulu=False,
        hint_branches=frozenset({"konut_insaat_gayrimenkul", "fiyatlar_enflasyon"}),
        tree=tree,
    )
    assert decision.rows[0].status == "accepted"


def test_absent_hint_runs_no_check() -> None:
    tree = load_tree()
    accepted = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.9},
        leaf_scores={"konut_fiyatlari": 0.9},
        cok_konulu=False,
        hint_branches=None,
        tree=tree,
    )
    assert accepted.rows[0].status == "accepted"
    assert "kategori_uyusmazligi" not in accepted.reasons


def test_manual_rows_are_protected() -> None:
    tree = load_tree()
    decision = tag.decide_tags(
        branch_scores={"konut_insaat_gayrimenkul": 0.9},
        leaf_scores={"konut_fiyatlari": 0.9},
        cok_konulu=False,
        has_manual=True,
        tree=tree,
    )
    assert decision.skipped is True
    assert decision.rows == ()


# --------------------------------------------------------------------------- #
# Question builders
# --------------------------------------------------------------------------- #


def test_branch_questions_put_the_branch_name_in_instructions() -> None:
    tree = load_tree()
    questions = jev.branch_questions(tree, BRANCH_BODIES["branch"])
    assert set(questions) == set(tree.branch_ids)
    assert len(questions) == 24
    for branch_id, question in questions.items():
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"evet", "hayır"}
        assert tree.branches[branch_id].ad in question["instructions"]


def test_leaf_question_carries_degildir_text() -> None:
    concept = Concept(
        id="x",
        ad="Test dalı",
        tanim="Tanım",
        degildir=(("y", "Şu değildir gerekçesi"),),
    )
    question = jev.leaf_question(BRANCH_BODIES["leaf"], concept)
    assert "Şunlar bu etiket değildir" in question["instructions"]
    assert "Şu değildir gerekçesi" in question["instructions"]


def test_real_prompt_files_build_questions() -> None:
    branch_body = (jev.PROMPT_DIR / "tag_branch.md").read_text(encoding="utf-8")
    concept = load_tree().branches["fiyatlar_enflasyon"]
    question = jev.branch_question(branch_body, concept)
    assert concept.ad in question["instructions"]
    assert concept.tanim in question["instructions"]


def test_leaf_questions_only_for_passing_branches() -> None:
    tree = load_tree()
    passing = ["konut_insaat_gayrimenkul"]
    questions = jev.leaf_questions(tree, BRANCH_BODIES["leaf"], passing)
    assert set(questions) == set(tree.branch_leaves["konut_insaat_gayrimenkul"])
    assert "tuketici_fiyatlari" not in questions  # not in a passing branch


def test_question_chunking_at_forty() -> None:
    tree = load_tree()
    all_leaves = {
        leaf_id: {"type": "noul", "instructions": leaf_id, "criteria": {}}
        for leaf_id in tree.leaf_order
    }
    assert len(tree.leaf_order) == 109
    chunks = jev.chunk_questions(all_leaves)
    assert [len(chunk) for chunk in chunks] == [40, 40, 29]
    assert len(jev.chunk_questions(jev.branch_questions(tree, "x"))) == 1


def test_category_state_has_no_dataset_name() -> None:
    state = jev.build_category_state("tcmb", "Fiyatlar > TÜFE")
    assert set(state) == {"kurum", "kategori"}


# --------------------------------------------------------------------------- #
# Two-stage scoring and Jev-free refresh
# --------------------------------------------------------------------------- #


def _score_fixture() -> tuple[_FakeJev, dict[str, float], dict[str, float], tag.Decision]:
    tree = load_tree()
    fake = _FakeJev(
        branch={"konut_insaat_gayrimenkul": 0.95},
        leaf={"konut_fiyatlari": 0.92, "konut_satislari": 0.65},
    )
    branch_scores, leaf_scores, decision = jev.score_dataset(
        decider=fake,
        tree=tree,
        dataset=_dataset(),
        view=_view(),
        institution_code="tuik",
        bodies=BRANCH_BODIES,
        refs=REFS,
    )
    return fake, branch_scores, leaf_scores, decision


def test_full_dataset_scores_both_stages() -> None:
    tree = load_tree()
    fake, branch_scores, leaf_scores, decision = _score_fixture()
    assert len(fake.calls) == 2
    assert set(fake.calls[0]) == set(tree.branch_ids)  # one call for 24 branches
    assert set(fake.calls[1]) == set(tree.branch_leaves["konut_insaat_gayrimenkul"])
    assert branch_scores["konut_insaat_gayrimenkul"] == pytest.approx(0.95)
    assert leaf_scores["konut_fiyatlari"] == pytest.approx(0.92)
    assert [row.tag_id for row in decision.rows if row.status == "accepted"] == [
        "konut_fiyatlari",
        "konut_satislari",
    ]


def test_cok_konulu_skips_the_leaf_stage() -> None:
    tree = load_tree()
    fake = _FakeJev(branch={"para_kredi_bankacilik": 0.7})
    branch_scores, leaf_scores, decision = jev.score_dataset(
        decider=fake,
        tree=tree,
        dataset=_dataset(cok=True),
        view=_view(),
        institution_code="tcmb",
        bodies=BRANCH_BODIES,
        refs=REFS,
    )
    assert len(fake.calls) == 1  # branch only
    assert leaf_scores == {}
    assert decision.rows[0].level == "branch"


def test_refresh_decides_without_calling_jev() -> None:
    _fake, branch_scores, leaf_scores, decision = _score_fixture()
    assert any(row.status == "accepted" for row in decision.rows)
    calls_before = len(_fake.calls)

    stricter = tag.decide_tags(
        branch_scores=branch_scores,
        leaf_scores=leaf_scores,
        cok_konulu=False,
        leaf_accept=0.95,
        tree=load_tree(),
    )
    assert all(row.status != "accepted" for row in stricter.rows)
    assert len(_fake.calls) == calls_before  # refresh never talks to Jev


# --------------------------------------------------------------------------- #
# Tree loader extension
# --------------------------------------------------------------------------- #


def test_tree_loader_exposes_names_definitions_and_degildir() -> None:
    tree = load_tree()
    assert tree.branch_order and tree.leaf_order
    assert set(tree.branch_order) == set(tree.branch_ids)
    assert set(tree.leaf_order) == set(tree.leaf_ids)
    for position, branch_id in enumerate(tree.branch_order):
        concept = tree.branches[branch_id]
        assert concept.ad and concept.tanim
        assert concept.degildir
        assert tree.branch_position[branch_id] == position
        assert tree.branch_leaves[branch_id]
    for position, leaf_id in enumerate(tree.leaf_order):
        concept = tree.leaves[leaf_id]
        assert concept.ad and concept.tanim
        assert tree.leaf_position[leaf_id] == position
    leaf = tree.leaf_order[0]
    assert tree.branch_of(leaf) == tree.leaf_to_branch[leaf]
    assert tree.branch_of("fiyatlar_enflasyon") == "fiyatlar_enflasyon"


# --------------------------------------------------------------------------- #
# Hints
# --------------------------------------------------------------------------- #


def test_hints_regeneration_keeps_manual_entries() -> None:
    existing = {
        "tuik|Elle": {"branches": {"fiyatlar_enflasyon": 0.9}, "manual": True},
        "tcmb|Eski": {"branches": {"doviz_kurlari": 0.5}, "manual": False},
    }
    generated = {
        "tuik|Elle": {"branches": {"konut_insaat_gayrimenkul": 0.8}, "manual": False},
        "tcmb|Yeni": {"branches": {"para_kredi_bankacilik": 0.7}, "manual": False},
    }
    merged = tag.merge_hints(existing, generated)
    assert merged["tuik|Elle"]["manual"] is True
    assert merged["tuik|Elle"]["branches"] == {"fiyatlar_enflasyon": 0.9}
    assert "tcmb|Eski" not in merged  # non-manual stale entry is dropped
    assert merged["tcmb|Yeni"]["branches"] == {"para_kredi_bankacilik": 0.7}


def test_hints_written_sorted_and_stable(tmp_path: Path) -> None:
    merged = {
        "tuik|B": {"branches": {"b": 0.5, "a": 0.6}, "manual": False},
        "tcmb|A": {"branches": {"c": 0.7}, "manual": False},
    }
    path = tmp_path / "hints.yaml"
    tag.write_hints(path, merged)
    first = path.read_text(encoding="utf-8")
    tag.write_hints(path, merged)
    assert path.read_text(encoding="utf-8") == first  # stable bytes
    loaded = yaml.safe_load(first)
    assert list(loaded) == ["tcmb|A", "tuik|B"]  # sorted top-level keys
    assert tag._read_hints(path)["tuik|B"]["branches"]["a"] == pytest.approx(0.6)


def test_resolve_include_accepts_external_code_or_identity() -> None:
    identities = {"tuik:ABC": ("tuik", False), "tcmb:XYZ": ("tcmb", False)}
    assert tag._resolve_include(identities, ["ABC"]) == ["tuik:ABC"]
    assert tag._resolve_include(identities, ["tuik:ABC", "XYZ"]) == ["tuik:ABC", "tcmb:XYZ"]
    with pytest.raises(ValueError):
        tag._resolve_include(identities, ["NOPE"])


# --------------------------------------------------------------------------- #
# CLI boot
# --------------------------------------------------------------------------- #


def test_cli_help_lists_subcommands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.catalog.tag", "--help"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    for command in ("run", "hints", "sample", "report", "review-list", "refresh", "set"):
        assert command in result.stdout
