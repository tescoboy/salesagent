"""Broadstreet adapter configuration schemas.

Single source of truth for Broadstreet-adapter Pydantic shapes:
- BroadstreetConnectionConfig: tenant-level API credentials
- BroadstreetProductConfig: per-product implementation_config (registered as
  product_config_class on BroadstreetAdapter; validated at the adapter
  boundary on read via parse_implementation_config)

Reconciles the historical split between the admin-slot ProductConfig (7
fields, json_schema_extra-driven) and the runtime
BroadstreetImplementationConfig (12 fields, field-validator-driven). Phase 2
of #996 — sister to MockProductConfig (#1240) and the upcoming GAM
reconciliation.

Creative template metadata (BROADSTREET_TEMPLATES + helpers) lives separately
in config_schema.py — it's a different concern from product config.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from src.adapters.base import BaseConnectionConfig, BaseProductConfig


class BroadstreetConnectionConfig(BaseConnectionConfig):
    """Connection configuration for Broadstreet API.

    Stored in AdapterConfig.config_json at the tenant level.
    """

    network_id: str = Field(
        ...,
        description="Broadstreet network ID",
        json_schema_extra={"ui_order": 1},
    )
    api_key: str = Field(
        ...,
        description="Broadstreet API access token",
        json_schema_extra={"secret": True, "ui_order": 2},
    )
    default_advertiser_id: str | None = Field(
        default=None,
        description="Default advertiser ID for principals without platform_mappings",
        json_schema_extra={"ui_order": 3},
    )


class CreativeSize(BaseModel):
    """Defines expected creative dimensions for a zone."""

    width: int = Field(..., description="Creative width in pixels")
    height: int = Field(..., description="Creative height in pixels")
    expected_count: int = Field(1, ge=1, description="Number of creatives expected for this size")


class ZoneTargeting(BaseModel):
    """Broadstreet zone targeting configuration."""

    zone_id: str = Field(..., description="Broadstreet zone ID")
    zone_name: str | None = Field(None, description="Human-readable zone name")
    sizes: list[CreativeSize] = Field(default_factory=list, description="Supported creative sizes")
    position: str | None = Field(None, description="Ad position (above_fold, below_fold)")


class BroadstreetProductConfig(BaseProductConfig):
    """Per-product Broadstreet adapter configuration.

    Stored in Product.implementation_config; controls how Broadstreet
    campaigns/placements are created when fulfilling a media buy. Registered
    as BroadstreetAdapter.product_config_class and validated at the adapter
    boundary on read.
    """

    adapter_type: Literal["broadstreet"] = Field(
        default="broadstreet",
        description="Adapter discriminator for typed implementation_config dispatch.",
    )

    # Zone/placement targeting (core Broadstreet concept)
    targeted_zone_ids: list[str] = Field(
        default_factory=list,
        description="Broadstreet zone IDs to target",
        json_schema_extra={"ui_component": "zone_selector"},
    )
    zone_targeting: list[ZoneTargeting] = Field(
        default_factory=list,
        description="Detailed zone targeting with sizes",
    )

    # Campaign naming
    campaign_name_template: str = Field(
        default="AdCP-{po_number}-{product_name}",
        description="Campaign naming template. Variables: {po_number}, {product_name}, {advertiser_name}, {timestamp}",
    )

    # Pricing
    cost_type: str = Field(
        default="CPM",
        description="Pricing model: CPM or FLAT_RATE",
    )

    # Delivery
    delivery_rate: str = Field(
        default="EVEN",
        description="Delivery pacing: EVEN, FRONTLOADED, ASAP",
    )
    frequency_cap: int | None = Field(
        default=None,
        ge=1,
        description="Max impressions per user per day",
    )

    # Creative specifications
    creative_sizes: list[CreativeSize] = Field(
        default_factory=list,
        description="Expected creative sizes for this product (zone-level overrides via zone_targeting)",
    )

    # Ad format
    ad_format: str = Field(
        default="display",
        description="Primary ad format: display, html, text",
    )
    allow_html_creatives: bool = Field(
        default=True,
        description="Allow HTML/JavaScript creatives",
    )
    allow_text_creatives: bool = Field(
        default=True,
        description="Allow text-only creatives",
    )

    # Automation
    automation_mode: str = Field(
        default="manual",
        description="Automation mode: 'automatic', 'confirmation_required', 'manual'",
    )

    @field_validator("cost_type")
    @classmethod
    def validate_cost_type(cls, v: str) -> str:
        valid = {"CPM", "FLAT_RATE"}
        v_upper = v.upper()
        if v_upper not in valid:
            raise ValueError(f"Invalid cost_type '{v}'. Must be one of: {valid}")
        return v_upper

    @field_validator("delivery_rate")
    @classmethod
    def validate_delivery_rate(cls, v: str) -> str:
        valid = {"EVEN", "FRONTLOADED", "ASAP"}
        v_upper = v.upper()
        if v_upper not in valid:
            raise ValueError(f"Invalid delivery_rate '{v}'. Must be one of: {valid}")
        return v_upper

    @field_validator("ad_format")
    @classmethod
    def validate_ad_format(cls, v: str) -> str:
        valid = {"display", "html", "text"}
        v_lower = v.lower()
        if v_lower not in valid:
            raise ValueError(f"Invalid ad_format '{v}'. Must be one of: {valid}")
        return v_lower

    @field_validator("automation_mode")
    @classmethod
    def validate_automation_mode(cls, v: str) -> str:
        valid = {"automatic", "confirmation_required", "manual"}
        v_lower = v.lower()
        if v_lower not in valid:
            raise ValueError(f"Invalid automation_mode '{v}'. Must be one of: {valid}")
        return v_lower

    def get_zone_ids(self) -> list[str]:
        """Return all zone IDs from targeted_zone_ids and zone_targeting (deduped)."""
        zone_ids = set(self.targeted_zone_ids)
        for zt in self.zone_targeting:
            zone_ids.add(zt.zone_id)
        return list(zone_ids)

    def get_creative_sizes_for_zone(self, zone_id: str) -> list[CreativeSize]:
        """Return creative sizes for a zone, falling back to global creative_sizes."""
        for zt in self.zone_targeting:
            if zt.zone_id == zone_id:
                return zt.sizes
        return self.creative_sizes


def parse_implementation_config(config: dict[str, Any] | None) -> BroadstreetProductConfig:
    """Parse Product.implementation_config into BroadstreetProductConfig.

    Returns default-constructed instance on empty/None input; validates strict otherwise.
    """
    if not config:
        return BroadstreetProductConfig()
    return BroadstreetProductConfig.model_validate(config)
