"""Markdown tables of the measured error profile of one method (docs/calibration/RESULT-tables.md).

Everything is computed from the tuning records (nothing is re-run) for ONE method under ONE gate,
on the final two-window decisions: a replicate counts as reliable only if it was decided and BOTH
windows pass the gate. The tables are descriptive: they are tuning data, not validation evidence.

    python -m app.analysis.calib.report --dir ... --features ... --method chain_24 --out ...
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from app.analysis.calib import accept
from app.analysis.calib.dgp import GATED_FAMILIES, REGION_FAMILIES
from app.analysis.gate import GateThresholds


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def build_tables(frame: pd.DataFrame, method: str, gate: GateThresholds) -> str:
    part = frame[frame["method"] == method]
    stat = part[(part["layer"] == "stat") & part["n"].isin(accept.REGION_SIZES)]
    table = accept.cell_table(stat, gate)
    table["share"] = table["reliable"] / table["reps"]
    lines: list[str] = []

    structural = accept.cell_table(stat, GateThresholds(0, 9, 9, float("inf"), float("inf")))
    impossible = sorted(
        {(c.split("/")[2], c.split("/")[3]) for c in structural[structural["decided"] == 0].index}
    )
    lines += [
        "## Yapısal olarak karar verilemeyen hücreler",
        "",
        "Her tekrarın `veri yetersiz` olduğu (en az gözlem kuralı) yapı/boyut çiftleri: "
        + ", ".join(f"`{s}` n={n[1:]}" for s, n in impossible)
        + ". Bunlar yöntem zayıflığı değil, motorun `eşik × sürücü sayısı` kuralının sonucudur "
        "(üç sürücülü regresyon için aylıkta 108 gözlem).",
        "",
    ]

    fs = table[~table["true_support"] & table["family"].isin(REGION_FAMILIES)].copy()
    fs["rate"] = fs["false_support"] / fs["reliable"].clip(lower=1)
    lines += [
        "## Bölge ailelerinde yanlış destekleme (nihai iki-pencere kararı, yalnız "
        "güvenilir tekrarlar)",
        "",
        "Hücre = (aile, yapı, n). `toplu` = hata toplamı / güvenilir toplamı; `en kötü` "
        "= en yüksek tek "
        "hücre oranı (en az 200 güvenilir tekrarı olan hücreler). Karar kuralı iki "
        "taraflı p < 0,05 "
        "+ doğru işaret; tek bir sabit testin ideali ~%2,5.",
        "",
        "| aile | n | güvenilir pay | toplu yanlış destekleme | en kötü hücre | hücre sayısı |",
        "|---|---|---|---|---|---|",
    ]
    for (family, n), group in fs.groupby(["family", "n"]):
        enough = group[group["reliable"] >= 200]
        worst = (
            (enough["false_support"] / enough["reliable"]).max() if len(enough) else float("nan")
        )
        pooled = group["false_support"].sum() / max(group["reliable"].sum(), 1)
        share = group["reliable"].sum() / group["reps"].sum()
        lines.append(
            f"| {family} | {n} | {_pct(share)} | {_pct(pooled)} | "
            f"{_pct(worst) if worst == worst else 'yetersiz'} | {len(group)} |"
        )

    gt = table[~table["true_support"] & table["family"].isin(GATED_FAMILIES)].copy()
    gt["leak"] = gt["false_support"] / gt["reps"]
    lines += [
        "",
        "## Kapının reddetmesi gereken aileler",
        "",
        "`sızıntı` = (güvenilir ve yanlış destekleyen) / tüm tekrarlar; `güvenilir pay` "
        "= güvenilir / "
        "tüm tekrarlar. Değerler hücreler üzerinden en büyük olandır.",
        "",
        "| aile | n | en büyük sızıntı | en büyük güvenilir pay |",
        "|---|---|---|---|",
    ]
    for (family, n), group in gt.groupby(["family", "n"]):
        lines.append(
            f"| {family} | {n} | {_pct(group['leak'].max())} | {_pct(group['share'].max())} |"
        )

    lines += [
        "",
        "## Güvenilir işaretleme payı (aile x sürücü sayısı x n)",
        "",
        "| aile | n | k=1 | k=2 | k=3 |",
        "|---|---|---|---|---|",
    ]
    for (family, n), group in table[table["family"].isin(REGION_FAMILIES)].groupby(["family", "n"]):
        cells = []
        for k in (1, 2, 3):
            g = group[group["k"] == k]
            cells.append(_pct(g["reliable"].sum() / g["reps"].sum()) if len(g) else "-")
        lines.append(f"| {family} | {n} | " + " | ".join(cells) + " |")

    pw = table[table["true_support"] & table["family"].isin(REGION_FAMILIES)].copy()
    lines += [
        "",
        "## Güç (gerçek ilişki; yalnız güvenilir tekrarlar arasında desteklenen oran)",
        "",
        "| yapı | n | güç |",
        "|---|---|---|",
    ]
    for (structure, n), group in pw.groupby(["structure", "n"]):
        rel = group["reliable"].sum()
        lines.append(
            f"| {structure} | {n} | {_pct(group['power'].sum() / rel) if rel else 'yok'} |"
        )

    bad = fs[(fs["reliable"] < 200) | (fs["rate"] > 0.04)].sort_values("rate", ascending=False)
    lines += [
        "",
        "## Protokolün ön-kayıtlı %4 payını aşan ya da yeterli güvenilir tekrarı olmayan hücreler",
        "",
        "| hücre | güvenilir | hata | oran |",
        "|---|---|---|---|",
    ]
    for cell, row in bad.head(30).iterrows():
        reliable, errors = int(row["reliable"]), int(row["false_support"])
        lines.append(f"| `{cell}` | {reliable} | {errors} | {_pct(row['rate'])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.analysis.calib.report", description=__doc__)
    parser.add_argument("--dir", action="append", required=True)
    parser.add_argument("--features", default=None)
    parser.add_argument("--method", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    raw = accept.load_records([Path(d) for d in args.dir])
    raw = raw[raw["method"] == args.method].copy()
    if args.features:
        raw = accept.attach_features(raw, Path(args.features))
    from app.analysis.runner import EXPERIMENTAL_GATE

    frame = accept.with_composites(raw)
    Path(args.out).write_text(build_tables(frame, args.method, EXPERIMENTAL_GATE), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
