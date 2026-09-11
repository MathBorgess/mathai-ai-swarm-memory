"""Persistent propose outbox; GitHub tokens never reach SQL."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.proposals.schema import (
    ProposalPayloadError,
    assert_branch,
    assert_write_path,
    derive_branch,
    derive_path,
    proposal_id_ok,
)


STATUSES = ("pending", "branch_created", "committed", "pr_opened")


class IdempotencyConflict(ValueError):
    """Same Idempotency-Key with a different payload hash."""


def _aware(at: datetime) -> None:
    if at.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")


@dataclass(frozen=True)
class ProposalRecord:
    proposal_id: str
    workspace_id: str
    principal_id: str
    idempotency_key: str
    payload_hash: str
    namespace: str
    title: str
    path: str
    branch: str
    note: str
    status: str
    branch_sha: str | None
    commit_sha: str | None
    pr_number: int | None
    pr_url: str | None
    created_at: datetime
    updated_at: datetime


class ProposalStore:
    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(
            path, timeout=30, check_same_thread=False, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.Lock()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS proposal_outbox (
                proposal_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                namespace TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                branch TEXT NOT NULL,
                note TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending', 'branch_created', 'committed', 'pr_opened'
                )),
                branch_sha TEXT,
                commit_sha TEXT,
                pr_number INTEGER,
                pr_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (workspace_id, principal_id, idempotency_key)
            );
        """)

    def close(self) -> None:
        self.connection.close()

    def _row(self, row: sqlite3.Row) -> ProposalRecord:
        return ProposalRecord(
            proposal_id=row["proposal_id"],
            workspace_id=row["workspace_id"],
            principal_id=row["principal_id"],
            idempotency_key=row["idempotency_key"],
            payload_hash=row["payload_hash"],
            namespace=row["namespace"],
            title=row["title"],
            path=row["path"],
            branch=row["branch"],
            note=row["note"],
            status=row["status"],
            branch_sha=row["branch_sha"],
            commit_sha=row["commit_sha"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get(self, proposal_id: str) -> ProposalRecord | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM proposal_outbox WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return None if row is None else self._row(row)

    def claim(
        self,
        *,
        workspace_id: str,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
        proposal_id: str,
        namespace: str,
        title: str,
        note: str,
        now: datetime,
    ) -> ProposalRecord:
        _aware(now)
        if not workspace_id or not principal_id or not idempotency_key or not payload_hash:
            raise ValueError("Invalid outbox claim")
        if not proposal_id_ok(proposal_id):
            raise ProposalPayloadError("Invalid proposal id")
        path = derive_path(namespace, proposal_id, now)
        branch = derive_branch(proposal_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self.connection.execute(
                    """SELECT * FROM proposal_outbox
                       WHERE workspace_id = ? AND principal_id = ? AND idempotency_key = ?""",
                    (workspace_id, principal_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise IdempotencyConflict("Idempotency key reused with a different payload")
                    self.connection.commit()
                    return self._row(existing)
                try:
                    self.connection.execute(
                        """INSERT INTO proposal_outbox VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?, ?
                        )""",
                        (
                            proposal_id, workspace_id, principal_id, idempotency_key, payload_hash,
                            namespace, title, path, branch, note, now.isoformat(), now.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    raced = self.connection.execute(
                        """SELECT * FROM proposal_outbox
                           WHERE workspace_id = ? AND principal_id = ? AND idempotency_key = ?""",
                        (workspace_id, principal_id, idempotency_key),
                    ).fetchone()
                    if raced is None:
                        raise
                    if raced["payload_hash"] != payload_hash:
                        raise IdempotencyConflict(
                            "Idempotency key reused with a different payload"
                        ) from None
                    self.connection.commit()
                    return self._row(raced)
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        record = self.get(proposal_id)
        if record is None:
            raise RuntimeError("Outbox insert vanished")
        return record

    def mark_branch(self, proposal_id: str, branch_sha: str, now: datetime) -> ProposalRecord:
        return self._advance(proposal_id, now, "branch_created", branch_sha=branch_sha)

    def mark_commit(self, proposal_id: str, commit_sha: str, now: datetime) -> ProposalRecord:
        return self._advance(proposal_id, now, "committed", commit_sha=commit_sha)

    def mark_pr(self, proposal_id: str, pr_number: int, pr_url: str, now: datetime) -> ProposalRecord:
        if not isinstance(pr_number, int) or pr_number <= 0 or not pr_url.startswith("https://"):
            raise ValueError("Invalid pull request metadata")
        return self._advance(proposal_id, now, "pr_opened", pr_number=pr_number, pr_url=pr_url)

    def _advance(self, proposal_id: str, now: datetime, status: str, **fields) -> ProposalRecord:
        _aware(now)
        assignments = ["status = ?", "updated_at = ?"]
        values: list[object] = [status, now.isoformat()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(proposal_id)
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute(
                    "SELECT * FROM proposal_outbox WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown proposal")
                current = STATUSES.index(row["status"])
                target = STATUSES.index(status)
                if current > target:
                    self.connection.commit()
                    return self._row(row)
                self.connection.execute(
                    f"UPDATE proposal_outbox SET {', '.join(assignments)} WHERE proposal_id = ?",
                    values,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        record = self.get(proposal_id)
        if record is None:
            raise RuntimeError("Outbox update vanished")
        assert_write_path(record.path)
        assert_branch(record.branch)
        return record
