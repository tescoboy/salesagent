"""Unit tests for Broadstreet schemas (connection + product config)."""

import pytest
from pydantic import ValidationError

from src.adapters.broadstreet.schemas import (
    BroadstreetProductConfig,
    CreativeSize,
    ZoneTargeting,
    parse_implementation_config,
)


class TestCreativeSize:
    """Tests for CreativeSize model."""

    def test_valid_creative_size(self):
        size = CreativeSize(width=300, height=250)

        assert size.width == 300
        assert size.height == 250
        assert size.expected_count == 1

    def test_creative_size_with_expected_count(self):
        size = CreativeSize(width=728, height=90, expected_count=3)

        assert size.expected_count == 3

    def test_creative_size_requires_positive_count(self):
        with pytest.raises(ValueError):
            CreativeSize(width=300, height=250, expected_count=0)


class TestZoneTargeting:
    """Tests for ZoneTargeting model."""

    def test_minimal_zone_targeting(self):
        zone = ZoneTargeting(zone_id="zone_123")

        assert zone.zone_id == "zone_123"
        assert zone.zone_name is None
        assert zone.sizes == []
        assert zone.position is None

    def test_full_zone_targeting(self):
        zone = ZoneTargeting(
            zone_id="zone_123",
            zone_name="Top Banner",
            sizes=[
                CreativeSize(width=728, height=90),
                CreativeSize(width=300, height=250),
            ],
            position="above_fold",
        )

        assert zone.zone_id == "zone_123"
        assert zone.zone_name == "Top Banner"
        assert len(zone.sizes) == 2
        assert zone.position == "above_fold"


