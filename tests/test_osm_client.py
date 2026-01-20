"""Unit tests for the OSM client module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models import Business, Coordinates
from src.osm_client import (
    MAX_RADIUS_MILES,
    MILES_TO_METERS,
    OSMAPIError,
    OSMClientError,
    OSMTimeoutError,
    OSMValidationError,
    _build_address,
    _build_overpass_query,
    _deduplicate_businesses,
    _extract_phone,
    _has_website,
    _parse_element,
    _parse_response,
    _validate_inputs,
    fetch_businesses,
)


class TestBuildOverpassQuery:
    """Tests for _build_overpass_query function."""

    def test_builds_query_with_coordinates(self):
        """Query includes correct coordinates."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        query = _build_overpass_query(center, 1000)

        assert "40.7128" in query
        assert "-74.006" in query

    def test_builds_query_with_radius(self):
        """Query includes correct radius."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        query = _build_overpass_query(center, 1500.5)

        assert "1500.5" in query

    def test_includes_all_business_tags(self):
        """Query includes all business tag types."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        query = _build_overpass_query(center, 1000)

        assert '"shop"' in query
        assert '"amenity"' in query
        assert '"office"' in query
        assert '"craft"' in query
        assert '"healthcare"' in query
        assert '"tourism"' in query

    def test_output_format_is_json(self):
        """Query specifies JSON output format."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        query = _build_overpass_query(center, 1000)

        assert "[out:json]" in query

    def test_includes_timeout(self):
        """Query includes timeout setting."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        query = _build_overpass_query(center, 1000)

        assert "[timeout:" in query


class TestBuildAddress:
    """Tests for _build_address function."""

    def test_full_address(self):
        """Builds complete address from all tags."""
        tags = {
            "addr:housenumber": "123",
            "addr:street": "Main Street",
            "addr:city": "New York",
            "addr:state": "NY",
            "addr:postcode": "10001",
        }
        address = _build_address(tags)

        assert address == "123 Main Street, New York, NY, 10001"

    def test_street_only(self):
        """Builds address with just street."""
        tags = {"addr:street": "Broadway"}
        address = _build_address(tags)

        assert address == "Broadway"

    def test_house_number_and_street(self):
        """Builds address with house number and street."""
        tags = {
            "addr:housenumber": "456",
            "addr:street": "Oak Avenue",
        }
        address = _build_address(tags)

        assert address == "456 Oak Avenue"

    def test_city_and_state_only(self):
        """Builds address with city and state only."""
        tags = {
            "addr:city": "Boston",
            "addr:state": "MA",
        }
        address = _build_address(tags)

        assert address == "Boston, MA"

    def test_empty_tags(self):
        """Returns empty string for empty tags."""
        address = _build_address({})

        assert address == ""

    def test_house_number_only(self):
        """Handles house number without street."""
        tags = {"addr:housenumber": "42"}
        address = _build_address(tags)

        assert address == "42"


class TestExtractPhone:
    """Tests for _extract_phone function."""

    def test_phone_tag(self):
        """Extracts phone from primary phone tag."""
        tags = {"phone": "+1-555-123-4567"}
        phone = _extract_phone(tags)

        assert phone == "+1-555-123-4567"

    def test_contact_phone_tag(self):
        """Extracts phone from contact:phone tag."""
        tags = {"contact:phone": "+1-555-987-6543"}
        phone = _extract_phone(tags)

        assert phone == "+1-555-987-6543"

    def test_prefers_phone_over_contact_phone(self):
        """Prefers phone tag over contact:phone."""
        tags = {
            "phone": "+1-555-111-1111",
            "contact:phone": "+1-555-222-2222",
        }
        phone = _extract_phone(tags)

        assert phone == "+1-555-111-1111"

    def test_no_phone(self):
        """Returns None when no phone tag exists."""
        tags = {"name": "Test Business"}
        phone = _extract_phone(tags)

        assert phone is None

    def test_empty_phone(self):
        """Returns None for empty phone string."""
        tags = {"phone": "   "}
        phone = _extract_phone(tags)

        assert phone is None


