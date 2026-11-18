"""Shared pytest configuration: isolate audit logs and JWT secrets from the real environment."""

from __future__ import annotations

import pytest

TEST_JWT_SECRET = "ops-bridge-test-only-hs256-secret-32b"


@pytest.fixture(autouse=True)
def _isolated_runtime_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Never append test traffic to the repo's data/audit.jsonl
    monkeypatch.setenv("OPS_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("OPS_JWT_SECRET", TEST_JWT_SECRET)
    # Start every test from the least-privileged default unless a test opts in
    for var in ("X_OPERATOR_ROLE", "OPS_OPERATOR_ROLE", "OPS_OPERATOR_JWT"):
        monkeypatch.delenv(var, raising=False)
    yield
