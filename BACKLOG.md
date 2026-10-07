# Backlog

## Task 1.8: manually entered indicators (only when a news item needs one)

- Status: Deferred
- Context: The user decided not to build this now ("ihtiyacımız olursa yaparız"). No indicator is chosen yet; indicators are added as news scanning shows a need. Decisions so far: entry through an admin form (Phase 5, not yet in its checklist), values with an effective date and `irregular` frequency, history from 2000 for each added indicator, source note and entry time stored per value, corrections stored as new rows. Idea: a "manual" institution with normal `Series`/`Observation` rows so catalog, search and charts work unchanged.
- Remaining work: When a news item needs an indicator that TÜİK/TCMB lack, agree the indicator list with the user, then build the mechanism, add the form item to Task 5.3, and verify live.
- Resume from: `roadmap/phase-1-data-layer.md` (Task 1.8), `docs/DECISIONS.md` (§5 and open decision #6)
- Blocked by: A real need for a non-TÜİK/TCMB indicator

## Strip the BOM from press-release text

- Status: Deferred
- Context: `press-fetch --dry-run` crashes on the Windows console (cp1254) because press content starts with a `﻿` BOM that `html_to_text` keeps. The real write path is unaffected; only the preview print and stored text carry the BOM.
- Remaining work: Drop `﻿` in `html_to_text` and add a unit test.
- Resume from: `backend/app/connectors/tuik/veriportali.py` (`html_to_text`), `backend/tests/test_tuik_veriportali.py`
- Blocked by: None

## Decide how to handle Excel-only press releases

- Status: Deferred
- Context: Press 57945 ("Yapay Zeka İstatistikleri", 2025-10-01) is catalogued (documents id 9939) but `content_text` is empty (bodies are fetched on demand) and its statistical table is a `/api/tr/data/downloads?t=i...` xls link, not a databrowser2 `TR,DF_x,ver` URL. `parse_statistical_table_url` returns nothing, so the bulletin links to no dataset and its numbers are not in our data. Excel files are never downloaded today.
- Remaining work: User decides whether to download and parse such xls files (downloads are limited to 1 per 5 s per IP) or to ignore them; then implement the choice.
- Resume from: `backend/app/connectors/tuik/veriportali.py` (`parse_press_detail`, `parse_statistical_table_url`), `docs/source-profiles/tuik.md`
- Blocked by: User decision on downloading xls files

## Concept tree has no "average level" measure type

- Status: Deferred
- Context: Task 2.2 review found values that fit none of the 23 measure types well: average household size, total fertility rate, students per teacher, average wage, average working hours. They were typed `oran_pay` / `fiyat_kur` / `sure` by hand (aggregation `ortalama`, which is correct for frequency matching), so nothing is broken today.
- Remaining work: Decide with the user whether to add an `ortalama_duzey` type to `backend/app/catalog/concept_tree.yaml` and re-type those combinations.
- Resume from: `backend/app/catalog/concept_tree.yaml` (`measure_types`), `python -m app.catalog.enrich set-combination`
- Blocked by: User decision

## TCMB aggregation conflicts are noisy

- Status: Deferred
- Context: 19,894 combinations carry `aggregation_conflict = true`, almost all because TCMB's default `last` differs from our type rule (e.g. rates/prices → `ortalama`). By the user's §8 decision our rule wins for `last`, so the flag is informational only.
- Remaining work: When Phase 3/8 uses frequency matching, decide whether the flag should only mark `sum`/`avg` disagreements.
- Resume from: `backend/app/catalog/enrich_rules.py` (`effective_aggregation`)
- Blocked by: None

## Write real one-sentence dataset descriptions with an LLM

- Status: Deferred
- Context: Task 2.2 fills the reader-facing dataset description with a template (institution · category path · frequency · unit), no LLM, because the text is only read by people and never used in search (DECISIONS §7). Jev cannot write text (decision API: yes/no, choice, score only). TÜİK already ships short English source descriptions for 613 of 616 datasets; TCMB and HMB have none.
- Remaining work: When readers need real sentences, generate Turkish one-sentence descriptions with a text model (EVREN), passing the source description and metadata as input; keep the template as fallback.
- Resume from: `backend/app/data/models.py` (`Dataset.description`), the Task 2.2 template code
- Blocked by: None

## Search: a branch or leaf eliminated by Jev ends the search

- Status: Deferred
- Context: Task 2.6 misses h09 ("Kira artışı, yeni kiracı kira endeksi") and a17 ("Reel efektif döviz kuru, birim iş gücü maliyeti bazlı"). `bie_ykke` is tagged with the right leaf (0.91) and `bie_rkbigm` with `reel_efektif_kur` (1.00), but Jev scored the branch (0.42 / 0.19) or the leaf (0.15) below the 0.60 pass line, so the datasets were never scored and there is no fallback. The user chose to leave it and optimize later (2026-10-05).
- Remaining work: Design a fallback (e.g. when no strong result exists, score the datasets of the best near-miss branch/leaf, or search the dataset tags of the best leaves directly) and measure it with the Task 2.6 set.
- Resume from: `docs/search-eval/2026-10-05-sonuc.md`, `backend/app/catalog/search.py` (`search_series`), `python -m app.catalog.search_eval run`
- Blocked by: None (better judged with Phase 3 real queries)

## Search: "by province" requests do not fit one series

- Status: Deferred
- Context: a07 ("İllere göre genel doğurganlık hızı") finds the right dataset (`DF_DOGUM_GDH_C`, 1.00) but the series check fails because the tool picks one region (`REF_AREA=TR1`, confidence 0.24) and a single-region series does not match "by province". The same shape hurts a06 and any "il bazında" request.
- Remaining work: Decide how a request for every value of a breakdown is answered (a dataset-level result with the breakdown named, or one recipe per region), and how the verification question treats it.
- Resume from: `docs/search-eval/2026-10-05-sonuc.md`, `backend/app/catalog/search.py` (`_choose_dimension_codes`, `_verify_item`)
- Blocked by: User decision on the result shape

## Search: Jev scores wobble around the 0.60 line

- Status: Deferred
- Context: Task 2.6 repeated runs: h18 (`bie_pyrepo` 0.64 then 0.44, hit then miss, not caused by tagging: controlled experiment) and a16 (`bie_urbeka` vs `bie_pkauo`, hit then miss). 9 of 10 repeated queries were stable, so the effect is concentrated on queries whose true series sits near the pass line.
- Remaining work: Measure the wobble over more repeats; consider asking the dataset score twice and taking the mean, or a wider near-miss band, and re-measure.
- Resume from: `docs/search-eval/2026-10-05-sonuc.md`, `python -m app.catalog.search_eval run --repeat 3`
- Blocked by: None

## TUIK_TURIZM_SINIR_CATALOG has an unparseable default frequency

- Status: Deferred
- Context: The dataset carries `default_frequency = 'mixed'` and a `FREKANS` dimension (Yıllık, ...), so `build_series_definition` fails ("unknown default_frequency 'mixed'") and the search logs an error for every border-tourism series it tries. Other datasets of the family still work (h16 hits).
- Remaining work: Read the frequency from the `FREKANS` code like the SDMX `FREQ` rule (with a Turkish code map), measure which codes exist, and show the rule to the user before applying it live (the 2.4b pattern).
- Resume from: `backend/app/connectors/tuik/turizm.py`, `backend/app/connectors/base.py` (`_frequency_from_codes`)
- Blocked by: None

## Catch value jumps and bad revisions at load time

- Status: Deferred
- Context: On 2026-10-05 TÜİK's own CSV for `DF_TUFE_SDMX_TT10` (CPI, base 2025) returned `OBS_VALUE=4289.23` for 2026-08 (that is the 2003-base value; the correct value was 134.75). The load stored it as a normal revision and raised no alert; it was only found by comparing two series by hand. Any test using such a series would see a 30x spike.
- Remaining work: When observations are loaded or an existing period is revised, flag implausible changes (a period-to-period jump far outside the series' own history, or a revision far from the previous value) as an admin-panel alert of the existing data-alert kind. Decide the thresholds with the user. Keep the stored value (observations are append-only); the alert is the output.
- Resume from: `backend/app/core/alerts.py`, `backend/app/data/observations.py` (`record_observations`), `docs/DECISIONS.md` §5 (data-stop alert rules)
- Blocked by: None

## Decide the mixed-frequency and annual-series policy of the relation test engine

- Status: Deferred
- Context: The engine reduces every relation to its lowest frequency (monthly into quarterly or annual with the catalog rule: mean, sum or last; incomplete periods dropped). The user decided on 2026-10-07 to keep this as it is and revisit after production. Known consequences: a relation with an annual series can never be decided (the 2017-2026 window has at most 9 annual points, 10 are required, so the relation ends `insufficient_data`); the calibration covered monthly data only, so mixed-frequency and annual error rates are unknown; a lag written as 1-3 months becomes 1-3 years in an annual match.
- Remaining work: Choose the policy with the user (same frequency only; monthly with quarterly allowed and annual only with annual; or the current behavior) and the minimum window rule for annual series; extend the calibration to quarterly and mixed cases if they are supported; make the first agent state lags in the units of the lowest frequency.
- Resume from: `backend/app/analysis/frequency.py`, `backend/app/analysis/engine.py` (`prepare`, `EngineConfig.min_obs`), `docs/calibration/RESULT.md`
- Blocked by: User decision after the production launch

## Measure and refine the first agent's tautology rule and idea quality

- Status: Deferred
- Context: Task 3.3 flags an idea `tautological` when target and a driver share an accepted leaf concept tag AND the same measure type AND data nature (`backend/app/first_agent/checks.py`). It is coarse: two non-hierarchical series of one family (exports and imports tagged with one leaf) are flagged too. Live review (2026-10-07, 6 real news runs) also showed weak points: generic macro ideas repeat in almost every economic news (exchange rate -> CPI), ideas can be loosely tied to the news (a news comparing bank deposit rates gave no idea about the banks' rates), and `direction_hint` can contradict the mechanism text.
- Remaining work: Count `tautological` ideas over more stored runs (`first_agent_ideas.status`) and check them by hand for false positives; narrow the rule if needed (for example dimension-code subsumption inside the same dataset). Add prompt rules for news-specific ideas and for direction/mechanism consistency, activate as a new prompt version, re-run the same news and compare.
- Resume from: `backend/app/first_agent/checks.py`, `backend/app/first_agent/prompts/first_agent_system.md`, `python -m app.first_agent show --run-id N`
- Blocked by: None

## Fill the catalog gaps the first agent recorded

- Status: Deferred
- Context: The first agent records every series it needed and the catalog search could not find in `catalog_gaps` (`python -m app.first_agent gaps`). Live gaps so far: Brent oil price, pump fuel prices (only the CPI "fuels and lubricants" index and the TÜİK average-prices dataset exist), net minimum wage, bank deposit interest rate.
- Remaining work: Review the open gaps with the user, decide which sources to add (new connector channels vs. existing datasets that were just hard to find), and mark rows `resolved` when added. The admin list is Task 5.3.
- Resume from: `catalog_gaps` table, `python -m app.first_agent gaps`, `docs/DECISIONS.md` §5
- Blocked by: Task 5.3 for the admin view; adding sources needs a user decision
