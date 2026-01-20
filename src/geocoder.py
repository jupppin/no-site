"""Geocoding component for converting locations to coordinates.

Converts zip codes and neighborhood names to geographic coordinates
using the Nominatim (OpenStreetMap) geocoding API.
"""

import re
from typing import Final

import aiohttp

from .models import Coordinates


# Nominatim API configuration
NOMINATIM_URL: Final[str] = "https://nominatim.openstreetmap.org/search"
USER_AGENT: Final[str] = "no-site/1.0 (https://github.com/no-site)"
REQUEST_TIMEOUT: Final[int] = 10  # seconds


def _is_zip_code(location: str) -> bool:
    """Check if the location string is a US zip code.

    Supports both 5-digit (12345) and ZIP+4 (12345-6789) formats.
    """
    # Match 5-digit zip or ZIP+4 format
    zip_pattern = r"^\d{5}(-\d{4})?$"
    return bool(re.match(zip_pattern, location.strip()))


def _normalize_location(location: str) -> str:
    """Normalize location input for geocoding.

    For zip codes, appends USA country qualifier.
    For neighborhood names, returns as-is for Nominatim's free-form search.
    """
    location = location.strip()

    if _is_zip_code(location):
        # Extract just the 5-digit portion for geocoding
        zip_5 = location[:5]
        return f"{zip_5}, USA"

    return location


async def geocode(location: str) -> Coordinates:
    """Convert a location string to geographic coordinates.

    Args:
        location: A zip code (e.g., "10001" or "10001-1234") or
                  neighborhood name (e.g., "SoHo, New York").

    Returns:
        Coordinates with latitude and longitude in WGS84 decimal degrees.

    Raises:
        ValueError: If the location cannot be geocoded or input is invalid.
        aiohttp.ClientError: If there's a network error connecting to the API.

    Example:
        >>> coords = await geocode("10001")
        >>> print(f"{coords.lat}, {coords.lon}")
        40.7484, -73.9967

        >>> coords = await geocode("SoHo, New York")
        >>> print(f"{coords.lat}, {coords.lon}")
        40.7233, -74.0030
    """
    if not location or not location.strip():
        raise ValueError("Location cannot be empty")

    normalized = _normalize_location(location)

    params = {
        "q": normalized,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }

    headers = {
        "User-Agent": USER_AGENT,
    }

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            NOMINATIM_URL,
            params=params,
            headers=headers,
        ) as response:
            response.raise_for_status()
            results = await response.json()

    if not results:
        raise ValueError(f"Could not geocode location: {location}")

    # Nominatim returns lat/lon as strings
    result = results[0]

    try:
        lat = float(result["lat"])
        lon = float(result["lon"])
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(f"Invalid response from geocoding service: {e}")

    return Coordinates(lat=lat, lon=lon)
