"""
Tests for scripts/dedupe_sheet.py: the clean-up of duplicate Registry_Projects rows.

The plan is pure, so most tests call plan_dedupe directly. The CLI is driven with a
fake SheetManager that has ONLY the two registry methods, so any attempt to touch
another tab (or call a method that does not exist) fails loudly. No network, no
credentials: every secret below is an obviously fake string. Every project,
developer and id is SYNTHETIC.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import dedupe_sheet as dedupe
from src.config import TAB_REGISTRY, Config
from src.models import REGISTRY_HEADERS, RegistryRow, dedupe_rows

ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 10, 25, 3, 0, 0, tzinfo=timezone.utc)

FAKE_SHEET_ID = "fake-spreadsheet-id-0123456789abcdef"
FAKE_API_KEY = "fake-api-key-abcdef123456"
FAKE_BEARER = "fake-bearer-token-987654321"
FAKE_PRIVATE_KEY = "fake-private-key-material-ZZZ"
FAKE_CLIENT_EMAIL = "fake-bot@example.invalid"
FAKE_SA_JSON = json.dumps(
    {
        "type": "service_account",
        "private_key": f"-----BEGIN PRIVATE KEY-----\n{FAKE_PRIVATE_KEY}\n-----END PRIVATE KEY-----\n",
        "private_key_id": "fake-private-key-id-77",
        "client_email": FAKE_CLIENT_EMAIL,
    }
)
LEAKS = (FAKE_SHEET_ID, FAKE_API_KEY, FAKE_BEARER, FAKE_PRIVATE_KEY, FAKE_CLIENT_EMAIL, "fake-private-key-id-77")


# ---------------------------------------------------------------------------
# Factories (SYNTHETIC)
# ---------------------------------------------------------------------------
def dld(number="1001", name="Example Heights", **extra) -> RegistryRow:
    # SYNTHETIC
    data = dict(
        emirate="Dubai",
        registry="DLD",
        registry_project_no=number,
        project_name_en=name,
        developer="Example Developer LLC",
    )
    data.update(extra)
    return RegistryRow(**data)


def seed(name="Example Heights", emirate="Dubai", **extra) -> RegistryRow:
    # SYNTHETIC
    data = dict(emirate=emirate, registry="SEED", project_name_en=name, developer="Example Developer LLC")
    data.update(extra)
    return RegistryRow(**data)


def srerd(number="S-77", name="Test Residences A", **extra) -> RegistryRow:
    # SYNTHETIC
    data = dict(
        emirate="Sharjah",
        registry="SRERD",
        registry_project_no=number,
        project_name_en=name,
        developer="Example Developer LLC",
    )
    data.update(extra)
    return RegistryRow(**data)


class FakeManager:
    """
    Stands in for SheetManager. It has only read_registry and write_registry, so a
    call to delete_rows, write_map_pins or anything else is an AttributeError: the
    tests therefore also prove the script touches nothing but Registry_Projects.
    """

    def __init__(self, rows, problems=(), *, read_error=None, write_error=None, drop_on_write=0, backup_dir=None):
        self.rows = list(rows)
        self.problems = list(problems)
        self.read_error = read_error
        self.write_error = write_error
        self.drop_on_write = drop_on_write
        self.backup_dir = backup_dir
        self.calls: list[str] = []
        self.written: list[list[RegistryRow]] = []
        self.backups_at_write: list[Path] = []

    def read_registry(self):
        self.calls.append("read_registry")
        if self.read_error is not None:
            raise self.read_error
        return list(self.rows), list(self.problems)

    def write_registry(self, rows):
        self.calls.append("write_registry")
        if self.backup_dir is not None:
            self.backups_at_write = sorted(Path(self.backup_dir).glob("*.json"))
        if self.write_error is not None:
            raise self.write_error
        rows = list(rows)
        self.written.append(rows)
        self.rows = rows[: len(rows) - self.drop_on_write] if self.drop_on_write else rows


CFG = Config(spreadsheet_id=FAKE_SHEET_ID, gcp_service_account_key=FAKE_SA_JSON, data_dubai_api_key=FAKE_API_KEY)


def run_cli(argv, manager, tmp_path, *, cfg=CFG):
    return dedupe.main(
        argv,
        config_loader=lambda: cfg,
        manager_factory=lambda _cfg: manager,
        backup_dir=tmp_path / "backups",
        now=FIXED_NOW,
    )


def backups(tmp_path):
    return sorted((tmp_path / "backups").glob("*.json"))


def duplicate_pair_rows():
    """A seed row and a registry row for one project, plus one unrelated project."""
    return [seed(), dld("2002", "Other Tower"), dld("1001", area_en="Business Bay")]


# ---------------------------------------------------------------------------
# plan_dedupe: what is merged
# ---------------------------------------------------------------------------
class TestPlan:
    def test_no_duplicates_is_an_empty_plan(self):
        rows = [dld("1001"), dld("1002", "Other Tower"), srerd()]
        plan = dedupe.plan_dedupe(rows)
        assert plan.merges == () and plan.refusals == () and plan.rows_to_delete == ()
        assert list(plan.result_rows) == rows
        assert plan.total_rows == 3 and plan.rows_after == 3

    def test_seed_row_and_registry_row_collapse_to_one_with_the_id(self):
        rows = duplicate_pair_rows()
        plan = dedupe.plan_dedupe(rows)
        assert not plan.blocked
        assert len(plan.merges) == 1
        merge = plan.merges[0]
        assert merge.project_key == "dld:dubai:1001"
        assert merge.keep_position == 1  # the earliest row's place is kept
        assert merge.remove_positions == (3,)
        assert plan.rows_to_delete == (3,)
        assert plan.rows_after == 2
        assert "Registry Project No." in merge.gained and "Area (EN)" in merge.gained
        assert merge.merged.registry == "DLD" and merge.merged.registry_project_no == "1001"
        assert [r.project_key for r in plan.result_rows] == ["dld:dubai:1001", "dld:dubai:2002"]

    def test_result_is_exactly_what_the_pipeline_would_produce(self):
        rows = duplicate_pair_rows() + [seed("Third Tower"), seed("Third Tower")]
        assert list(dedupe.plan_dedupe(rows).result_rows) == dedupe_rows(rows)

    def test_input_is_not_changed(self):
        rows = duplicate_pair_rows()
        snapshot = [r.model_copy(deep=True) for r in rows]
        dedupe.plan_dedupe(rows)
        assert rows == snapshot

    def test_transitive_cluster_of_three(self):
        # Row 2 holds the name of row 1 and the id of row 3, so all three are one project.
        rows = [seed(), dld("1001"), dld("1001", status="Active")]
        plan = dedupe.plan_dedupe(rows)
        assert len(plan.merges) == 1 and plan.merges[0].remove_positions == (2, 3)
        assert plan.rows_after == 1

    def test_identical_rows_and_the_same_object_twice_collapse(self):
        row = dld()
        plan = dedupe.plan_dedupe([row, row, dld()])
        assert plan.merges[0].keep_position == 1
        assert plan.merges[0].remove_positions == (2, 3)
        assert plan.rows_after == 1

    def test_different_registry_ids_with_one_name_are_never_merged(self):
        rows = [dld("1001"), dld("1002")]  # same name, same developer, different ids
        plan = dedupe.plan_dedupe(rows)
        assert plan.merges == () and not plan.blocked and plan.rows_after == 2

    def test_an_id_less_row_is_not_guessed_into_one_of_two_ids(self):
        rows = [dld("1001"), dld("1002"), seed()]
        plan = dedupe.plan_dedupe(rows)
        assert plan.merges == () and plan.rows_after == 3

    def test_dubai_and_sharjah_projects_with_one_name_stay_apart(self):
        rows = [seed("Marina Views", emirate="Dubai"), seed("Marina Views", emirate="Sharjah")]
        plan = dedupe.plan_dedupe(rows)
        assert plan.merges == () and plan.rows_after == 2

    def test_tower_a_and_tower_b_stay_apart(self):
        plan = dedupe.plan_dedupe([seed("Example Tower A"), seed("Example Tower B")])
        assert plan.merges == ()

    def test_first_seen_is_earliest_and_last_seen_latest(self):
        rows = [
            seed(first_seen="2026-09-01", last_seen="2026-09-30"),
            dld("1001", first_seen="2026-08-15", last_seen="2026-10-20"),
        ]
        merged = dedupe.plan_dedupe(rows).merges[0].merged
        assert merged.first_seen == "2026-08-15" and merged.last_seen == "2026-10-20"

    def test_a_pin_is_never_assembled_from_two_rows(self):
        half = seed(latitude=25.2048)  # half-written: latitude only
        full = dld("1001", latitude=25.1, longitude=55.2, location_precision="community")
        merged = dedupe.plan_dedupe([half, full]).merges[0].merged
        assert (merged.latitude, merged.longitude) == (25.1, 55.2)
        assert merged.location_precision == "community"

    def test_a_half_written_pin_is_not_written_back(self):
        plan = dedupe.plan_dedupe([seed(latitude=25.2048), dld("1001")])
        row = plan.result_rows[0].to_sheet_dict()
        assert row["Latitude"] == "" and row["Longitude"] == "" and row["Location Precision"] == ""

    def test_arabic_only_rows_have_no_key_and_are_left_alone(self):
        rows = [
            RegistryRow(emirate="Dubai", registry="SEED", project_name_ar="برج المثال"),
            RegistryRow(emirate="Dubai", registry="SEED", project_name_ar="برج المثال"),
        ]
        plan = dedupe.plan_dedupe(rows)
        # Two keyless rows share the empty key; that must not look like a collision.
        assert plan.merges == () and plan.rows_after == 2 and not plan.blocked

    def test_keyless_rows_pass_through_the_cli_untouched(self, tmp_path):
        rows = [
            RegistryRow(emirate="Dubai", registry="SEED", project_name_ar="برج المثال"),
            RegistryRow(emirate="Dubai", registry="SEED", project_name_ar="برج المثال"),
            seed(),
            dld("1001"),
        ]
        manager = FakeManager(rows)
        assert run_cli(["--apply"], manager, tmp_path) == 0
        assert len(manager.rows) == 3 and [r.project_key for r in manager.rows].count("") == 2

    def test_arabic_text_survives_a_merge(self):
        rows = [seed(), dld("1001", project_name_ar="برج المثال", area_ar="الخليج التجاري")]
        merged = dedupe.plan_dedupe(rows).merges[0].merged
        assert merged.project_name_ar == "برج المثال"

    def test_huge_values_do_not_break_the_plan(self):
        big = "Example Heights " + "x" * 20000
        plan = dedupe.plan_dedupe([seed(big, status="y" * 50000), seed(big)])
        assert len(plan.merges) == 1
        long_id = dld("9" * 12)  # not a valid DLD id: no registry key, falls back to the name
        assert long_id.registry_key() is None
        assert dedupe.plan_dedupe([long_id, long_id]).rows_after == 1

    def test_empty_input(self):
        plan = dedupe.plan_dedupe([])
        assert plan.total_rows == 0 and plan.merges == () and plan.result_rows == ()

    @pytest.mark.parametrize("bad", [None, "rows", b"rows"])
    def test_wrong_container_type_is_a_type_error(self, bad):
        with pytest.raises(TypeError):
            dedupe.plan_dedupe(bad)

    def test_a_non_row_element_is_a_type_error_naming_its_position(self):
        with pytest.raises(TypeError, match="row 2"):
            dedupe.plan_dedupe([dld(), {"Project Key": "x"}])

    def test_three_thousand_rows(self):
        rows = [dld(str(10000 + i), f"Test Residences {i}") for i in range(2000)]
        rows += [seed(f"Test Residences {i}") for i in range(1000)]
        plan = dedupe.plan_dedupe(rows)
        assert len(plan.merges) == 1000 and plan.rows_after == 2000
        assert plan.rows_to_delete == tuple(range(2001, 3001))

    def test_running_the_plan_again_on_its_own_result_finds_nothing(self):
        first = dedupe.plan_dedupe(duplicate_pair_rows() + [seed("Third Tower"), seed("Third Tower")])
        second = dedupe.plan_dedupe(list(first.result_rows))
        assert second.merges == () and not second.blocked


# ---------------------------------------------------------------------------
# plan_dedupe: the safety checks (cluster_rows promises these; they are checked anyway)
# ---------------------------------------------------------------------------
class TestRefusals:
    def test_two_different_registry_ids_in_one_cluster_block_the_plan(self, monkeypatch):
        rows = [dld("1001"), dld("1002"), dld("3003", "Fine Tower"), seed("Fine Tower")]
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [[r[0], r[1]], [r[2], r[3]]])
        plan = dedupe.plan_dedupe(rows)
        assert plan.blocked
        assert "different DLD registry ids" in plan.refusals[0].reason
        assert plan.refusals[0].positions == (1, 2)
        assert "dld:dubai:1001" in plan.refusals[0].reason and "dld:dubai:1002" in plan.refusals[0].reason
        # one bad cluster means no cluster is trusted: nothing to merge, nothing to delete
        assert plan.merges == () and plan.result_rows == () and plan.rows_to_delete == ()
        assert plan.rows_after == 4

    def test_two_emirates_in_one_cluster_block_the_plan(self, monkeypatch):
        rows = [dld("1001"), seed(emirate="Sharjah")]
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [list(r)])
        plan = dedupe.plan_dedupe(rows)
        assert plan.blocked and "different emirates" in plan.refusals[0].reason

    def test_merge_rows_refusing_blocks_the_plan(self, monkeypatch):
        def refuse(a, b):
            raise ValueError("cannot merge")

        monkeypatch.setattr(dedupe, "merge_rows", refuse)
        plan = dedupe.plan_dedupe([seed(), dld("1001")])
        assert plan.blocked and "merge_rows refused" in plan.refusals[0].reason

    def test_a_merge_that_loses_the_registry_id_blocks_the_plan(self, monkeypatch):
        monkeypatch.setattr(
            dedupe, "merge_rows", lambda a, b: a.model_copy(update={"registry": "SEED", "registry_project_no": None})
        )
        plan = dedupe.plan_dedupe([dld("1001"), seed()])
        assert plan.blocked and "registry id" in plan.refusals[0].reason

    def test_a_merge_that_invents_an_id_blocks_the_plan(self, monkeypatch):
        monkeypatch.setattr(
            dedupe, "merge_rows", lambda a, b: a.model_copy(update={"registry": "DLD", "registry_project_no": "999"})
        )
        plan = dedupe.plan_dedupe([seed(), seed()])
        assert plan.blocked

    def test_a_merge_that_changes_emirate_blocks_the_plan(self, monkeypatch):
        monkeypatch.setattr(dedupe, "merge_rows", lambda a, b: a.model_copy(update={"emirate": "Sharjah"}))
        plan = dedupe.plan_dedupe([seed(), seed()])
        assert plan.blocked and "emirate" in plan.refusals[0].reason

    def test_a_clustering_that_drops_a_row_blocks_the_plan(self, monkeypatch):
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [[row] for row in r[:-1]])
        plan = dedupe.plan_dedupe([dld("1001"), dld("1002")])
        assert plan.blocked and "dropped a row" in plan.refusals[0].reason
        assert plan.result_rows == ()

    def test_a_clustering_that_returns_foreign_rows_blocks_the_plan(self, monkeypatch):
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [[row.model_copy()] for row in r])
        plan = dedupe.plan_dedupe([dld("1001"), dld("1002")])
        assert plan.blocked and "not in its input" in plan.refusals[0].reason

    def test_two_result_rows_with_one_key_block_the_plan(self, monkeypatch):
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [[row] for row in r])
        plan = dedupe.plan_dedupe([dld("1001"), dld("1001")])
        assert plan.blocked and "share the key dld:dubai:1001" in plan.refusals[0].reason

    def test_a_blocked_plan_reports_itself_without_listing_merges(self, monkeypatch):
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [list(r)])
        text = dedupe.format_plan(dedupe.plan_dedupe([dld("1001"), dld("1002")]))
        assert "REFUSED" in text and "nothing will be written" in text and "keep position" not in text


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_has_counts_and_keys(self):
        text = dedupe.format_plan(dedupe.plan_dedupe(duplicate_pair_rows()))
        assert "3 row(s) read, 1 duplicate cluster(s), 1 row(s) to remove, 2 row(s) afterwards" in text
        assert "[dld:dubai:1001] keep position 1, remove 3" in text

    def test_report_with_no_duplicates(self):
        text = dedupe.format_plan(dedupe.plan_dedupe([dld("1001")]))
        assert "0 duplicate cluster(s)" in text and "keep position" not in text

    def test_a_long_report_is_capped(self):
        rows = []
        for i in range(dedupe.MAX_CLUSTERS_LISTED + 5):
            rows += [seed(f"Test Residences {i}"), dld(str(5000 + i), f"Test Residences {i}")]
        text = dedupe.format_plan(dedupe.plan_dedupe(rows))
        assert "... and 5 more cluster(s)" in text
        assert len(text.splitlines()) == dedupe.MAX_CLUSTERS_LISTED + 2


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
class TestBackup:
    def test_backup_file_name_content_and_round_trip(self, tmp_path):
        rows = [dld("1001", project_name_ar="برج المثال", area_en="Business Bay"), srerd()]
        path = dedupe.write_backup(rows, backup_dir=tmp_path / "nested" / "backups", now=FIXED_NOW)
        assert path.parent == tmp_path / "nested" / "backups"
        assert path.name == f"{TAB_REGISTRY}-20261025T030000Z.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["tab"] == TAB_REGISTRY and payload["row_count"] == 2
        assert payload["captured_at"] == "2026-10-25T03:00:00Z"
        assert payload["headers"] == list(REGISTRY_HEADERS)
        assert "برج المثال" in path.read_text(encoding="utf-8")  # kept readable, not \u-escaped
        assert dedupe.load_backup(path) == rows

    def test_a_second_backup_in_the_same_second_gets_its_own_file(self, tmp_path):
        first = dedupe.write_backup([dld()], backup_dir=tmp_path, now=FIXED_NOW)
        second = dedupe.write_backup([dld("1002")], backup_dir=tmp_path, now=FIXED_NOW)
        assert first != second and first.exists() and second.exists()
        assert len(dedupe.load_backup(first)) == 1 and dedupe.load_backup(second)[0].registry_project_no == "1002"

    def test_a_backup_that_cannot_be_written_raises(self, tmp_path):
        blocker = tmp_path / "backups"
        blocker.write_text("a file where the directory should be", encoding="utf-8")
        with pytest.raises(OSError):
            dedupe.write_backup([dld()], backup_dir=blocker, now=FIXED_NOW)

    def test_a_failed_write_leaves_no_half_file(self, tmp_path, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(dedupe.json, "dump", explode)
        with pytest.raises(OSError, match="disk full"):
            dedupe.write_backup([dld()], backup_dir=tmp_path, now=FIXED_NOW)
        assert list(tmp_path.glob("*.json")) == []

    def test_a_backup_that_does_not_read_back_is_refused_and_removed(self, tmp_path, monkeypatch):
        # The disk "accepts" the write but the file on it is cut short.
        def truncate(fd):
            import os

            os.ftruncate(fd, 20)

        monkeypatch.setattr(dedupe.os, "fsync", truncate)
        with pytest.raises(OSError, match="read-back"):
            dedupe.write_backup([dld(), srerd()], backup_dir=tmp_path, now=FIXED_NOW)
        assert list(tmp_path.glob("*.json")) == []

    def test_a_backup_that_reads_back_with_other_rows_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dedupe, "load_backup", lambda path, tab=TAB_REGISTRY: [dld("9999", "Different Tower")])
        with pytest.raises(OSError, match="did not match"):
            dedupe.write_backup([dld(), srerd()], backup_dir=tmp_path, now=FIXED_NOW)
        assert list(tmp_path.glob("*.json")) == []

    def test_an_unreadable_backup_stops_the_cli_before_any_write(self, tmp_path, monkeypatch, capsys, caplog):
        def truncate(fd):
            import os

            os.ftruncate(fd, 20)

        monkeypatch.setattr(dedupe.os, "fsync", truncate)
        manager = FakeManager(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "write_registry" not in manager.calls

    def test_a_naive_clock_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            dedupe.write_backup([dld()], backup_dir=tmp_path, now=datetime(2026, 10, 25, 3, 0, 0))
        assert list(tmp_path.glob("*.json")) == []

    @pytest.mark.parametrize(
        "content, message",
        [
            ("{ not json", "not valid JSON"),
            ("[1, 2]", "expected a JSON object"),
            (json.dumps({"tab": "Listing_Data", "rows": []}), "not 'Registry_Projects'"),
            (json.dumps({"tab": TAB_REGISTRY}), "no 'rows' list"),
            (json.dumps({"tab": TAB_REGISTRY, "rows": ["x"]}), "row 1 is not an object"),
            (json.dumps({"tab": TAB_REGISTRY, "rows": [{"Emirate": "Abu Dhabi", "Registry": "DLD"}]}), "row 1 is invalid"),
        ],
    )
    def test_load_backup_rejects_what_is_not_a_backup(self, tmp_path, content, message):
        path = tmp_path / "bad.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            dedupe.load_backup(path)

    def test_load_backup_error_does_not_echo_row_values(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"tab": TAB_REGISTRY, "rows": [{"Emirate": "Zzyzx Marker", "Registry": "DLD"}]}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as caught:
            dedupe.load_backup(path)
        assert "Zzyzx" not in str(caught.value)

    def test_backups_are_git_ignored(self):
        lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
        assert "backups/" in lines
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "backups/example.json"], cwd=ROOT, capture_output=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return
        if result.returncode in (0, 1):  # 128 means "not a usable repository": skip
            assert result.returncode == 0


# ---------------------------------------------------------------------------
# CLI: dry run, apply, ordering
# ---------------------------------------------------------------------------
class TestCliDryRunAndApply:
    def test_dry_run_is_the_default_and_writes_nothing(self, tmp_path, capsys):
        manager = FakeManager(duplicate_pair_rows())
        assert run_cli([], manager, tmp_path) == 0
        out = capsys.readouterr().out
        assert manager.calls == ["read_registry"]
        assert "DRY RUN" in out and "[dld:dubai:1001]" in out
        assert "1 duplicate cluster(s)" in out

    def test_dry_run_still_writes_a_backup_and_prints_its_path(self, tmp_path, capsys):
        run_cli([], FakeManager(duplicate_pair_rows()), tmp_path)
        files = backups(tmp_path)
        assert len(files) == 1
        assert f"Backup written: {files[0]}" in capsys.readouterr().out
        assert len(json.loads(files[0].read_text(encoding="utf-8"))["rows"]) == 3

    def test_apply_writes_the_deduplicated_tab_and_verifies(self, tmp_path, capsys):
        manager = FakeManager(duplicate_pair_rows(), backup_dir=tmp_path / "backups")
        assert run_cli(["--apply"], manager, tmp_path) == 0
        # Changed on review: one more read, directly before the write, so a row added since the first read
        # is never replaced away (see test_a_row_typed_in_between_the_read_and_the_write_is_never_deleted).
        assert manager.calls == ["read_registry", "read_registry", "write_registry", "read_registry"]
        assert [r.project_key for r in manager.rows] == ["dld:dubai:1001", "dld:dubai:2002"]
        assert "Done: removed 1 duplicate row(s) in 1 cluster(s)" in capsys.readouterr().out

    def test_the_backup_exists_and_is_complete_before_the_write(self, tmp_path):
        manager = FakeManager(duplicate_pair_rows(), backup_dir=tmp_path / "backups")
        run_cli(["--apply"], manager, tmp_path)
        assert len(manager.backups_at_write) == 1
        saved = dedupe.load_backup(manager.backups_at_write[0])
        assert saved == duplicate_pair_rows()  # the ORIGINAL rows, duplicates included

    def test_no_backup_no_write(self, tmp_path):
        blocker = tmp_path / "backups"
        blocker.write_text("not a directory", encoding="utf-8")
        manager = FakeManager(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "write_registry" not in manager.calls

    def test_the_backup_dir_option(self, tmp_path):
        target = tmp_path / "elsewhere"
        assert run_cli(["--backup-dir", str(target)], FakeManager(duplicate_pair_rows()), tmp_path) == 0
        assert len(list(target.glob("*.json"))) == 1

    def test_nothing_to_do_when_clean(self, tmp_path, capsys):
        manager = FakeManager([dld("1001"), dld("1002", "Other Tower")])
        assert run_cli(["--apply"], manager, tmp_path) == 0
        assert "write_registry" not in manager.calls
        assert "No duplicates found" in capsys.readouterr().out

    def test_empty_tab_is_not_an_error_and_writes_no_backup(self, tmp_path, capsys):
        manager = FakeManager([])
        assert run_cli(["--apply"], manager, tmp_path) == 0
        assert manager.calls == ["read_registry"] and backups(tmp_path) == []
        assert "Nothing to do" in capsys.readouterr().out

    def test_rerunning_after_apply_finds_nothing(self, tmp_path, capsys):
        manager = FakeManager(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 0
        capsys.readouterr()
        assert run_cli(["--apply"], manager, tmp_path) == 0
        assert manager.calls.count("write_registry") == 1
        assert "No duplicates found" in capsys.readouterr().out

    def test_the_hand_added_columns_caveat_is_printed_before_anything_is_written(self, tmp_path, capsys):
        run_cli([], FakeManager(duplicate_pair_rows()), tmp_path)
        assert dedupe.HAND_COLUMNS_NOTE in capsys.readouterr().out
        run_cli([], FakeManager([dld("1001")]), tmp_path)
        assert dedupe.HAND_COLUMNS_NOTE not in capsys.readouterr().out  # only when there is something to merge

    def test_values_that_could_not_follow_their_row_are_reported_after_the_write(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.INFO)

        class WithExtras(FakeManager):
            def write_registry(self, rows):
                super().write_registry(rows)
                self.last_dropped_extras = ["dld:dubai:1001", f"row 9 (no Project Key) key={FAKE_API_KEY}"]

        assert run_cli(["--apply"], WithExtras(duplicate_pair_rows()), tmp_path) == 0
        out = capsys.readouterr().out
        assert "WARNING: values in columns added by hand could not be carried over for 2 row(s)" in out
        assert "dld:dubai:1001" in out and "WARNING: values in columns" in caplog.text
        assert FAKE_API_KEY not in out + caplog.text

    @pytest.mark.parametrize("value", [None, [], (), "dld:dubai:1001", 3])
    def test_a_manager_without_a_usable_dropped_list_adds_no_warning(self, tmp_path, capsys, value):
        class Odd(FakeManager):
            last_dropped_extras = value

        assert run_cli(["--apply"], Odd(duplicate_pair_rows()), tmp_path) == 0
        assert "could not be carried over" not in capsys.readouterr().out

    def test_output_is_counts_and_keys_not_row_dumps(self, tmp_path, capsys):
        rows = [seed(escrow_agent="Zzyzx Escrow Marker", zone="Zzyzx Zone Marker"), dld("1001")]
        run_cli(["--apply"], FakeManager(rows), tmp_path)
        captured = capsys.readouterr()
        assert "Zzyzx" not in captured.out + captured.err
        assert "Example Heights" not in captured.out  # names live in keys only as slugs

    def test_unknown_flag_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit) as caught:
            run_cli(["--delete-everything"], FakeManager([]), tmp_path)
        assert caught.value.code == 2

    def test_there_is_no_option_to_pick_another_tab(self, tmp_path):
        with pytest.raises(SystemExit):
            run_cli(["--worksheet", "Listing_Data"], FakeManager([]), tmp_path)


class TestCliRefusals:
    def test_apply_is_refused_while_rows_are_unreadable(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.INFO)
        manager = FakeManager(duplicate_pair_rows(), problems=["row 9: emirate must be one of ('Dubai', 'Sharjah')"])
        assert run_cli(["--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls
        assert "unreadable" in caplog.text
        assert "row 9: emirate must be" in capsys.readouterr().out

    def test_a_dry_run_with_unreadable_rows_still_reports(self, tmp_path, capsys):
        manager = FakeManager(duplicate_pair_rows(), problems=["row 9: bad"])
        assert run_cli([], manager, tmp_path) == 0
        assert "1 row(s) could not be read" in capsys.readouterr().out

    def test_many_problem_lines_are_capped(self, tmp_path, capsys):
        manager = FakeManager([dld()], problems=[f"row {i}: bad" for i in range(50)])
        run_cli([], manager, tmp_path)
        out = capsys.readouterr().out
        assert "... and 30 more" in out and "row 19: bad" in out and "row 20: bad" not in out

    def test_a_blocked_plan_writes_nothing_even_with_apply(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [list(r)])
        manager = FakeManager([dld("1001"), dld("1002")])
        assert run_cli(["--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls
        assert "safety check failed" in caplog.text

    def test_a_blocked_dry_run_is_not_a_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dedupe, "cluster_rows", lambda r: [list(r)])
        assert run_cli([], FakeManager([dld("1001"), dld("1002")]), tmp_path) == 2

    def test_a_huge_removal_needs_an_explicit_flag(self, tmp_path):
        rows = [dld(), dld(), dld()]  # three copies: two of three rows would go
        manager = FakeManager(rows)
        assert run_cli(["--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls
        assert run_cli(["--apply", "--allow-large-removal"], manager, tmp_path) == 0
        assert len(manager.rows) == 1

    def test_removing_exactly_half_is_allowed(self, tmp_path):
        manager = FakeManager([dld(), dld()])
        assert run_cli(["--apply"], manager, tmp_path) == 0

    def test_a_manager_that_returns_the_wrong_kind_of_row_is_a_failure_not_a_crash(self, tmp_path):
        manager = FakeManager([dld(), {"Project Key": "x"}])
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "write_registry" not in manager.calls


class TestCliFailures:
    def test_verification_failure_is_reported_with_the_backup_path(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        manager = FakeManager(duplicate_pair_rows(), drop_on_write=1)
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "VERIFICATION FAILED" in caplog.text
        assert str(backups(tmp_path)[0]) in caplog.text

    def test_verification_failure_when_the_keys_differ(self, tmp_path, caplog):
        class Swapping(FakeManager):
            def write_registry(self, rows):
                super().write_registry([dld("7777", "Wrong Tower")] * len(rows))

        manager = Swapping(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 1

    def test_verification_failure_when_the_readback_reports_problems(self, tmp_path):
        class Corrupting(FakeManager):
            def write_registry(self, rows):
                super().write_registry(rows)
                self.problems = ["row 2: garbage"]

        assert run_cli(["--apply"], Corrupting(duplicate_pair_rows()), tmp_path) == 1

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("read timed out"),
            RuntimeError("APIError: [429]: Quota exceeded for quota metric 'Read requests'"),
            RuntimeError("APIError: [503]: The service is currently unavailable."),
            ValueError("not enough values to unpack"),
        ],
    )
    def test_a_read_failure_exits_one_and_writes_nothing(self, tmp_path, error):
        manager = FakeManager([dld()], read_error=error)
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert manager.calls == ["read_registry"] and backups(tmp_path) == []

    @pytest.mark.parametrize(
        "error",
        [TimeoutError("write timed out"), RuntimeError("APIError: [429]: Quota exceeded"), RuntimeError("[503] backend")],
    )
    def test_a_write_failure_exits_one_and_names_the_backup(self, tmp_path, caplog, error):
        caplog.set_level(logging.INFO)
        manager = FakeManager(duplicate_pair_rows(), write_error=error)
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "may be partly written" in caplog.text
        assert str(backups(tmp_path)[0]) in caplog.text

    def test_a_row_typed_in_between_the_read_and_the_write_is_never_deleted(self, tmp_path, caplog):
        # Reviewed: the plan comes from one read, then a backup is written (seconds on a big tab), then
        # write_registry replaces EVERY data row. main.py re-reads and refuses when the tab moved; this tool did not.
        class Racing(FakeManager):
            def read_registry(self):
                result = super().read_registry()
                if self.calls.count("read_registry") == 1:  # a person adds a row right after our read
                    self.rows.append(dld("9009", "Typed By Hand"))
                return result

        manager = Racing(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls
        assert "Typed By Hand" in [r.project_name_en for r in manager.rows]
        assert "changed" in caplog.text and "nothing was written" in caplog.text

    def test_a_row_that_became_unreadable_in_between_is_never_deleted(self, tmp_path, caplog):
        # The readable rows are unchanged, but someone typed a row the reader cannot parse: write_registry would
        # replace every data row and that row, which is in no list we hold, would be lost.
        class Racing(FakeManager):
            def read_registry(self):
                result = super().read_registry()
                if self.calls.count("read_registry") == 1:
                    self.problems.append("row 9: emirate: not Dubai or Sharjah")
                return result

        manager = Racing(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls
        assert "changed" in caplog.text

    def test_a_failed_re_read_before_the_write_writes_nothing(self, tmp_path):
        class FlakySecondRead(FakeManager):
            def read_registry(self):
                if self.calls.count("read_registry") == 1:
                    self.calls.append("read_registry")
                    raise TimeoutError("read timed out")
                return super().read_registry()

        manager = FlakySecondRead(duplicate_pair_rows())
        assert run_cli(["--apply"], manager, tmp_path) == 1
        assert "write_registry" not in manager.calls

    def test_a_failure_to_open_the_sheet_exits_one(self, tmp_path):
        def factory(cfg):
            raise RuntimeError("could not authorise")

        code = dedupe.main([], config_loader=lambda: CFG, manager_factory=factory, backup_dir=tmp_path, now=FIXED_NOW)
        assert code == 1


# ---------------------------------------------------------------------------
# Secrets never reach the console, the log or an exception
# ---------------------------------------------------------------------------
def nasty_error(prefix="boom"):
    return RuntimeError(
        f"{prefix}: POST https://sheets.googleapis.com/v4/spreadsheets/{FAKE_SHEET_ID}/values:batchUpdate"
        f"?key={FAKE_API_KEY} failed; Authorization: Bearer {FAKE_BEARER}; "
        f'{{"private_key": "-----BEGIN PRIVATE KEY-----\\n{FAKE_PRIVATE_KEY}\\n-----END PRIVATE KEY-----", '
        f'"client_email": "{FAKE_CLIENT_EMAIL}"}} private_key_id=fake-private-key-id-77'
    )


def assert_no_leak(capsys, caplog, *extra):
    seen = capsys.readouterr()
    everything = seen.out + seen.err + caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    for leak in (*LEAKS, *extra):
        assert leak not in everything, leak
    assert "BEGIN PRIVATE KEY" not in everything


class TestSecretsAreScrubbed:
    def test_a_write_error_full_of_secrets_prints_none(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        manager = FakeManager(duplicate_pair_rows(), write_error=nasty_error())
        assert run_cli(["--apply"], manager, tmp_path) == 1
        # Deliberately changed: this used to assert "[redacted]", i.e. that the unplanned error's text was shown
        # with its secrets scrubbed. The text of a class this tool does not own is no longer shown at all (the
        # scrubber only removes what it recognises), so there is nothing left to redact; the own-error variant
        # below keeps the scrubbing covered.
        assert "the write failed and the tab may be partly written: RuntimeError (at " in caplog.text
        assert "POST https" not in caplog.text and "boom" not in caplog.text
        assert_no_leak(capsys, caplog)

    def test_a_read_error_full_of_secrets_prints_none(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        assert run_cli([], FakeManager([dld()], read_error=nasty_error()), tmp_path) == 1
        assert_no_leak(capsys, caplog)

    def test_an_open_error_full_of_secrets_prints_none(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)

        def factory(cfg):
            raise nasty_error()

        dedupe.main([], config_loader=lambda: CFG, manager_factory=factory, backup_dir=tmp_path, now=FIXED_NOW)
        assert_no_leak(capsys, caplog)

    def test_secret_shaped_text_is_scrubbed_even_when_the_value_is_unknown(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        manager = FakeManager(duplicate_pair_rows(), write_error=nasty_error())
        assert run_cli(["--apply"], manager, tmp_path, cfg=Config()) == 1  # no configured secrets at all
        assert_no_leak(capsys, caplog)

    def test_the_verification_failure_message_carries_no_secret(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        assert run_cli(["--apply"], FakeManager(duplicate_pair_rows(), drop_on_write=1), tmp_path) == 1
        assert_no_leak(capsys, caplog)

    def test_an_unreadable_row_line_is_scrubbed_too(self, tmp_path, capsys, caplog):
        manager = FakeManager([dld()], problems=[f"row 3: bad value key={FAKE_API_KEY}"])
        run_cli([], manager, tmp_path)
        assert_no_leak(capsys, caplog)

    def test_a_plain_run_prints_no_sheet_id_or_url(self, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        run_cli(["--apply"], FakeManager(duplicate_pair_rows()), tmp_path)
        assert_no_leak(capsys, caplog)
        assert "docs.google.com" not in capsys.readouterr().out

    @pytest.mark.parametrize(
        "text, gone",
        [
            ("GET https://x.example/v1/items?key=fake-key-12345&alt=json", "fake-key-12345"),
            ("GET https://x.example/v1?api_key=fake-key-12345", "fake-key-12345"),
            ("url?token=fake-token-12345", "fake-token-12345"),
            ("password=fake-password-1", "fake-password-1"),
            ("Authorization: Bearer fake-bearer-12345678", "fake-bearer-12345678"),
            ("Bearer fake-bearer-12345678", "fake-bearer-12345678"),
            ('{"api_key": "fake-key-in-json"}', "fake-key-in-json"),
            # Added on review: a dict printed by Python (single quotes) and header-dump colon forms
            ("{'api_key': 'fake-key-in-repr'}", "fake-key-in-repr"),
            ("{'access_token': 'fake-token-in-repr', 'expires_in': 3599}", "fake-token-in-repr"),
            ("{'Authorization': 'fake-auth-in-repr'}", "fake-auth-in-repr"),
            ("X-Api-Key: fake-key-in-header", "fake-key-in-header"),
            ("token: fake-token-in-log", "fake-token-in-log"),
            ("-----BEGIN PRIVATE KEY-----\nfake-material\n-----END PRIVATE KEY-----", "fake-material"),
            ("-----BEGIN PRIVATE KEY-----\ncut-off-material", "cut-off-material"),
            ("https://docs.google.com/spreadsheets/d/fake-sheet-id-abcdefghijklmnop/edit", "fake-sheet-id-abcdefghijklmnop"),
            ("https://sheets.googleapis.com/v4/spreadsheets/fake-sheet-id-abcdefghijklmnop/values", "fake-sheet-id-abcdefghijklmnop"),
        ],
    )
    def test_scrub_patterns(self, text, gone):
        assert gone not in dedupe.scrub(text)

    def test_scrub_removes_known_values_and_keeps_the_rest(self):
        out = dedupe.scrub("failed for fake-secret-value on tab Registry_Projects", ["fake-secret-value"])
        assert "fake-secret-value" not in out and "Registry_Projects" in out

    def test_scrub_ignores_trivially_short_secrets(self):
        assert dedupe.scrub("abc and abc", ["ab"]) == "abc and abc"

    def test_scrub_caps_length_and_collapses_lines(self):
        out = dedupe.scrub("line one\nline two " + "x" * 1000)
        assert "\n" not in out and len(out) <= dedupe.MAX_MESSAGE_CHARS + 3

    def test_secret_values_collects_everything_and_the_service_account_parts(self):
        cfg = Config(
            spreadsheet_id=FAKE_SHEET_ID,
            gcp_service_account_key=FAKE_SA_JSON,
            gemini_api_key="fake-gemini-key",
            share_with_emails=("someone@example.invalid",),
        )
        values = dedupe.secret_values(cfg)
        for needed in (FAKE_SHEET_ID, FAKE_SA_JSON, "fake-gemini-key", "someone@example.invalid", FAKE_CLIENT_EMAIL):
            assert needed in values
        assert any(FAKE_PRIVATE_KEY in v for v in values)
        assert values == sorted(values, key=len, reverse=True)

    def test_secret_values_tolerates_a_service_account_that_is_not_json(self):
        assert dedupe.secret_values(Config(gcp_service_account_key="not json at all")) == ["not json at all"]

    def test_describe_error_survives_an_exception_whose_str_fails(self):
        class Broken(Exception):
            def __str__(self):
                raise RuntimeError("no")

        # Deliberately changed from == "Broken": a class that is not this tool's own now carries its location
        # (here "unknown": the exception was never raised, so it has no traceback) instead of its text.
        assert dedupe.describe_error(Broken()) == "Broken (at unknown)"

        class BrokenSetup(dedupe.SetupError):
            def __str__(self):
                raise RuntimeError("no")

        assert dedupe.describe_error(BrokenSetup()) == "BrokenSetup"  # an own type whose text fails is still reportable


# What an unplanned error's text can carry: a cell value, a project name, a request URL, a response body.
LEAK = "MARKER-LEAK Alpha Residences Tower"


class UnrecognisedError(Exception):
    """A class this tool has never heard of (an SDK, a library, a bug)."""


class TestErrorTextIsShownOnlyForTheToolsOwnErrors:
    def raised(self, error):
        try:
            raise error
        except BaseException as caught:  # noqa: BLE001 - the traceback is what describe_error locates
            return caught

    def test_an_unplanned_class_is_shown_as_its_name_and_where_it_happened(self):
        out = dedupe.describe_error(self.raised(UnrecognisedError(LEAK)))
        assert out.startswith("UnrecognisedError (at test_dedupe_script.py:") and out.endswith(" in raised)")
        assert "MARKER-LEAK" not in out

    @pytest.mark.parametrize(
        "error",
        [KeyError(LEAK), ValueError(f"bad cell {LEAK}"), OSError(f"[Errno 2] No such file: '{LEAK}'"), RuntimeError(LEAK)],
        ids=["key-error", "value-error", "os-error", "runtime-error"],
    )
    def test_the_text_of_builtin_errors_is_not_shown_either(self, error):
        out = dedupe.describe_error(self.raised(error))
        assert out.startswith(f"{type(error).__name__} (at ") and "MARKER-LEAK" not in out

    @pytest.mark.parametrize("path", ["read", "write", "open"])
    def test_an_unplanned_error_text_reaches_no_output_on_any_path(self, tmp_path, capsys, caplog, path):
        caplog.set_level(logging.DEBUG)
        error = UnrecognisedError(f"{LEAK} {FAKE_API_KEY}")
        if path == "open":
            def factory(cfg):
                raise error

            code = dedupe.main([], config_loader=lambda: CFG, manager_factory=factory, backup_dir=tmp_path, now=FIXED_NOW)
        else:
            manager = FakeManager(duplicate_pair_rows(), **{f"{path}_error": error})
            code = run_cli(["--apply"], manager, tmp_path)
        assert code == 1
        seen = capsys.readouterr()
        everything = seen.out + seen.err + caplog.text
        assert "UnrecognisedError (at " in everything
        assert "MARKER-LEAK" not in everything and FAKE_API_KEY not in everything

    @pytest.mark.parametrize("own", [dedupe.SetupError, dedupe.BackupError], ids=["setup-error", "backup-error"])
    def test_this_tools_own_errors_keep_their_text_with_secrets_scrubbed(self, own, tmp_path, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        text = str(nasty_error("planned failure"))

        def factory(cfg):
            raise own(text)

        code = dedupe.main([], config_loader=lambda: CFG, manager_factory=factory, backup_dir=tmp_path, now=FIXED_NOW)
        assert code == 1
        assert f"could not open the spreadsheet: {own.__name__}: planned failure" in caplog.text
        assert "[redacted]" in caplog.text
        assert_no_leak(capsys, caplog)

    def test_the_sheets_clients_error_keeps_its_text_once_that_module_is_loaded(self, monkeypatch):
        class SheetError(Exception):
            pass

        error = SheetError("the tab 'Registry_Projects' does not exist")
        assert dedupe.describe_error(error) == "SheetError (at unknown)"  # a class of that name, not the client's: hidden
        module = types.ModuleType("src.gsheet_manager")
        module.SheetError = SheetError
        monkeypatch.setitem(sys.modules, "src.gsheet_manager", module)
        assert dedupe.describe_error(error) == "SheetError: the tab 'Registry_Projects' does not exist"
        assert dedupe.describe_error(RuntimeError(LEAK)).startswith("RuntimeError (at ")  # other classes stay hidden

    def test_a_missing_sheets_module_leaves_the_own_types_working(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "src.gsheet_manager", types.ModuleType("src.gsheet_manager"))  # no SheetError
        assert dedupe.describe_error(dedupe.SetupError("SPREADSHEET_ID is not set")) == "SetupError: SPREADSHEET_ID is not set"


class TestBackupError:
    def test_it_is_both_an_oserror_and_a_valueerror(self):
        # write_backup's callers handle OSError, restore handles ValueError: both keep working
        assert issubclass(dedupe.BackupError, OSError) and issubclass(dedupe.BackupError, ValueError)

    def test_a_backup_for_another_tab_does_not_repeat_the_tab_the_file_names(self, tmp_path):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"tab": "MARKER-LEAK-Tab", "rows": []}), encoding="utf-8")
        with pytest.raises(dedupe.BackupError) as caught:
            dedupe.load_backup(path)
        assert str(caught.value) == "this backup is for another tab, not 'Registry_Projects'"

    def test_restore_names_why_the_file_cannot_be_used(self, tmp_path, caplog):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"tab": "MARKER-LEAK-Tab", "rows": []}), encoding="utf-8")
        assert run_cli(["--restore", str(path)], FakeManager([dld()]), tmp_path) == 2
        assert "cannot use the backup file: BackupError: this backup is for another tab, not 'Registry_Projects'" in caplog.text
        assert "MARKER-LEAK" not in caplog.text

    def test_a_failed_read_back_is_a_backup_error_the_caller_can_catch_as_oserror(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dedupe, "load_backup", lambda path, tab=TAB_REGISTRY: [dld("9999", "Different Tower")])
        with pytest.raises(OSError) as caught:
            dedupe.write_backup([dld(), srerd()], backup_dir=tmp_path, now=FIXED_NOW)
        assert isinstance(caught.value, dedupe.BackupError)
        assert dedupe.describe_error(caught.value) == "BackupError: backup read-back did not match what was written"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
class TestRestore:
    def make_backup(self, tmp_path, rows):
        return dedupe.write_backup(rows, backup_dir=tmp_path / "saved", now=FIXED_NOW)

    def test_restore_dry_run_changes_nothing(self, tmp_path, capsys):
        good = self.make_backup(tmp_path, [dld("1001"), dld("1002", "Other Tower"), srerd()])
        manager = FakeManager([dld("1001")])
        assert run_cli(["--restore", str(good)], manager, tmp_path) == 0
        assert "write_registry" not in manager.calls
        out = capsys.readouterr().out
        assert "3 row(s) from the backup would replace the 1 row(s)" in out and "DRY RUN" in out

    def test_restore_apply_backs_up_the_current_tab_then_replaces_it(self, tmp_path):
        good = self.make_backup(tmp_path, [dld("1001"), dld("1002", "Other Tower"), srerd()])
        manager = FakeManager([dld("1001")], backup_dir=tmp_path / "backups")
        assert run_cli(["--restore", str(good), "--apply"], manager, tmp_path) == 0
        assert sorted(r.project_key for r in manager.rows) == sorted(
            ["dld:dubai:1001", "dld:dubai:1002", "srerd:sharjah:S-77"]
        )
        assert len(manager.backups_at_write) == 1
        assert [r.project_key for r in dedupe.load_backup(manager.backups_at_write[0])] == ["dld:dubai:1001"]

    def test_restore_into_an_empty_tab(self, tmp_path):
        good = self.make_backup(tmp_path, [dld("1001")])
        manager = FakeManager([])
        assert run_cli(["--restore", str(good), "--apply"], manager, tmp_path) == 0
        assert len(manager.rows) == 1 and backups(tmp_path) == []

    def test_an_empty_backup_is_refused(self, tmp_path):
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"tab": TAB_REGISTRY, "rows": []}), encoding="utf-8")
        manager = FakeManager([dld("1001")])
        assert run_cli(["--restore", str(empty), "--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls

    def test_restore_is_refused_while_rows_are_unreadable(self, tmp_path):
        good = self.make_backup(tmp_path, [dld("1001")])
        manager = FakeManager([dld("1002", "Other Tower")], problems=["row 4: bad"])
        assert run_cli(["--restore", str(good), "--apply"], manager, tmp_path) == 2
        assert "write_registry" not in manager.calls

    def test_a_bad_backup_file_is_refused_before_the_sheet_is_opened(self, tmp_path):
        wrong = tmp_path / "wrong.json"
        wrong.write_text(json.dumps({"tab": "Listing_Data", "rows": []}), encoding="utf-8")

        def factory(cfg):
            raise AssertionError("the sheet must not be opened for a bad backup file")

        for argument in (str(wrong), str(tmp_path / "missing.json")):
            code = dedupe.main(
                ["--restore", argument], config_loader=lambda: CFG, manager_factory=factory, backup_dir=tmp_path, now=FIXED_NOW
            )
            assert code == 2

    def test_restore_verification_failure(self, tmp_path):
        good = self.make_backup(tmp_path, [dld("1001"), dld("1002", "Other Tower")])
        manager = FakeManager([dld("1003", "Third Tower")], drop_on_write=1)
        assert run_cli(["--restore", str(good), "--apply"], manager, tmp_path) == 1


# ---------------------------------------------------------------------------
# SheetManager is optional at import time and never creates a spreadsheet
# ---------------------------------------------------------------------------
class TestSheetManagerAccess:
    def fake_module(self, monkeypatch):
        created = []

        class FakeSheetManager:
            def __init__(self, cfg, **kwargs):
                created.append(("init", cfg.spreadsheet_id))

            def open(self, *, read_only=False):
                created.append(("open", read_only))

        module = types.ModuleType("src.gsheet_manager")
        module.SheetManager = FakeSheetManager
        monkeypatch.setitem(sys.modules, "src.gsheet_manager", module)
        return created

    def test_importing_the_script_does_not_import_the_sheets_client(self):
        code = (
            "import sys, scripts.dedupe_sheet; "
            "bad = [m for m in ('src.gsheet_manager', 'gspread') if m in sys.modules]; "
            "sys.exit(1 if bad else 0)"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, timeout=120)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def test_without_a_spreadsheet_id_nothing_is_created(self, monkeypatch):
        created = self.fake_module(monkeypatch)
        with pytest.raises(dedupe.SetupError, match="SPREADSHEET_ID"):
            dedupe.open_sheet_manager(Config())
        assert created == []

    def test_with_an_id_it_opens_the_existing_sheet(self, monkeypatch):
        created = self.fake_module(monkeypatch)
        dedupe.open_sheet_manager(Config(spreadsheet_id=FAKE_SHEET_ID))
        assert created == [("init", FAKE_SHEET_ID), ("open", True)]  # read-only: a dry run must write nothing

    def test_a_missing_sheets_module_is_a_clear_failure(self, monkeypatch, tmp_path, capsys, caplog):
        monkeypatch.setitem(sys.modules, "src.gsheet_manager", None)
        with pytest.raises(dedupe.SetupError, match="could not be loaded"):
            dedupe.open_sheet_manager(Config(spreadsheet_id=FAKE_SHEET_ID))
        caplog.set_level(logging.DEBUG)
        code = dedupe.main(
            [], config_loader=lambda: CFG, manager_factory=dedupe.open_sheet_manager, backup_dir=tmp_path, now=FIXED_NOW
        )
        assert code == 1
        assert_no_leak(capsys, caplog)


# ---------------------------------------------------------------------------
# Contract: the script against the REAL SheetManager over the in-memory fake
# gspread (no network). The FakeManager above proves the script's own logic;
# these prove the two agree on what read_registry, write_registry and open do.
# ---------------------------------------------------------------------------
INTEGRATION_SHEET_ID = "fake-spreadsheet-id-integration-0001"


@pytest.fixture()
def real_sheet():
    """(fake client, opened SheetManager, Config) over a spreadsheet that exists but has no tabs yet."""
    from src.gsheet_manager import SheetManager
    from tests import fake_gspread

    client = fake_gspread.FakeClient()
    client.add_spreadsheet("Test Tracker", sheet_id=INTEGRATION_SHEET_ID, tabs={})
    cfg = Config(
        spreadsheet_id=INTEGRATION_SHEET_ID, gcp_service_account_key=FAKE_SA_JSON, data_dubai_api_key=FAKE_API_KEY
    )
    manager = SheetManager(cfg, client=client, sleep=lambda seconds: None)
    manager.open()
    return client, manager, cfg


def run_real(argv, client_manager_cfg, tmp_path):
    _, manager, cfg = client_manager_cfg
    return dedupe.main(
        argv,
        config_loader=lambda: cfg,
        manager_factory=lambda _cfg: manager,
        backup_dir=tmp_path / "backups",
        now=FIXED_NOW,
    )


def registry_values(client):
    return client.spreadsheets[INTEGRATION_SHEET_ID].tab(TAB_REGISTRY).dump()


class TestAgainstTheRealSheetManager:
    def test_apply_merges_the_duplicates_in_the_sheet(self, real_sheet, tmp_path, capsys):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        assert run_real(["--apply"], real_sheet, tmp_path) == 0
        rows, problems = manager.read_registry()
        assert problems == [] and [r.project_key for r in rows] == ["dld:dubai:1001", "dld:dubai:2002"]
        assert rows[0].area_en == "Business Bay"  # the survivor gained the registry row's facts
        assert len(registry_values(client)) == 3  # header + 2 data rows, nothing left over below
        assert len(list((tmp_path / "backups").glob("*.json"))) == 1
        assert "Done: removed 1 duplicate row(s)" in capsys.readouterr().out

    def test_a_dry_run_changes_nothing_in_the_sheet(self, real_sheet, tmp_path):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        before = registry_values(client)
        client.reset_calls()
        assert run_real([], real_sheet, tmp_path) == 0
        assert registry_values(client) == before
        assert client.calls_to("update") == [] and client.calls_to("delete_rows") == []

    def test_an_unreadable_row_blocks_apply_and_leaves_the_sheet_alone(self, real_sheet, tmp_path):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        ws = client.spreadsheets[INTEGRATION_SHEET_ID].tab(TAB_REGISTRY)
        values = ws.dump()
        bad = [""] * len(values[0])
        bad[values[0].index("Emirate")], bad[values[0].index("Registry")] = "Abu Dhabi", "DLD"
        ws.put(values + [bad])
        before = registry_values(client)
        client.reset_calls()
        assert run_real(["--apply"], real_sheet, tmp_path) == 2
        assert registry_values(client) == before and client.calls_to("update") == []

    def test_hand_added_columns_follow_their_row_and_losses_are_reported(self, real_sheet, tmp_path, capsys):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        ws = client.spreadsheets[INTEGRATION_SHEET_ID].tab(TAB_REGISTRY)
        values = ws.dump()
        notes = ["Analyst Note", "note on the seed row", "note on the other tower", "note on the registry row"]
        ws.put([row + [note] for row, note in zip(values, notes)])
        assert run_real(["--apply"], real_sheet, tmp_path) == 0
        after = registry_values(client)
        column = after[0].index("Analyst Note")
        kept = [row[column] for row in after[1:]]
        assert "note on the other tower" in kept and len(kept) == 2
        # Two duplicate rows both carried a note; only one can survive, and the script says so.
        assert "could not be carried over for 1 row(s)" in capsys.readouterr().out

    def test_a_quota_error_exits_one_and_the_backup_holds_the_original_rows(self, real_sheet, tmp_path, caplog):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        caplog.set_level(logging.INFO)
        client.inject("update", 429, times=None, tab=TAB_REGISTRY)
        assert run_real(["--apply"], real_sheet, tmp_path) == 1
        saved = dedupe.load_backup(next((tmp_path / "backups").glob("*.json")))
        assert saved == duplicate_pair_rows()
        assert "may be partly written" in caplog.text

    def test_a_server_error_full_of_secrets_prints_none(self, real_sheet, tmp_path, capsys, caplog):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        caplog.set_level(logging.DEBUG)
        # The id in the message is the one this sheet is really configured with.
        poison = f"backend said key={FAKE_API_KEY} Authorization: Bearer {FAKE_BEARER} sheet {INTEGRATION_SHEET_ID}"
        client.inject("update", 500, message=poison, times=None, tab=TAB_REGISTRY)
        assert run_real(["--apply"], real_sheet, tmp_path) == 1
        assert_no_leak(capsys, caplog, INTEGRATION_SHEET_ID)

    def test_a_clean_tab_is_left_alone(self, real_sheet, tmp_path, capsys):
        client, manager, _ = real_sheet
        manager.write_registry([dld("1001"), dld("1002", "Other Tower")])
        client.reset_calls()
        assert run_real(["--apply"], real_sheet, tmp_path) == 0
        assert client.calls_to("update") == []
        assert "No duplicates found" in capsys.readouterr().out

    def test_an_empty_registry_is_not_an_error(self, real_sheet, tmp_path, capsys):
        assert run_real(["--apply"], real_sheet, tmp_path) == 0
        assert "Nothing to do" in capsys.readouterr().out

    def test_restore_puts_the_backed_up_rows_back(self, real_sheet, tmp_path):
        client, manager, _ = real_sheet
        manager.write_registry(duplicate_pair_rows())
        assert run_real(["--apply"], real_sheet, tmp_path) == 0
        backup = next((tmp_path / "backups").glob("*.json"))
        assert run_real(["--restore", str(backup), "--apply"], real_sheet, tmp_path) == 0
        rows, problems = manager.read_registry()
        assert problems == [] and len(rows) == 3
        assert sorted(r.project_key for r in rows) == sorted(r.project_key for r in duplicate_pair_rows())

    def test_open_sheet_manager_opens_the_existing_sheet_by_id(self, real_sheet, monkeypatch):
        from src.gsheet_manager import SheetManager

        client, _, cfg = real_sheet
        monkeypatch.setattr(SheetManager, "_build_client", lambda self: client)
        manager = dedupe.open_sheet_manager(cfg)
        assert isinstance(manager, SheetManager) and manager.created_spreadsheet is False
        assert list(client.spreadsheets) == [INTEGRATION_SHEET_ID]  # nothing new was created

    MUTATIONS = ("update", "append_rows", "delete_rows", "clear", "resize", "freeze", "update_index",
                 "add_worksheet", "del_worksheet", "share", "create")

    def _old_schema_sheet(self, monkeypatch):
        """An existing spreadsheet as an older release left it: only the registry tab, last 3 columns missing."""
        from src.gsheet_manager import SheetManager
        from tests import fake_gspread

        client = fake_gspread.FakeClient()
        old = list(REGISTRY_HEADERS)[:-3]
        rows = [dld("1001").to_sheet_dict(), seed().to_sheet_dict()]
        client.add_spreadsheet(
            "Old", sheet_id=INTEGRATION_SHEET_ID, tabs={TAB_REGISTRY: [old] + [[r.get(h, "") for h in old] for r in rows]}
        )
        monkeypatch.setattr(SheetManager, "_build_client", lambda self: client)
        cfg = Config(spreadsheet_id=INTEGRATION_SHEET_ID, gcp_service_account_key=FAKE_SA_JSON)
        return client, cfg

    def _run_cli(self, argv, cfg, tmp_path):
        return dedupe.main(
            argv, config_loader=lambda: cfg, manager_factory=dedupe.open_sheet_manager,
            backup_dir=tmp_path / "backups", now=FIXED_NOW,
        )

    def test_a_dry_run_through_the_real_open_writes_nothing_at_all(self, monkeypatch, tmp_path, capsys):
        """It prints "nothing was changed", so it must not add tabs or columns on its way in."""
        client, cfg = self._old_schema_sheet(monkeypatch)
        sheet = client.spreadsheets[INTEGRATION_SHEET_ID]
        before = {ws.title: ws.dump() for ws in sheet._sheets}
        assert self._run_cli([], cfg, tmp_path) == 0
        assert [c.method for c in client.calls if c.method in self.MUTATIONS] == []
        assert {ws.title: ws.dump() for ws in sheet._sheets} == before
        assert "DRY RUN: nothing was changed" in capsys.readouterr().out

    def test_apply_touches_only_the_registry_tab(self, monkeypatch, tmp_path):
        client, cfg = self._old_schema_sheet(monkeypatch)
        sheet = client.spreadsheets[INTEGRATION_SHEET_ID]
        assert self._run_cli(["--apply"], cfg, tmp_path) == 0
        assert sheet.titles() == [TAB_REGISTRY]  # a clean-up tool does not create the tracker's other tabs
        assert len(sheet.tab(TAB_REGISTRY).dump()) == 2  # header + the one merged project

    def test_a_missing_registry_tab_is_reported_not_created(self, monkeypatch, tmp_path, caplog):
        from src.gsheet_manager import SheetManager
        from tests import fake_gspread

        client = fake_gspread.FakeClient()
        client.add_spreadsheet("Other", sheet_id=INTEGRATION_SHEET_ID, tabs={"Notes": [["x"]]})
        monkeypatch.setattr(SheetManager, "_build_client", lambda self: client)
        cfg = Config(spreadsheet_id=INTEGRATION_SHEET_ID, gcp_service_account_key=FAKE_SA_JSON)
        assert self._run_cli(["--apply"], cfg, tmp_path) == 1
        assert [c.method for c in client.calls if c.method in self.MUTATIONS] == []
        assert "does not exist" in caplog.text

    def test_open_sheet_manager_without_an_id_creates_nothing(self, real_sheet, monkeypatch):
        from src.gsheet_manager import SheetManager

        client, _, _ = real_sheet
        monkeypatch.setattr(SheetManager, "_build_client", lambda self: client)
        with pytest.raises(dedupe.SetupError):
            dedupe.open_sheet_manager(Config(gcp_service_account_key=FAKE_SA_JSON))
        assert list(client.spreadsheets) == [INTEGRATION_SHEET_ID]


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------
def test_say_degrades_on_a_console_that_cannot_show_a_character(monkeypatch):
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    dedupe._say("row 3: برج المثال bad")
    sys.stdout.flush()
    # three letters, a space, six letters: every Arabic letter becomes one "?"
    assert raw.getvalue().decode("ascii").strip() == "row 3: ??? ?????? bad"
