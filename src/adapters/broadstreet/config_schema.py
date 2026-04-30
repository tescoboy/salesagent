"""Broadstreet creative template metadata.

BROADSTREET_TEMPLATES enumerates the ad templates Broadstreet supports, with
required and optional asset slots per template. Used by the advertisement
manager to validate creative payloads before submission.

Product/connection schemas live in schemas.py — this module is template
metadata only.
"""

from typing import Any

# Each template has required assets that must be provided
BROADSTREET_TEMPLATES = {
    # Basic types (no template - direct content)
    "static": {
        "name": "Static Image",
        "description": "Basic banner/display image ad",
        "required_assets": ["image"],
        "optional_assets": ["click_url", "alt_text"],
    },
    "html": {
        "name": "HTML/JavaScript",
        "description": "Custom HTML or JavaScript ad",
        "required_assets": ["html"],
        "optional_assets": ["click_url"],
    },
    "text": {
        "name": "Text Ad",
        "description": "Text-only ad",
        "required_assets": ["headline", "body"],
        "optional_assets": ["click_url"],
    },
    # Special templates (use setAdvertisementSource API)
    "cube_3d": {
        "name": "Amazing 3D Cube Gallery",
        "description": "6-sided rotating cube with images",
        "required_assets": [
            "front_image",
            "back_image",
            "left_image",
            "right_image",
            "top_image",
            "bottom_image",
        ],
        "optional_assets": [
            "front_caption",
            "back_caption",
            "left_caption",
            "right_caption",
            "top_caption",
            "bottom_caption",
            "click_url",
            "logo",
            "auto_rotate_ms",
        ],
        "api_source_type": "cube",
    },
    "youtube_video": {
        "name": "YouTube Video with Text",
        "description": "YouTube video embed with optional text overlay",
        "required_assets": ["youtube_url"],
        "optional_assets": ["headline", "body", "click_url", "autoplay"],
        "api_source_type": "youtube",
    },
    "push_pin": {
        "name": "Push Pin Photo",
        "description": "Pop-up bulletin board style ad",
        "required_assets": ["image"],
        "optional_assets": ["click_url", "caption", "pin_color"],
        "api_source_type": "pushpin",
    },
    "gallery": {
        "name": "Image Gallery",
        "description": "Multiple images in a slideshow",
        "required_assets": ["images"],  # List of image URLs
        "optional_assets": ["captions", "click_urls", "auto_rotate_ms"],
        "api_source_type": "gallery",
    },
    "native": {
        "name": "Native Ad",
        "description": "Native content ad matching site style",
        "required_assets": ["headline", "image"],
        "optional_assets": ["body", "sponsor", "click_url", "cta_text"],
        "api_source_type": "native",
    },
}


def get_template_info(template_type: str) -> dict[str, Any] | None:
    """Get template information including required assets.

    Args:
        template_type: Template type name

    Returns:
        Template info dict or None if not found
    """
    return BROADSTREET_TEMPLATES.get(template_type)


def validate_template_assets(template_type: str, assets: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate that assets meet template requirements.

    Args:
        template_type: Template type name
        assets: Asset dictionary

    Returns:
        Tuple of (is_valid, error_message)
    """
    template = BROADSTREET_TEMPLATES.get(template_type)
    if not template:
        return False, f"Unknown template type: {template_type}"

    required = template.get("required_assets", [])
    missing = [asset for asset in required if asset not in assets or not assets[asset]]

    if missing:
        return False, f"Missing required assets for {template_type}: {', '.join(missing)}"

    return True, None
