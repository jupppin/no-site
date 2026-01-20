"""CLI entry point for no-site: Find local businesses without web presence.

This module provides the command-line interface for searching and discovering
local businesses that lack dedicated websites, making them potential leads
for web development services.

Usage:
    python -m src.main --location "10001" --radius 5 --output businesses.csv
    python -m src.main --location "SoHo, New York" --radius 2 --output soho.csv
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import NoReturn

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .async_handler import run_with_rate_limit
from .csv_formatter import write_csv
from .geocoder import geocode
from .models import Business, Coordinates
from .osm_client import fetch_businesses, MAX_RADIUS_MILES, OSMClientError
from .website_checker import check_website


# Console instances for output management
console = Console()
error_console = Console(stderr=True)

# Exit codes
EXIT_SUCCESS = 0
EXIT_INVALID_INPUT = 1
EXIT_GEOCODE_ERROR = 2
EXIT_OSM_ERROR = 3
EXIT_WEBSITE_CHECK_ERROR = 4
EXIT_OUTPUT_ERROR = 5
EXIT_INTERRUPTED = 130


def validate_radius(ctx: click.Context, param: click.Parameter, value: float) -> float:
    """Validate that radius is within acceptable bounds.

    Args:
        ctx: Click context.
        param: Click parameter.
        value: The radius value to validate.

    Returns:
        The validated radius value.

    Raises:
        click.BadParameter: If radius is out of bounds.
    """
    if value <= 0:
        raise click.BadParameter("Radius must be a positive number.")
    if value > MAX_RADIUS_MILES:
        raise click.BadParameter(
            f"Radius cannot exceed {MAX_RADIUS_MILES} miles due to API limitations."
        )
    return value


def validate_output_path(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """Validate and normalize the output file path.

    Args:
        ctx: Click context.
        param: Click parameter.
        value: The output path to validate.

    Returns:
        The validated output path.

    Raises:
        click.BadParameter: If path is invalid or not writable.
    """
    path = Path(value)

    # Ensure it has .csv extension
    if path.suffix.lower() != ".csv":
        value = f"{value}.csv"
        path = Path(value)

    # Check if parent directory exists or can be created
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise click.BadParameter(f"Cannot create directory: {path.parent}")
    except OSError as e:
        raise click.BadParameter(f"Invalid output path: {e}")

    return str(path)


@click.command()
@click.option(
    "--location", "-l",
    required=True,
    help="Location to search (ZIP code or neighborhood name). "
         "Examples: '10001', 'SoHo, New York', 'Downtown Austin, TX'",
)
@click.option(
    "--radius", "-r",
    type=float,
    default=1.0,
    callback=validate_radius,
    help=f"Search radius in miles (default: 1.0, max: {MAX_RADIUS_MILES}).",
)
@click.option(
    "--output", "-o",
    type=str,
    default="businesses.csv",
    callback=validate_output_path,
    help="Output CSV file path (default: businesses.csv).",
)
@click.option(
    "--concurrency", "-c",
    type=int,
    default=5,
    help="Maximum concurrent website checks (default: 5).",
)
@click.option(
    "--delay",
    type=float,
    default=1.0,
    help="Delay between website checks in seconds (default: 1.0).",
)
@click.option(
    "--limit", "-n",
    type=int,
    default=None,
    help="Maximum number of businesses to check (default: no limit).",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Enable verbose output with detailed progress.",
)
@click.option(
    "--quiet", "-q",
    is_flag=True,
    default=False,
    help="Suppress all output except errors and final result.",
)
@click.version_option(version="0.1.0", prog_name="no-site")
@click.help_option("--help", "-h")
def main(
    location: str,
    radius: float,
    output: str,
    concurrency: int,
    delay: float,
    limit: int | None,
    verbose: bool,
    quiet: bool,
) -> None:
    """Find local businesses without web presence.

    Searches for businesses in the specified location and identifies those
    that do not have a dedicated website, outputting results to a CSV file.

    \b
    Examples:
        no-site --location "10001" --radius 5 --output businesses.csv
        no-site -l "SoHo, New York" -r 2 -o soho.csv
        no-site -l "Downtown Austin, TX" -r 1.5 --verbose
    """
    if quiet and verbose:
        error_console.print("[red]Error:[/red] Cannot use --quiet and --verbose together.")
        sys.exit(EXIT_INVALID_INPUT)

    try:
        asyncio.run(_run_pipeline(
            location=location,
            radius=radius,
            output=output,
            concurrency=concurrency,
            delay=delay,
            limit=limit,
            verbose=verbose,
            quiet=quiet,
        ))
    except KeyboardInterrupt:
        if not quiet:
            error_console.print("\n[yellow]Operation cancelled by user.[/yellow]")
        sys.exit(EXIT_INTERRUPTED)


async def _run_pipeline(
    location: str,
    radius: float,
    output: str,
    concurrency: int,
    delay: float,
    limit: int | None,
    verbose: bool,
    quiet: bool,
) -> None:
    """Execute the full business discovery pipeline.

    Args:
        location: Search location string.
        radius: Search radius in miles.
        output: Output CSV file path.
        concurrency: Max concurrent website checks.
        delay: Delay between checks in seconds.
        limit: Maximum businesses to check (None for no limit).
        verbose: Enable verbose output.
        quiet: Suppress non-essential output.
    """
    if not quiet:
        _print_header(location, radius, output, limit)

    # Phase 1: Geocode the location
    coordinates = await _phase_geocode(location, quiet)
    if coordinates is None:
        sys.exit(EXIT_GEOCODE_ERROR)

    if verbose and not quiet:
        console.print(f"  Coordinates: {coordinates.lat:.4f}, {coordinates.lon:.4f}")

    # Phase 2: Fetch businesses from OSM
    businesses = await _phase_fetch_businesses(coordinates, radius, quiet)
    if businesses is None:
        sys.exit(EXIT_OSM_ERROR)

    if not businesses:
        if not quiet:
            console.print("\n[yellow]No businesses found in this area.[/yellow]")
            console.print("Try increasing the search radius or using a different location.")
        sys.exit(EXIT_SUCCESS)

    # Apply limit if specified
    total_found = len(businesses)
    if limit is not None and len(businesses) > limit:
        businesses = businesses[:limit]
        if not quiet:
            console.print(f"[yellow]Limiting to {limit} of {total_found} businesses found[/yellow]")

    if verbose and not quiet:
        console.print(f"  Checking {len(businesses)} businesses")

    # Phase 3: Check websites for each business
    checked_businesses = await _phase_check_websites(
        businesses, concurrency, delay, quiet, verbose
    )
    if checked_businesses is None:
        sys.exit(EXIT_WEBSITE_CHECK_ERROR)

    # Phase 4: Filter to businesses without websites
    no_website = [b for b in checked_businesses if b.has_website is False]

    if not quiet:
        _print_results_summary(len(checked_businesses), len(no_website), verbose)

    if not no_website:
        if not quiet:
            console.print("\n[yellow]All businesses in this area appear to have websites.[/yellow]")
        sys.exit(EXIT_SUCCESS)

    # Phase 5: Write CSV output
    success = await _phase_write_output(no_website, output, quiet)
    if not success:
        sys.exit(EXIT_OUTPUT_ERROR)

    if not quiet:
        _print_completion(output, len(no_website))

    sys.exit(EXIT_SUCCESS)


def _print_header(location: str, radius: float, output: str, limit: int | None = None) -> None:
    """Print the startup header with search parameters."""
    console.print()
    console.print(Panel.fit(
        "[bold blue]no-site[/bold blue] - Find Local Businesses Without Websites",
        border_style="blue",
    ))
    console.print()
    console.print(f"[dim]Location:[/dim]  {location}")
    console.print(f"[dim]Radius:[/dim]    {radius} mile{'s' if radius != 1 else ''}")
    if limit:
        console.print(f"[dim]Limit:[/dim]     {limit} businesses")
    console.print(f"[dim]Output:[/dim]    {output}")
    console.print()


async def _phase_geocode(location: str, quiet: bool) -> Coordinates | None:
    """Phase 1: Geocode the location string to coordinates.

    Args:
        location: The location string to geocode.
        quiet: Suppress progress output.

    Returns:
        Coordinates on success, None on failure.
    """
    if quiet:
        try:
            return await geocode(location)
        except ValueError as e:
            error_console.print(f"[red]Error:[/red] {e}")
            return None
        except Exception as e:
            error_console.print(f"[red]Error:[/red] Failed to geocode location: {e}")
            return None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Geocoding location...", total=None)

        try:
            coords = await geocode(location)
            progress.update(task, description="[green]Location geocoded successfully[/green]")
            return coords
        except ValueError as e:
            progress.stop()
            error_console.print(f"\n[red]Error:[/red] {e}")
            error_console.print("[dim]Tip: Try a more specific location or verify the ZIP code.[/dim]")
            return None
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[red]Error:[/red] Failed to geocode location: {e}")
            error_console.print("[dim]Tip: Check your internet connection and try again.[/dim]")
            return None


async def _phase_fetch_businesses(
    coordinates: Coordinates,
    radius: float,
    quiet: bool,
) -> list[Business] | None:
    """Phase 2: Fetch businesses from OpenStreetMap.

    Args:
        coordinates: Search center coordinates.
        radius: Search radius in miles.
        quiet: Suppress progress output.

    Returns:
        List of businesses on success, None on failure.
    """
    if quiet:
        try:
            return await fetch_businesses(coordinates, radius)
        except OSMClientError as e:
            error_console.print(f"[red]Error:[/red] {e}")
            return None
        except Exception as e:
            error_console.print(f"[red]Error:[/red] Failed to fetch businesses: {e}")
            return None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Fetching businesses from OpenStreetMap...", total=None)

        try:
            businesses = await fetch_businesses(coordinates, radius)
            count = len(businesses)
            progress.update(
                task,
                description=f"[green]Found {count} business{'es' if count != 1 else ''}[/green]"
            )
            return businesses
        except OSMClientError as e:
            progress.stop()
            error_console.print(f"\n[red]Error:[/red] {e}")
            return None
        except Exception as e:
            progress.stop()
            error_console.print(f"\n[red]Error:[/red] Failed to fetch businesses: {e}")
            error_console.print("[dim]Tip: The OpenStreetMap API may be temporarily unavailable. Try again later.[/dim]")
            return None


async def _phase_check_websites(
    businesses: list[Business],
    concurrency: int,
    delay: float,
    quiet: bool,
    verbose: bool,
) -> list[Business] | None:
    """Phase 3: Check each business for website presence.

    Args:
        businesses: List of businesses to check.
        concurrency: Maximum concurrent checks.
        delay: Delay between checks in seconds.
        quiet: Suppress progress output.
        verbose: Show detailed progress.

    Returns:
        List of businesses with website status updated, None on failure.
    """
    async def create_check_task(business: Business) -> Business:
        """Create a website check task that updates the business object."""
        has_website = await check_website(business)
        business.has_website = has_website
        return business

    if quiet:
        try:
            tasks = [create_check_task(b) for b in businesses]
            results = await run_with_rate_limit(tasks, concurrency, delay)
            # Filter out any exceptions
            return [r for r in results if isinstance(r, Business)]
        except Exception as e:
            error_console.print(f"[red]Error:[/red] Website checking failed: {e}")
            return None

    # Create progress display for website checking
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        total = len(businesses)
        task = progress.add_task(
            f"Checking websites (0/{total})...",
            total=total,
        )

        completed = 0
        with_website = 0
        without_website = 0

        async def check_and_track(business: Business) -> Business:
            """Check website and update progress."""
            nonlocal completed, with_website, without_website

            has_website = await check_website(business)
            business.has_website = has_website

            completed += 1
            if has_website:
                with_website += 1
            else:
                without_website += 1

            description = f"Checking websites ({completed}/{total})"
            if verbose:
                description += f" | Has site: {with_website} | No site: {without_website}"
            progress.update(task, advance=1, description=description)

            return business

        try:
            tasks = [check_and_track(b) for b in businesses]
            results = await run_with_rate_limit(tasks, concurrency, delay)

            # Filter out any exceptions that might have been returned
            valid_results = [r for r in results if isinstance(r, Business)]

            progress.update(
                task,
                description=f"[green]Website check complete ({completed}/{total})[/green]",
            )

            return valid_results

        except Exception as e:
            progress.stop()
            error_console.print(f"\n[red]Error:[/red] Website checking failed: {e}")
            return None


async def _phase_write_output(
    businesses: list[Business],
    output_path: str,
    quiet: bool,
) -> bool:
    """Phase 5: Write results to CSV file.

    Args:
        businesses: Businesses to write.
        output_path: Output file path.
        quiet: Suppress progress output.

    Returns:
        True on success, False on failure.
    """
    if not quiet:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            console=console,
        ) as progress:
            progress.add_task("Writing CSV output...", total=None)

            try:
                write_csv(businesses, output_path)
                return True
            except IOError as e:
                progress.stop()
                error_console.print(f"\n[red]Error:[/red] Failed to write output file: {e}")
                return False
            except Exception as e:
                progress.stop()
                error_console.print(f"\n[red]Error:[/red] Unexpected error writing output: {e}")
                return False
    else:
        try:
            write_csv(businesses, output_path)
            return True
        except Exception as e:
            error_console.print(f"[red]Error:[/red] Failed to write output: {e}")
            return False


def _print_results_summary(total: int, without_website: int, verbose: bool) -> None:
    """Print summary of website check results."""
    with_website = total - without_website

    console.print()

    if verbose:
        table = Table(title="Results Summary", show_header=True, header_style="bold")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right")
        table.add_column("Percentage", justify="right")

        table.add_row(
            "Total businesses checked",
            str(total),
            "100%",
        )
        table.add_row(
            "With website",
            str(with_website),
            f"{with_website/total*100:.1f}%" if total > 0 else "0%",
        )
        table.add_row(
            "Without website",
            str(without_website),
            f"{without_website/total*100:.1f}%" if total > 0 else "0%",
            style="bold green" if without_website > 0 else None,
        )

        console.print(table)
    else:
        console.print(f"[dim]Businesses checked:[/dim] {total}")
        console.print(f"[dim]With website:[/dim]      {with_website}")
        console.print(f"[bold green]Without website:[/bold green]   {without_website}")


def _print_completion(output_path: str, count: int) -> None:
    """Print completion message with output location."""
    console.print()
    console.print(Panel.fit(
        f"[green]Success![/green] Found [bold]{count}[/bold] business{'es' if count != 1 else ''} without websites.\n"
        f"Results saved to: [cyan]{output_path}[/cyan]",
        border_style="green",
    ))
    console.print()


if __name__ == "__main__":
    main()
