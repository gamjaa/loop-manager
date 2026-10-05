"""Durable, input-bound whole-Requirement judgments."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_judgments (
    repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    input_token TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('fulfilled', 'unfulfilled', 'uncertain')),
    criteria_evidence TEXT NOT NULL,
    relevant_paths TEXT NOT NULL,
    assessed_sha TEXT NOT NULL,
    invalidation_reason TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, source_path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

MOVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_move_receipts (
    repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    source_oid TEXT NOT NULL,
    judgment_token TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('pending', 'published')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, source_path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS requirement_audit (
    repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    input_token TEXT NOT NULL,
    source_oid TEXT NOT NULL,
    observed_sha TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'restore', 'uncertain', 'fulfilled', 'superseded')),
    reason TEXT NOT NULL,
    criteria TEXT NOT NULL DEFAULT '[]',
    criteria_evidence TEXT NOT NULL DEFAULT '[]',
    relevant_paths TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL CHECK (stage IN ('observed', 'staged', 'published')),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, source_path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class Judgment:
    source_path: str
    input_token: str
    outcome: str
    criteria_evidence: tuple[str, ...]
    relevant_paths: tuple[str, ...]
    assessed_sha: str
    invalidation_reason: str | None = None


class RequirementJudgmentRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute(SCHEMA)
        connection.execute(MOVE_SCHEMA)
        connection.execute(AUDIT_SCHEMA)

    def get(self, repo: str, path: str) -> Judgment | None:
        row = self.connection.execute(
            "SELECT * FROM requirement_judgments WHERE repo = ? AND source_path = ?",
            (repo, path),
        ).fetchone()
        if row is None:
            return None
        return Judgment(
            str(row["source_path"]), str(row["input_token"]), str(row["outcome"]),
            tuple(json.loads(str(row["criteria_evidence"]))),
            tuple(json.loads(str(row["relevant_paths"]))),
            str(row["assessed_sha"]), row["invalidation_reason"],
        )

    def put(self, repo: str, judgment: Judgment) -> None:
        self.connection.execute(
            "INSERT INTO requirement_judgments "
            "(repo, source_path, input_token, outcome, criteria_evidence, "
            "relevant_paths, assessed_sha, invalidation_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo, source_path) DO UPDATE SET "
            "input_token=excluded.input_token, outcome=excluded.outcome, "
            "criteria_evidence=excluded.criteria_evidence, "
            "relevant_paths=excluded.relevant_paths, assessed_sha=excluded.assessed_sha, "
            "invalidation_reason=excluded.invalidation_reason, "
            "updated_at=CURRENT_TIMESTAMP",
            (
                repo, judgment.source_path, judgment.input_token, judgment.outcome,
                json.dumps(judgment.criteria_evidence, ensure_ascii=False),
                json.dumps(judgment.relevant_paths, ensure_ascii=False),
                judgment.assessed_sha, judgment.invalidation_reason,
            ),
        )

    def invalidate(self, repo: str, path: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE requirement_judgments SET invalidation_reason = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND source_path = ? "
            "AND invalidation_reason IS NULL",
            (reason, repo, path),
        )
