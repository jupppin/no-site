"""CSV formatter for business data output."""

import csv
from pathlib import Path

from .models import Business


def _format_phone(phone: str | None) -> str:
    """Format phone number to (XXX) XXX-XXXX format.

    Args:
        phone: Raw phone number string or None.

    Returns:
        Formatted phone number or empty string if None/invalid.
    """
    if phone is None:
        return ""

    # Extract digits only
    digits = "".join(c for c in phone if c.isdigit())

    # Handle 10-digit US phone numbers
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    # Handle 11-digit numbers starting with 1 (US country code)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    # Return original if format not recognized
    return phone.strip() if phone else ""


def _get_website_status(has_website: bool | None) -> str:
    """Convert has_website boolean to status string.

    Args:
        has_website: Boolean indicating website presence, or None if unknown.

    Returns:
        Status string: "active", "not_found", or "unknown".
    """
    if has_website is None:
        return "unknown"
    return "active" if has_website else "not_found"


def write_csv(businesses: list[Business], output_path: str) -> str:
    """Write business data to CSV file.

    Generates a CSV file with columns: business_name, address, phone, website_status.

    Args:
        businesses: List of Business objects to write.
        output_path: Path where CSV file will be created.

    Returns:
        The output path on success.

    Raises:
        IOError: If file cannot be written.
    """
    fieldnames = ["business_name", "address", "phone", "website_status"]

    # Ensure parent directory exists
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()

        for business in businesses:
            row = {
                "business_name": business.name.strip() if business.name else "",
                "address": business.address.strip() if business.address else "",
                "phone": _format_phone(business.phone),
                "website_status": _get_website_status(business.has_website),
            }
            writer.writerow(row)

    return output_path
