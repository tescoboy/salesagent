"""End-to-end config round-trip for Triton + FreeWheel.

Verifies the full lifecycle that breaks if any of the wiring layers (schema
validation, encryption, persistence, rehydration, tenant_status reporting)
drifts:

  payload → save_adapter_config → AdapterConfig.config_json → ciphertext at rest
  ciphertext at rest → tenant_status check → is_configured=True
  ciphertext at rest → connection_config_class.model_validate → plaintext in memory

This catches wiring bugs (broken templates, missing fields, schema drift)
without requiring real adapter credentials.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from src.adapters.freewheel import FreeWheelConnectionConfig
from src.adapters.triton import TritonConnectionConfig
from src.core.database.database_session import get_db_session
from src.core.database.models import AdapterConfig
from src.core.database.models import Tenant as ModelTenant
from src.core.tenant_status import get_tenant_status, is_tenant_ad_server_configured
from src.core.utils.encryption import is_encrypted

_TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()


@pytest.fixture
def _encryption_key():
    with patch.dict(os.environ, {"ENCRYPTION_KEY": _TEST_ENCRYPTION_KEY}):
        yield


@pytest.fixture
def _tenant(integration_db, _encryption_key):
    from tests.utils.database_helpers import create_tenant_with_timestamps

    tenant_id = "tenant_cfg_roundtrip"
    with get_db_session() as session:
        tenant = create_tenant_with_timestamps(
            tenant_id=tenant_id,
            name="Config Roundtrip",
            subdomain="cfg-roundtrip",
            ad_server="mock",
            is_active=True,
        )
        session.add(tenant)
        session.commit()

    yield tenant_id

    with get_db_session() as session:
        session.execute(delete(AdapterConfig).where(AdapterConfig.tenant_id == tenant_id))
        session.execute(delete(ModelTenant).where(ModelTenant.tenant_id == tenant_id))
        session.commit()


def _persist_adapter_config(tenant_id: str, adapter_type: str, config_json: dict) -> None:
    """Persist a validated config_json for the tenant's adapter."""
    with get_db_session() as session:
        existing = session.scalars(select(AdapterConfig).filter_by(tenant_id=tenant_id)).first()
        if existing:
            existing.adapter_type = adapter_type
            existing.config_json = config_json
        else:
            session.add(AdapterConfig(tenant_id=tenant_id, adapter_type=adapter_type, config_json=config_json))
        session.commit()
    # Update tenant.ad_server so tenant_status checks the right adapter
    with get_db_session() as session:
        tenant = session.scalars(select(ModelTenant).filter_by(tenant_id=tenant_id)).first()
        tenant.ad_server = adapter_type
        session.commit()


@pytest.mark.integration
@pytest.mark.requires_db
class TestTritonConfigRoundtrip:
    """Triton config persists encrypted, rehydrates plaintext, reports configured."""

    def test_password_auth_full_roundtrip(self, _tenant):
        validated = TritonConnectionConfig(
            username="alice@publisher.example",
            password="hunter2",
        )
        _persist_adapter_config(_tenant, "triton", validated.model_dump())

        # On-disk: ciphertext
        with get_db_session() as session:
            row = session.scalars(select(AdapterConfig).filter_by(tenant_id=_tenant)).first()
            assert row.adapter_type == "triton"
            assert is_encrypted(row.config_json["password"]), "password must be ciphertext at rest"
            assert row.config_json["username"] == "alice@publisher.example"
            assert row.config_json["auth_type"] == "password"

        # Tenant status reports configured
        assert is_tenant_ad_server_configured(_tenant) is True
        status = get_tenant_status(_tenant)
        assert status["is_configured"] is True
        assert status["adapter_type"] == "triton"
        assert status["missing_config"] == []

        # Rehydration through the schema yields plaintext
        with get_db_session() as session:
            row = session.scalars(select(AdapterConfig).filter_by(tenant_id=_tenant)).first()
            rehydrated = TritonConnectionConfig.model_validate(row.config_json)
            assert rehydrated.password == "hunter2"

    def test_oauth_client_credentials_auth_roundtrip(self, _tenant):
        validated = TritonConnectionConfig(
            username="client_id_xyz",
            password="client_secret_abc",
            auth_type="oauth_client_credentials",
        )
        _persist_adapter_config(_tenant, "triton", validated.model_dump())

        with get_db_session() as session:
            row = session.scalars(select(AdapterConfig).filter_by(tenant_id=_tenant)).first()
            assert row.config_json["auth_type"] == "oauth_client_credentials"
            assert is_encrypted(row.config_json["password"])

        rehydrated = TritonConnectionConfig.model_validate(row.config_json)
        assert rehydrated.password == "client_secret_abc"

    def test_missing_password_reports_unconfigured(self, _tenant):
        # config_json with username but no password (simulates partial provisioning)
        _persist_adapter_config(_tenant, "triton", {"username": "alice@publisher.example"})

        assert is_tenant_ad_server_configured(_tenant) is False
        status = get_tenant_status(_tenant)
        assert status["is_configured"] is False
        assert any("password" in m.lower() for m in status["missing_config"])


@pytest.mark.integration
@pytest.mark.requires_db
class TestFreeWheelConfigRoundtrip:
    """FreeWheel config persists encrypted, rehydrates plaintext, reports configured."""

    def test_full_roundtrip_staging(self, _tenant):
        validated = FreeWheelConnectionConfig(
            client_id="fw_client_xyz",
            client_secret="fw_secret_abc",
            network_id="9876",
            environment="staging",
        )
        _persist_adapter_config(_tenant, "freewheel", validated.model_dump())

        with get_db_session() as session:
            row = session.scalars(select(AdapterConfig).filter_by(tenant_id=_tenant)).first()
            assert row.adapter_type == "freewheel"
            assert is_encrypted(row.config_json["client_secret"]), "client_secret must be ciphertext at rest"
            assert row.config_json["client_id"] == "fw_client_xyz"
            assert row.config_json["network_id"] == "9876"
            assert row.config_json["environment"] == "staging"

        assert is_tenant_ad_server_configured(_tenant) is True
        status = get_tenant_status(_tenant)
        assert status["is_configured"] is True
        assert status["adapter_type"] == "freewheel"

        rehydrated = FreeWheelConnectionConfig.model_validate(row.config_json)
        assert rehydrated.client_secret == "fw_secret_abc"
        assert rehydrated.base_url == "https://api.stg.freewheel.tv"

    def test_missing_network_id_reports_unconfigured(self, _tenant):
        # Persist a partial config to simulate misconfiguration
        _persist_adapter_config(
            _tenant,
            "freewheel",
            {
                "client_id": "fw_client_xyz",
                "client_secret": "fw_secret_abc",
                # network_id missing
            },
        )
        assert is_tenant_ad_server_configured(_tenant) is False
        status = get_tenant_status(_tenant)
        assert any("network_id" in m for m in status["missing_config"])