class TestHasWebsite:
    """Tests for _has_website function."""

    def test_website_tag(self):
        """Returns True when website tag exists."""
        tags = {"website": "https://example.com"}
        result = _has_website(tags)

        assert result is True

    def test_contact_website_tag(self):
        """Returns True when contact:website tag exists."""
        tags = {"contact:website": "https://example.com"}
        result = _has_website(tags)

        assert result is True

    def test_no_website(self):
        """Returns None when no website tag exists."""
        tags = {"name": "Test Business"}
        result = _has_website(tags)

        assert result is None


class TestParseElement:
    """Tests for _parse_element function."""

    def test_parses_complete_element(self):
        """Parses element with all fields."""
        element = {
            "type": "node",
            "id": 123456,
            "lat": 40.7128,
            "lon": -74.0060,
            "tags": {
                "name": "Joe's Pizza",
                "amenity": "restaurant",
                "addr:housenumber": "123",
                "addr:street": "Main St",
                "addr:city": "New York",
                "addr:state": "NY",
                "addr:postcode": "10001",
                "phone": "+1-555-123-4567",
                "website": "https://joespizza.com",
            },
        }
        business = _parse_element(element)

        assert business is not None
        assert business.name == "Joe's Pizza"
        assert business.lat == 40.7128
        assert business.lon == -74.0060
        assert business.address == "123 Main St, New York, NY, 10001"
        assert business.phone == "+1-555-123-4567"
        assert business.has_website is True

    def test_skips_element_without_name(self):
        """Returns None for element without name tag."""
        element = {
            "type": "node",
            "id": 123456,
            "lat": 40.7128,
            "lon": -74.0060,
            "tags": {"amenity": "restaurant"},
        }
        business = _parse_element(element)

        assert business is None

    def test_skips_element_without_coordinates(self):
        """Returns None for element without coordinates."""
        element = {
            "type": "node",
            "id": 123456,
            "tags": {"name": "Test Business"},
        }
        business = _parse_element(element)

        assert business is None

    def test_handles_missing_optional_fields(self):
        """Parses element with only required fields."""
        element = {
            "type": "node",
            "id": 123456,
            "lat": 40.7128,
            "lon": -74.0060,
            "tags": {"name": "Simple Shop", "shop": "convenience"},
        }
        business = _parse_element(element)

        assert business is not None
        assert business.name == "Simple Shop"
        assert business.address == ""
        assert business.phone is None
        assert business.has_website is None

    def test_strips_whitespace_from_name(self):
        """Strips whitespace from business name."""
        element = {
            "type": "node",
            "id": 123456,
            "lat": 40.7128,
            "lon": -74.0060,
            "tags": {"name": "  Padded Name  ", "shop": "books"},
        }
        business = _parse_element(element)

        assert business.name == "Padded Name"


class TestParseResponse:
    """Tests for _parse_response function."""

    def test_parses_multiple_elements(self):
        """Parses response with multiple elements."""
        data = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 40.71,
                    "lon": -74.00,
                    "tags": {"name": "Business 1", "shop": "books"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 40.72,
                    "lon": -74.01,
                    "tags": {"name": "Business 2", "amenity": "cafe"},
                },
            ]
        }
        businesses = _parse_response(data)

        assert len(businesses) == 2
        assert businesses[0].name == "Business 1"
        assert businesses[1].name == "Business 2"

    def test_handles_empty_response(self):
        """Returns empty list for response with no elements."""
        data = {"elements": []}
        businesses = _parse_response(data)

        assert businesses == []

    def test_filters_invalid_elements(self):
        """Filters out elements that cannot be parsed."""
        data = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 40.71,
                    "lon": -74.00,
                    "tags": {"name": "Valid Business", "shop": "books"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 40.72,
                    "lon": -74.01,
                    "tags": {"shop": "unknown"},  # No name
                },
            ]
        }
        businesses = _parse_response(data)

        assert len(businesses) == 1
        assert businesses[0].name == "Valid Business"