class TestBroadstreetProductConfig:
    """Tests for BroadstreetProductConfig.

    Reconciled in #1239: single 12-field schema (was split between admin
    BroadstreetProductConfig (7 fields) and runtime
    BroadstreetImplementationConfig (12 fields)).
    """

    def test_default_config(self):
        config = BroadstreetProductConfig()

        assert config.adapter_type == "broadstreet"
        assert config.targeted_zone_ids == []
        assert config.zone_targeting == []
        assert config.campaign_name_template == "AdCP-{po_number}-{product_name}"
        assert config.cost_type == "CPM"
        assert config.delivery_rate == "EVEN"
        assert config.frequency_cap is None
        assert config.creative_sizes == []
        assert config.ad_format == "display"
        assert config.allow_html_creatives is True
        assert config.allow_text_creatives is True
        assert config.automation_mode == "manual"

    def test_adapter_type_locked(self):
        """adapter_type is a Literal discriminator — rejects other values."""
        with pytest.raises(ValidationError):
            BroadstreetProductConfig(adapter_type="google_ad_manager")

    def test_round_trip_preserves_discriminator(self):
        """model_dump → model_validate preserves adapter_type."""
        config = BroadstreetProductConfig(targeted_zone_ids=["z1"])
        round_tripped = BroadstreetProductConfig.model_validate(config.model_dump())

        assert round_tripped.adapter_type == "broadstreet"
        assert round_tripped == config

    def test_extra_field_rejected(self):
        """Inherits extra='forbid' from BaseProductConfig — typo rejected."""
        with pytest.raises(ValidationError):
            BroadstreetProductConfig(targetd_zone_ids=["z1"])  # missing 'e'

    def test_config_with_zones(self):
        config = BroadstreetProductConfig(targeted_zone_ids=["zone_1", "zone_2"])

        assert config.targeted_zone_ids == ["zone_1", "zone_2"]
        assert set(config.get_zone_ids()) == {"zone_1", "zone_2"}

    def test_config_with_zone_targeting(self):
        config = BroadstreetProductConfig(
            zone_targeting=[
                ZoneTargeting(
                    zone_id="zone_3",
                    zone_name="Sidebar",
                    sizes=[CreativeSize(width=300, height=250)],
                ),
            ],
        )

        assert "zone_3" in config.get_zone_ids()

    def test_get_zone_ids_combines_both_sources(self):
        config = BroadstreetProductConfig(
            targeted_zone_ids=["zone_1", "zone_2"],
            zone_targeting=[
                ZoneTargeting(zone_id="zone_3"),
                ZoneTargeting(zone_id="zone_1"),  # Duplicate
            ],
        )

        zone_ids = config.get_zone_ids()
        assert len(zone_ids) == 3
        assert set(zone_ids) == {"zone_1", "zone_2", "zone_3"}

    def test_cost_type_validation_cpm(self):
        config = BroadstreetProductConfig(cost_type="cpm")
        assert config.cost_type == "CPM"

    def test_cost_type_validation_flat_rate(self):
        config = BroadstreetProductConfig(cost_type="flat_rate")
        assert config.cost_type == "FLAT_RATE"

    def test_cost_type_validation_invalid(self):
        with pytest.raises(ValueError, match="Invalid cost_type"):
            BroadstreetProductConfig(cost_type="invalid")

    def test_delivery_rate_validation(self):
        config = BroadstreetProductConfig(delivery_rate="frontloaded")
        assert config.delivery_rate == "FRONTLOADED"

    def test_delivery_rate_validation_invalid(self):
        with pytest.raises(ValueError, match="Invalid delivery_rate"):
            BroadstreetProductConfig(delivery_rate="invalid")

    def test_ad_format_validation(self):
        config = BroadstreetProductConfig(ad_format="HTML")
        assert config.ad_format == "html"

    def test_ad_format_validation_invalid(self):
        with pytest.raises(ValueError, match="Invalid ad_format"):
            BroadstreetProductConfig(ad_format="video")

    def test_automation_mode_validation(self):
        config = BroadstreetProductConfig(automation_mode="AUTOMATIC")
        assert config.automation_mode == "automatic"

    def test_automation_mode_validation_invalid(self):
        with pytest.raises(ValueError, match="Invalid automation_mode"):
            BroadstreetProductConfig(automation_mode="invalid")

    def test_get_creative_sizes_for_zone(self):
        config = BroadstreetProductConfig(
            creative_sizes=[CreativeSize(width=728, height=90)],
            zone_targeting=[
                ZoneTargeting(
                    zone_id="zone_special",
                    sizes=[CreativeSize(width=300, height=250)],
                ),
            ],
        )

        # Zone with specific sizes
        sizes = config.get_creative_sizes_for_zone("zone_special")
        assert len(sizes) == 1
        assert sizes[0].width == 300

        # Zone without specific sizes falls back to global
        sizes = config.get_creative_sizes_for_zone("zone_other")
        assert len(sizes) == 1
        assert sizes[0].width == 728


class TestParseImplementationConfig:
    """Tests for parse_implementation_config helper."""

    def test_parse_none_returns_default(self):
        config = parse_implementation_config(None)

        assert isinstance(config, BroadstreetProductConfig)
        assert config.adapter_type == "broadstreet"
        assert config.cost_type == "CPM"

    def test_parse_empty_dict_returns_default(self):
        config = parse_implementation_config({})

        assert isinstance(config, BroadstreetProductConfig)
        assert config.cost_type == "CPM"

    def test_parse_valid_dict(self):
        config = parse_implementation_config(
            {
                "targeted_zone_ids": ["zone_1"],
                "cost_type": "flat_rate",
                "ad_format": "html",
            }
        )

        assert config.targeted_zone_ids == ["zone_1"]
        assert config.cost_type == "FLAT_RATE"  # validator uppercases
        assert config.ad_format == "html"

    def test_parse_nested_zone_targeting(self):
        config = parse_implementation_config(
            {
                "zone_targeting": [
                    {
                        "zone_id": "zone_1",
                        "zone_name": "Top",
                        "sizes": [{"width": 728, "height": 90}],
                    },
                ],
            }
        )

        assert isinstance(config, BroadstreetProductConfig)
        assert len(config.zone_targeting) == 1
        assert config.zone_targeting[0].zone_id == "zone_1"
        assert config.zone_targeting[0].sizes[0].width == 728

    def test_parse_invalid_dict_raises(self):
        with pytest.raises(ValidationError):
            parse_implementation_config({"cost_type": "INVALID"})
