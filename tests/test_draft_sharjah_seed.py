"""
Tests for scripts/draft_sharjah_seed.py: the SRERD list turned into draft seed rows.
No network: the list comes from the live-shape fixture through a scripted fake session.
Everything here is SYNTHETIC.
"""

from __future__ import annotations

import csv

import pytest

from scripts import draft_sharjah_seed as draft
from src.sources import seed_csv
from test_sources_support import FakeResponse, FakeSession, load_fixture, make_cfg


def live_shape():
    return load_fixture("srerd_live_shape.json")


def test_the_draft_has_the_seed_layout_plus_two_reference_columns(tmp_path):
    out = tmp_path / "draft.csv"
    session = FakeSession([FakeResponse(200, live_shape())])
    assert draft.main(["--out", str(out)], session=session, cfg=make_cfg()) == 0
    with out.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(draft.DRAFT_HEADER)
    assert set(rows[0][:10]) == set(seed_csv.HEADER_FOR_FIELD.values())  # the seed layout comes first, then the two reference columns
    body = rows[1:]
    assert len(body) == 6  # six distinct (name, area) pairs in the fixture; the unkeyed one still has a name
    first = dict(zip(rows[0], body[0]))
    assert first["Emirate"] == "Sharjah" and first["Project Name (EN)"] == "Test Complex 5"
    assert first["Area (EN)"] == "Test Area Industrial"  # the list's upper case is turned into title case
    assert first["Developer"] == "" and first["Source URL"] == draft.SRERD_LIST_PAGE
    assert first["Arabic Name (reference only)"] == "مجمع اختبار (5)" and first["Ownership (reference only)"] == "تمليك وانتفاع"
    arabic_only = next(dict(zip(rows[0], r)) for r in body if r[1] == "")
    assert arabic_only["Arabic Name (reference only)"] == "مساكن اختبار"  # the person adds the English name


def test_the_draft_rows_pass_the_seed_loader_once_a_developer_is_added(tmp_path):
    rows, counts = draft.draft_rows(live_shape()["data"])
    assert counts["written"] == 6 and counts["no_english_name"] == 1
    seed = tmp_path / "seed_registry.csv"
    with seed.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(draft.DRAFT_HEADER)
        for row in rows:
            if row[1]:  # the rows with an English name, as a person would complete them
                row = list(row)
                row[2] = "Example Developer LLC"
                writer.writerow(row)
    adapter = seed_csv.SeedCsvAdapter(path=seed)
    result = adapter.fetch()
    assert len(result.rows) == 5
    assert {r.emirate for r in result.rows} == {"Sharjah"} and all(r.project_key.startswith("name:sharjah:") for r in result.rows)
    assert all(r.source_dataset == draft.SRERD_LIST_PAGE for r in result.rows)


def test_repeated_names_and_nameless_rows_are_counted_not_written():
    data = live_shape()["data"]
    data = data + [dict(data[0]), {"proId": "99", "proNameE": "", "proName": "", "areaEName": "X"}]
    rows, counts = draft.draft_rows(data)
    assert counts["repeated"] == 1 and counts["no_name"] == 1 and counts["written"] == 6 and len(rows) == 6


def test_a_list_that_cannot_be_read_exits_1_without_echoing_the_body(tmp_path, capsys):
    session = FakeSession([FakeResponse(500, text="PRIVATE-PAGE") for _ in range(6)])
    assert draft.main(["--out", str(tmp_path / "d.csv")], session=session, cfg=make_cfg()) == 1
    err = capsys.readouterr().err
    assert "could not read the SRERD list" in err and "PRIVATE-PAGE" not in err
    assert not (tmp_path / "d.csv").exists()


def test_an_unwritable_output_exits_2(tmp_path, capsys):
    session = FakeSession([FakeResponse(200, live_shape())])
    assert draft.main(["--out", str(tmp_path / "missing-folder" / "d.csv")], session=session, cfg=make_cfg()) == 2
    assert "could not write the draft" in capsys.readouterr().err


def test_the_default_output_is_outside_the_repository():
    assert draft.DEFAULT_OUT.name == "sharjah_seed_draft.csv" and "RLScrapper" not in str(draft.DEFAULT_OUT.parent).lower()