class TestDeduplicateBusinesses:
    """Tests for _deduplicate_businesses function."""

    def test_removes_exact_duplicates(self):
        """Removes businesses with exact same name."""
        businesses = [
            Business("Coffee Shop", "123 Main St", "+1-555-1234", 40.71, -74.00),
            Business("Coffee Shop", "456 Oak Ave", None, 40.72, -74.01),
        ]
        result = _deduplicate_businesses(businesses)

        assert len(result) == 1

    def test_case_insensitive_deduplication(self):
        """Deduplication is case-insensitive."""
        businesses = [
            Business("COFFEE SHOP", "", None, 40.71, -74.00),
            Business("Coffee Shop", "123 Main St", "+1-555-1234", 40.72, -74.01),
        ]
        result = _deduplicate_businesses(businesses)

        assert len(result) == 1

    def test_keeps_most_complete_entry(self):
        """Keeps the entry with most complete data."""
        businesses = [
            Business("Pizza Place", "", None, 40.71, -74.00),
            Business("Pizza Place", "123 Main St", "+1-555-1234", 40.72, -74.01),
        ]
        result = _deduplicate_businesses(businesses)

        assert len(result) == 1
        assert result[0].address == "123 Main St"
        assert result[0].phone == "+1-555-1234"

    def test_preserves_unique_businesses(self):
        """Preserves all unique businesses."""
        businesses = [
            Business("Business A", "123 Main St", None, 40.71, -74.00),
            Business("Business B", "456 Oak Ave", None, 40.72, -74.01),
            Business("Business C", "789 Elm St", None, 40.73, -74.02),
        ]
        result = _deduplicate_businesses(businesses)

        assert len(result) == 3

    def test_handles_empty_list(self):
        """Handles empty input list."""
        result = _deduplicate_businesses([])

        assert result == []


class TestValidateInputs:
    """Tests for _validate_inputs function."""

    def test_valid_inputs(self):
        """Accepts valid inputs without raising."""
        center = Coordinates(lat=40.7128, lon=-74.0060)
        _validate_inputs(center, radius_miles=1.0)  # Should not raise

    def test_invalid_latitude_too_high(self):
        """Raises for latitude above 90."""
        center = Coordinates(lat=91.0, lon=-74.0060)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=1.0)

        assert "latitude" in str(exc_info.value).lower()

    def test_invalid_latitude_too_low(self):
        """Raises for latitude below -90."""
        center = Coordinates(lat=-91.0, lon=-74.0060)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=1.0)

        assert "latitude" in str(exc_info.value).lower()

    def test_invalid_longitude_too_high(self):
        """Raises for longitude above 180."""
        center = Coordinates(lat=40.7128, lon=181.0)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=1.0)

        assert "longitude" in str(exc_info.value).lower()

    def test_invalid_longitude_too_low(self):
        """Raises for longitude below -180."""
        center = Coordinates(lat=40.7128, lon=-181.0)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=1.0)

        assert "longitude" in str(exc_info.value).lower()

    def test_negative_radius(self):
        """Raises for negative radius."""
        center = Coordinates(lat=40.7128, lon=-74.0060)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=-1.0)

        assert "radius" in str(exc_info.value).lower()

    def test_zero_radius(self):
        """Raises for zero radius."""
        center = Coordinates(lat=40.7128, lon=-74.0060)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=0.0)

        assert "radius" in str(exc_info.value).lower()

    def test_radius_exceeds_maximum(self):
        """Raises for radius exceeding maximum."""
        center = Coordinates(lat=40.7128, lon=-74.0060)

        with pytest.raises(OSMValidationError) as exc_info:
            _validate_inputs(center, radius_miles=MAX_RADIUS_MILES + 1)

        assert "radius" in str(exc_info.value).lower()
        assert str(MAX_RADIUS_MILES) in str(exc_info.value)


