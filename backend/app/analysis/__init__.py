"""Relation test engine (Task 3.2, DECISIONS §8).

Pure code, no LLM: it reads series from PostgreSQL, matches frequencies, applies the
hypothesis transform, searches the lag, tests the relation in two windows and derives
the relation status that the graph layer (Task 3.1) stores. See ``engine.run_relation_test``.
"""
