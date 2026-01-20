"""OpenStreetMap Overpass API client for fetching business listings.

This module provides functionality to query OpenStreetMap's Overpass API
to retrieve business/POI listings within a specified radius of coordinates.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from .models import Business, Coordinates

logger = logging.getLogger(__name__)

# Overpass API endpoint
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"

# API request configuration
DEFAULT_TIMEOUT_SECONDS = 25
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2

# Conversion factor: 1 mile = 1609.34 meters
MILES_TO_METERS = 1609.34

# Maximum allowed radius in miles (approximately 3.1 miles = 5000 meters)
MAX_RADIUS_MILES = 3.1

# OSM tags to query for business listings
BUSINESS_TAGS = [
    "shop",        # Retail shops of all types
    "amenity",     # Restaurants, cafes, banks, etc.
    "office",      # Professional offices
    "craft",       # Craftsmen and artisans
    "healthcare",  # Medical facilities
    "tourism",     # Hotels, attractions, etc.
]


class OSMClientError(Exception):
    """Base exception for OSM client errors."""
    pass


class OSMAPIError(OSMClientError):
    """Exception raised when Overpass API returns an error."""
    pass


class OSMTimeoutError(OSMClientError):
    """Exception raised when Overpass API request times out."""
    pass


class OSMValidationError(OSMClientError):
    """Exception raised for invalid input parameters."""
    pass


def _build_overpass_query(center: Coordinates, radius_meters: float) -> str:
    """Build an Overpass QL query for businesses within a radius.

    Args:
        center: The center coordinates for the search.
        radius_meters: The search radius in meters.

    Returns:
        An Overpass QL query string.
    """
    # Build node queries for each business tag type
    node_queries = []
    for tag in BUSINESS_TAGS:
        node_queries.append(
            f'  node["{tag}"](around:{radius_meters},{center.lat},{center.lon});'
        )

    nodes_section = "\n".join(node_queries)

    query = f"""[out:json][timeout:{DEFAULT_TIMEOUT_SECONDS}];