class TestFetchBusinesses:
    """Tests for fetch_businesses function."""

    @pytest.fixture
    def mock_response_data(self):
        """Sample Overpass API response data."""
        return {
            "elements": [
                {
                    "type": "node",
                    "id": 12345,
                    "lat": 40.7128,
                    "lon": -74.0060,
                    "tags": {
                        "name": "Test Coffee Shop",
                        "amenity": "cafe",
                        "addr:street": "Main Street",
                        "addr:housenumber": "123",
                        "addr:city": "New York",
                        "phone": "+1-555-123-4567",
                    },
                },
                {
                    "type": "node",
                    "id": 12346,
                    "lat": 40.7130,
                    "lon": -74.0062,
                    "tags": {
                        "name": "Test Book Store",
                        "shop": "books",
                        "addr:street": "Broadway",
                        "addr:housenumber": "456",
                    },
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_successful_fetch(self, mock_response_data):
        """Successfully fetches and parses businesses."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=mock_response_data)

        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_post = MagicMock()
            mock_post.__aenter__ = AsyncMock(return_value=mock_response)
            mock_post.__aexit__ = AsyncMock(return_value=None)
            mock_session.post = MagicMock(return_value=mock_post)

            mock_session_class.return_value = mock_session

            center = Coordinates(lat=40.7128, lon=-74.0060)
            businesses = await fetch_businesses(center, radius_miles=0.5)

            assert len(businesses) == 2
            assert businesses[0].name == "Test Coffee Shop"
            assert businesses[1].name == "Test Book Store"

    @pytest.mark.asyncio
    async def test_validation_error(self):
        """Raises validation error for invalid inputs."""
        center = Coordinates(lat=100.0, lon=-74.0060)  # Invalid latitude

        with pytest.raises(OSMValidationError):
            await fetch_businesses(center, radius_miles=0.5)

    @pytest.mark.asyncio
    async def test_api_error_response(self):
        """Raises OSMAPIError for 4xx responses."""
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.text = AsyncMock(return_value="Bad Request")

        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_post = MagicMock()
            mock_post.__aenter__ = AsyncMock(return_value=mock_response)
            mock_post.__aexit__ = AsyncMock(return_value=None)
            mock_session.post = MagicMock(return_value=mock_post)

            mock_session_class.return_value = mock_session

            center = Coordinates(lat=40.7128, lon=-74.0060)

            with pytest.raises(OSMAPIError) as exc_info:
                await fetch_businesses(center, radius_miles=0.5)

            assert "400" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Returns empty list when no businesses found."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"elements": []})

        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_post = MagicMock()
            mock_post.__aenter__ = AsyncMock(return_value=mock_response)
            mock_post.__aexit__ = AsyncMock(return_value=None)
            mock_session.post = MagicMock(return_value=mock_post)

            mock_session_class.return_value = mock_session

            center = Coordinates(lat=40.7128, lon=-74.0060)
            businesses = await fetch_businesses(center, radius_miles=0.5)

            assert businesses == []

    @pytest.mark.asyncio
    async def test_deduplicates_results(self):
        """Deduplicates businesses with same name."""
        duplicate_data = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 40.71,
                    "lon": -74.00,
                    "tags": {"name": "Duplicate Shop", "shop": "convenience"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 40.72,
                    "lon": -74.01,
                    "tags": {
                        "name": "Duplicate Shop",
                        "shop": "convenience",
                        "addr:street": "Main St",
                        "phone": "+1-555-1234",
                    },
                },
            ]
        }

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=duplicate_data)

        with patch("aiohttp.ClientSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            mock_post = MagicMock()
            mock_post.__aenter__ = AsyncMock(return_value=mock_response)
            mock_post.__aexit__ = AsyncMock(return_value=None)
            mock_session.post = MagicMock(return_value=mock_post)

            mock_session_class.return_value = mock_session

            center = Coordinates(lat=40.7128, lon=-74.0060)
            businesses = await fetch_businesses(center, radius_miles=0.5)

            # Should be deduplicated to 1
            assert len(businesses) == 1
            # Should keep the more complete entry
            assert businesses[0].phone == "+1-555-1234"


class TestMilesToMetersConversion:
    """Tests for miles to meters conversion."""

    def test_conversion_factor(self):
        """Verifies correct conversion factor."""
        # 1 mile should be approximately 1609.34 meters
        assert abs(MILES_TO_METERS - 1609.34) < 0.01

    def test_max_radius_in_meters(self):
        """Max radius in miles corresponds to approximately 5000 meters."""
        max_meters = MAX_RADIUS_MILES * MILES_TO_METERS
        # Should be close to 5000 meters
        assert 4900 < max_meters < 5100
