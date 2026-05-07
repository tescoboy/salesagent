"""Unit tests for the per-principal billing_enabled gate (BR-RULE-061).

Slice 4 of the per-buyer-agent refactor. ``Principal.billing_enabled``
controls whether a buyer agent is allowed to be the billing party on any
Account it owns.

Rules tested here:

* ``billing_enabled=True`` (default) + ``billing="agent"`` → accepted
* ``billing_enabled=False`` + ``billing="agent"`` → rejected with
  ``BILLING_NOT_SUPPORTED``
* ``billing_enabled=False`` + ``billing="operator"`` → accepted
  (the gate only fires for ``"agent"``; operator-paid accounts are always
  allowed regardless of the agent's flag)
* tenant-level ``supported_billing`` filter (BR-RULE-059) still applies
  AND short-circuits before the principal-level check
"""

from __future__ import annotations

from unittest.mock import patch

from src.core.resolved_identity import ResolvedIdentity
from src.core.tools.accounts import _check_billing_policy


def _identity(*, principal_id: str = "p1", tenant_id: str = "t1", supported: list[str] | None = None) -> ResolvedIdentity:
    return ResolvedIdentity(
        principal_id=principal_id,
        tenant_id=tenant_id,
        tenant={
            "tenant_id": tenant_id,
            "supported_billing": supported,
        },
        protocol="mcp",
    )


class TestTenantPolicy:
    """BR-RULE-059 — tenant-level supported_billing filter."""

    def test_billing_in_supported_list_accepted(self):
        identity = _identity(supported=["operator", "agent"])
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = True
            result = _check_billing_policy("operator", identity)
        assert result is None

    def test_billing_not_in_supported_list_rejected(self):
        identity = _identity(supported=["operator"])
        result = _check_billing_policy("agent", identity)
        assert result is not None
        assert result[0].code == "BILLING_NOT_SUPPORTED"
        assert "not supported by this seller" in result[0].message

    def test_no_supported_list_means_no_tenant_filter(self):
        # supported_billing not configured → tenant filter accepts; falls
        # through to the principal-level check below.
        identity = _identity(supported=None)
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = True
            result = _check_billing_policy("agent", identity)
        assert result is None


class TestPrincipalBillingEnabled:
    """BR-RULE-061 — per-principal billing_enabled gate."""

    def test_billing_agent_accepted_when_principal_enabled(self):
        identity = _identity()
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = True
            result = _check_billing_policy("agent", identity)
        assert result is None

    def test_billing_agent_rejected_when_principal_disabled(self):
        identity = _identity()
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = False
            result = _check_billing_policy("agent", identity)
        assert result is not None
        assert result[0].code == "BILLING_NOT_SUPPORTED"
        assert "not authorized to be billed" in result[0].message
        assert "billing='operator'" in result[0].suggestion

    def test_billing_operator_accepted_regardless_of_principal_flag(self):
        # billing="operator" should NEVER hit the principal gate — the gate
        # only fires for billing="agent".
        identity = _identity()
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = False
            result = _check_billing_policy("operator", identity)
        assert result is None

    def test_billing_null_accepted_regardless_of_principal_flag(self):
        # billing=NULL (untyped) likewise bypasses the agent gate.
        identity = _identity()
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = False
            result = _check_billing_policy(None, identity)
        assert result is None

    def test_principal_lookup_failure_fails_closed(self):
        # If the principal can't be found, treat as billing-disabled (None
        # is falsy). Never silently allow a billing-disabled agent through.
        identity = _identity()
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            mock_db.return_value.__enter__.return_value.scalars.return_value.first.return_value = None
            result = _check_billing_policy("agent", identity)
        assert result is not None
        assert result[0].code == "BILLING_NOT_SUPPORTED"


class TestGateOrdering:
    """Tenant filter must short-circuit before the principal filter — when
    billing isn't supported by the seller at all, we don't need to ask the DB
    about the principal."""

    def test_tenant_filter_runs_before_principal_lookup(self):
        identity = _identity(supported=["operator"])  # excludes "agent"
        with patch("src.core.database.database_session.get_db_session") as mock_db:
            result = _check_billing_policy("agent", identity)
        assert result is not None
        assert result[0].code == "BILLING_NOT_SUPPORTED"
        assert "Supported models: operator" in result[0].message
        # No DB session opened — tenant filter caught it first.
        mock_db.assert_not_called()
