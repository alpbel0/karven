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
