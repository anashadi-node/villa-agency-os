"""Anchor publication tests: truncation and tampering must be detectable.

These tests are written against the *failure* cases, because the happy path of
"write a file with two numbers" is worthless on its own: an anchor is only useful
if a shortened or rewritten audit chain is caught afterwards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import anchors as A
from audit import AuditLog
from primitives import DomainError

START = "2026-09-21T08:00:00Z"


def _audit(conn, clock, ids):
    return AuditLog(conn, clock=clock, ids=ids)


def _append(audit, n: int = 3) -> None:
    for index in range(n):
        audit.append(
            action="booking.confirmed",
            entity_type="booking",
            entity_id=f"bkg_{index}",
            actor="agent_ana",
            actor_role="agent",
        )


def _detach_triggers(conn) -> None:
    """Simulate an attacker who owns the database file (tests only)."""

    conn.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_delete")
    conn.execute("DROP TRIGGER IF EXISTS trg_audit_events_no_update")


def _count_files(directory: Path) -> int:
    return len([p for p in directory.iterdir() if p.is_file()])


class TestPublishAnchor:
    """Publishing must bless a chain, never a story that was edited."""


    def test_publishing_is_itself_recorded_in_the_chain(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        published = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        events = audit.events(entity_type=A.ANCHOR_ENTITY_TYPE)
        assert [e.action for e in events] == [A.ANCHOR_AUDIT_ACTION]
        assert events[0].entity_id == published.file_name

    def test_second_publish_links_to_the_first(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        _append(audit, 2)
        clock.advance(minutes=61)
        second = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        raw = json.loads((tmp_path / second.file_name).read_text(encoding="utf-8"))
        assert raw["previous_file"] == first.file_name
        assert raw["previous_sha256"] == first.file_sha256
        assert second.count >= first.count
        assert second.file_name != first.file_name

    def test_head_moved_backwards_is_refused(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 3)
        first = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        _detach_triggers(conn)
        conn.execute("DELETE FROM audit_events WHERE seq > 3")
        clock.advance(minutes=5)
        with pytest.raises(DomainError) as caught:
            A.publish_anchor(
                conn=conn, audit=audit, clock=clock, directory=tmp_path,
                actor="owner_anas", actor_role="owner",
            )
        # Either the head moved backwards or the recorded anchor vanished: both are
        # reasons to refuse. What matters is that nothing new was blessed.
        assert caught.value.code in {
            "ANCHOR_ROLLBACK", "ANCHOR_REWRITTEN", "ANCHOR_NOT_RECORDED",
        }
        assert _count_files(tmp_path) == 1
        assert first.count > 0

    def test_head_rewritten_at_the_same_length_is_refused(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 2)
        A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        _detach_triggers(conn)
        conn.execute("DELETE FROM audit_events WHERE seq > 2")
        _append(audit, 1)
        clock.advance(minutes=5)
        with pytest.raises(DomainError) as caught:
            A.publish_anchor(
                conn=conn, audit=audit, clock=clock, directory=tmp_path,
                actor="owner_anas", actor_role="owner",
            )
        assert caught.value.code in {
            "ANCHOR_REWRITTEN", "ANCHOR_NOT_RECORDED", "ANCHOR_ROLLBACK",
        }
        assert _count_files(tmp_path) == 1

    def test_publish_refuses_a_broken_chain(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 3)
        _detach_triggers(conn)
        conn.execute("DELETE FROM audit_events WHERE seq = 2")
        with pytest.raises(DomainError) as caught:
            A.publish_anchor(
                conn=conn, audit=audit, clock=clock, directory=tmp_path,
                actor="owner_anas", actor_role="owner",
            )
        assert caught.value.code in {"AUDIT_SEQUENCE_GAP", "AUDIT_CHAIN_BROKEN"}
        assert _count_files(tmp_path) == 0

    def test_publish_refuses_when_previous_anchor_file_was_edited(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        path = tmp_path / first.file_name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["label"] = "edited after publication"
        path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        _append(audit, 1)
        clock.advance(minutes=5)
        with pytest.raises(A.AnchorFileTampered) as caught:
            A.publish_anchor(
                conn=conn, audit=audit, clock=clock, directory=tmp_path,
                actor="owner_anas", actor_role="owner",
            )
        assert caught.value.code == "ANCHOR_FILE_CHANGED"
        assert _count_files(tmp_path) == 1

    def test_publish_needs_an_existing_directory(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        missing = tmp_path / "tidak-ada"
        with pytest.raises(A.AnchorDirectoryMissing):
            A.publish_anchor(
                conn=conn, audit=audit, clock=clock, directory=missing,
                actor="owner_anas", actor_role="owner",
            )
        assert _count_files(tmp_path) == 0

    def test_publish_leaves_no_temporary_file(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


class TestVerifyPublishedAnchors:

    def test_verification_passes_right_after_publish(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 2)
        published = A.publish_anchor(
            conn=conn, audit=audit, clock=clock, directory=tmp_path,
            actor="owner_anas", actor_role="owner",
        )
        report = A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert report.issues == ()
        assert report.count == published.count
        assert report.head_hash == published.head_hash
        assert report.files_checked == 1

    def test_verification_passes_after_a_second_publish(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                         actor="owner_anas", actor_role="owner")
        _append(audit, 1)
        clock.advance(minutes=30)
        A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                         actor="owner_anas", actor_role="owner")
        report = A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert report.issues == ()
        assert report.files_checked == 2

    def test_detects_a_chain_shorter_than_the_anchor(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 4)
        A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                         actor="owner_anas", actor_role="owner")
        _detach_triggers(conn)
        conn.execute("DELETE FROM audit_events WHERE seq >= 3")
        with pytest.raises(DomainError) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code in {"AUDIT_ANCHOR_MISMATCH", "ANCHOR_NOT_RECORDED"}

    def test_detects_a_rewrite_at_the_same_length(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 2)
        A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                         actor="owner_anas", actor_role="owner")
        anchor_count = len(list(audit.events()))
        _detach_triggers(conn)
        conn.execute("DELETE FROM audit_events WHERE seq > ?", (anchor_count - 1,))
        _append(audit, 1)
        with pytest.raises(DomainError) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code in {"AUDIT_ANCHOR_MISMATCH", "ANCHOR_NOT_RECORDED"}

    def test_detects_anchor_file_edited_but_head_kept_valid(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        path = tmp_path / first.file_name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["label"] = "not what was published"
        path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(A.AnchorFileTampered) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code == "ANCHOR_FILE_CHANGED"

    def test_detects_anchor_file_whose_head_was_rewritten(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        path = tmp_path / first.file_name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["head_hash"] = "0" * 64
        path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(DomainError) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code in {
            "AUDIT_ANCHOR_MISMATCH", "ANCHOR_FILE_CHANGED", "ANCHOR_CHAIN_BROKEN",
        }

    def test_detects_a_removed_first_anchor_file(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        _append(audit, 1)
        clock.advance(minutes=45)
        A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                         actor="owner_anas", actor_role="owner")
        (tmp_path / first.file_name).unlink()
        with pytest.raises(A.AnchorFileTampered) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code == "ANCHOR_CHAIN_BROKEN"

    def test_detects_an_inserted_anchor_file(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        _append(audit, 1)
        clock.advance(minutes=45)
        second = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                  actor="owner_anas", actor_role="owner")
        injected = tmp_path / "anchor-20260101T000000Z-00000000.json"
        injected.write_text((tmp_path / first.file_name).read_text(encoding="utf-8"),
                            encoding="utf-8")
        with pytest.raises(A.AnchorFileTampered) as caught:
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)
        assert caught.value.code == "ANCHOR_CHAIN_BROKEN"
        assert second.file_name != injected.name

    def test_non_strict_mode_reports_instead_of_raising(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        path = tmp_path / first.file_name
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["label"] = "changed"
        path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
        report = A.verify_published_anchors(
            conn=conn, audit=audit, directory=tmp_path, strict=False,
        )
        assert report.issues
        assert any("ANCHOR_FILE_CHANGED" in issue for issue in report.issues)

    def test_verify_without_any_anchor_is_refused(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        with pytest.raises(A.AnchorNotFound):
            A.verify_published_anchors(conn=conn, audit=audit, directory=tmp_path)

    def test_verify_refuses_a_missing_directory(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        with pytest.raises(A.AnchorDirectoryMissing):
            A.verify_published_anchors(
                conn=conn, audit=audit, directory=tmp_path / "tidak-ada",
            )

    def test_anchor_file_carries_no_guest_data(self, conn, clock, ids, tmp_path):
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        raw = json.loads((tmp_path / first.file_name).read_text(encoding="utf-8"))
        assert set(raw) == {
            "schema", "published_at", "count", "head_hash",
            "previous_file", "previous_sha256", "label",
        }
        blob = json.dumps(raw)
        for forbidden in ("guest", "email", "phone", "nama", "passport"):
            assert forbidden not in blob.lower()

    def test_latest_published_returns_none_then_the_newest(self, conn, clock, ids, tmp_path):
        assert A.latest_published(tmp_path) is None
        audit = _audit(conn, clock, ids)
        _append(audit, 1)
        first = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                 actor="owner_anas", actor_role="owner")
        _append(audit, 1)
        clock.advance(minutes=15)
        second = A.publish_anchor(conn=conn, audit=audit, clock=clock, directory=tmp_path,
                                  actor="owner_anas", actor_role="owner")
        assert A.latest_published(tmp_path).file_name == second.file_name
        assert A.latest_published(tmp_path).file_name != first.file_name
