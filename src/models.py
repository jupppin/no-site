"""Shared data types for no-site application."""

from dataclasses import dataclass


@dataclass
class Coordinates:
    """Geographic coordinates."""
    lat: float
    lon: float


@dataclass
class Business:
    """Business information."""
    name: str
    address: str
    phone: str | None
    lat: float
    lon: float
    has_website: bool | None = None