(
{nodes_section}
);
out body;"""

    return query


def _build_address(tags: dict[str, str]) -> str:
    """Build a formatted address string from OSM address tags.

    Args:
        tags: Dictionary of OSM tags for an element.

    Returns:
        A formatted address string, or empty string if no address data.
    """
    parts = []

    # Street address (number + street)
    house_number = tags.get("addr:housenumber", "")
    street = tags.get("addr:street", "")

    if house_number and street:
        parts.append(f"{house_number} {street}")
    elif street:
        parts.append(street)
    elif house_number:
        parts.append(house_number)

    # City
    city = tags.get("addr:city", "")
    if city:
        parts.append(city)

    # State
    state = tags.get("addr:state", "")
    if state:
        parts.append(state)

    # Postcode
    postcode = tags.get("addr:postcode", "")
    if postcode:
        parts.append(postcode)

    return ", ".join(parts)


def _extract_phone(tags: dict[str, str]) -> str | None:
    """Extract phone number from OSM tags.

    Args:
        tags: Dictionary of OSM tags for an element.

    Returns:
        Phone number string or None if not available.
    """
    # Try primary phone tag first, then contact:phone
    phone = tags.get("phone") or tags.get("contact:phone")

    if phone:
        # Clean up the phone number (remove common prefixes)
        phone = phone.strip()
        return phone if phone else None

    return None


def _has_website(tags: dict[str, str]) -> bool | None:
    """Check if business has a website listed in OSM tags.

    Args:
        tags: Dictionary of OSM tags for an element.

    Returns:
        True if website exists, False if explicitly none, None if unknown.
    """
    website = tags.get("website") or tags.get("contact:website")

    if website:
        return True

    # If there's no website tag, we don't know for certain
    return None


def _determine_business_type(tags: dict[str, str]) -> str | None:
    """Determine the business type/category from OSM tags.

    Args:
        tags: Dictionary of OSM tags for an element.

    Returns:
        The business type string or None.
    """
    for tag in BUSINESS_TAGS:
        if tag in tags:
            return tags[tag]
    return None


def _parse_element(element: dict[str, Any]) -> Business | None:
    """Parse an OSM element into a Business object.

    Args:
        element: A single element from Overpass API response.

    Returns:
        A Business object or None if the element cannot be parsed.
    """
    tags = element.get("tags", {})

    # Name is required - skip elements without names
    name = tags.get("name")
    if not name:
        return None

    # Extract coordinates
    lat = element.get("lat")
    lon = element.get("lon")

    if lat is None or lon is None:
        return None

    # Build address from tags
    address = _build_address(tags)

    # Extract phone
    phone = _extract_phone(tags)

    # Check for website
    has_website = _has_website(tags)

    return Business(
        name=name.strip(),
        address=address,
        phone=phone,
        lat=float(lat),
        lon=float(lon),
        has_website=has_website,
    )


def _parse_response(data: dict[str, Any]) -> list[Business]:
    """Parse Overpass API response into Business objects.

    Args:
        data: The JSON response from Overpass API.

    Returns:
        A list of Business objects.
    """
    elements = data.get("elements", [])
    businesses = []

    for element in elements:
        business = _parse_element(element)
        if business:
            businesses.append(business)

    return businesses


def _deduplicate_businesses(businesses: list[Business]) -> list[Business]:
    """Remove duplicate businesses based on name.

    When duplicates are found, keeps the one with the most complete data
    (preferring entries with address and phone).

    Args:
        businesses: List of Business objects.

    Returns:
        Deduplicated list of Business objects.
    """
    seen: dict[str, Business] = {}

    for business in businesses:
        # Normalize name for comparison (lowercase, strip whitespace)
        normalized_name = business.name.lower().strip()

        if normalized_name not in seen:
            seen[normalized_name] = business
        else:
            # Keep the entry with more complete data
            existing = seen[normalized_name]

            # Score based on data completeness
            def completeness_score(b: Business) -> int:
                score = 0
                if b.address:
                    score += 2
                if b.phone:
                    score += 1
                if b.has_website is not None:
                    score += 1
                return score

            if completeness_score(business) > completeness_score(existing):
                seen[normalized_name] = business

    return list(seen.values())


def _validate_inputs(center: Coordinates, radius_miles: float) -> None:
    """Validate input parameters.

    Args:
        center: The center coordinates for the search.
        radius_miles: The search radius in miles.

    Raises:
        OSMValidationError: If inputs are invalid.
    """
    # Validate latitude (-90 to 90)
    if not -90 <= center.lat <= 90:
        raise OSMValidationError(
            f"Invalid latitude: {center.lat}. Must be between -90 and 90."
        )

    # Validate longitude (-180 to 180)
    if not -180 <= center.lon <= 180:
        raise OSMValidationError(
            f"Invalid longitude: {center.lon}. Must be between -180 and 180."
        )

    # Validate radius
    if radius_miles <= 0:
        raise OSMValidationError(
            f"Invalid radius: {radius_miles}. Must be a positive number."
        )

    if radius_miles > MAX_RADIUS_MILES:
        raise OSMValidationError(
            f"Radius {radius_miles} miles exceeds maximum of {MAX_RADIUS_MILES} miles."
        )


async def fetch_businesses(
    center: Coordinates,
    radius_miles: float,
) -> list[Business]:
    """Fetch business listings from OpenStreetMap within a radius.

    Queries the Overpass API to retrieve businesses (shops, restaurants,
    offices, services, amenities, craft businesses, and healthcare facilities)
    within the specified radius of the center coordinates.

    Args:
        center: The center coordinates for the search.
        radius_miles: The search radius in miles (max 3.1 miles / 5000 meters).

    Returns:
        A deduplicated list of Business objects found within the radius.

    Raises:
        OSMValidationError: If input parameters are invalid.
        OSMAPIError: If the Overpass API returns an error response.
        OSMTimeoutError: If the request times out after all retry attempts.
        OSMClientError: For other unexpected errors.

    Example:
        >>> import asyncio
        >>> from src.models import Coordinates
        >>> from src.osm_client import fetch_businesses
        >>>
        >>> async def main():
        ...     center = Coordinates(lat=40.7128, lon=-74.0060)
        ...     businesses = await fetch_businesses(center, radius_miles=0.5)
        ...     for b in businesses:
        ...         print(f"{b.name}: {b.address}")
        >>>
        >>> asyncio.run(main())
    """
    # Validate inputs
    _validate_inputs(center, radius_miles)

    # Convert miles to meters
    radius_meters = radius_miles * MILES_TO_METERS

    # Build the Overpass query
    query = _build_overpass_query(center, radius_meters)

    logger.debug(f"Querying Overpass API for businesses within {radius_miles} miles")
    logger.debug(f"Center: ({center.lat}, {center.lon})")

    # Retry logic for transient failures
    last_exception: Exception | None = None

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS + 5)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    OVERPASS_API_URL,
                    data={"data": query},
                ) as response:
                    # Handle rate limiting
                    if response.status == 429:
                        logger.warning(
                            f"Rate limited by Overpass API (attempt {attempt}/{MAX_RETRY_ATTEMPTS})"
                        )
                        if attempt < MAX_RETRY_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
                            continue
                        raise OSMAPIError("Rate limited by Overpass API after max retries")

                    # Handle server errors
                    if response.status >= 500:
                        logger.warning(
                            f"Server error {response.status} (attempt {attempt}/{MAX_RETRY_ATTEMPTS})"
                        )
                        if attempt < MAX_RETRY_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
                            continue
                        raise OSMAPIError(f"Overpass API server error: {response.status}")

                    # Handle client errors
                    if response.status >= 400:
                        error_text = await response.text()
                        raise OSMAPIError(
                            f"Overpass API error {response.status}: {error_text[:200]}"
                        )

                    # Parse successful response
                    data = await response.json()

                    # Parse and deduplicate results
                    businesses = _parse_response(data)
                    businesses = _deduplicate_businesses(businesses)

                    logger.info(f"Found {len(businesses)} unique businesses")

                    return businesses

        except asyncio.TimeoutError as e:
            logger.warning(f"Request timeout (attempt {attempt}/{MAX_RETRY_ATTEMPTS})")
            last_exception = e
            if attempt < MAX_RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

        except aiohttp.ClientError as e:
            logger.warning(f"Network error: {e} (attempt {attempt}/{MAX_RETRY_ATTEMPTS})")
            last_exception = e
            if attempt < MAX_RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue

    # All retries exhausted
    if isinstance(last_exception, asyncio.TimeoutError):
        raise OSMTimeoutError(
            f"Request timed out after {MAX_RETRY_ATTEMPTS} attempts"
        ) from last_exception

    raise OSMClientError(
        f"Failed to fetch businesses after {MAX_RETRY_ATTEMPTS} attempts"
    ) from last_exception
