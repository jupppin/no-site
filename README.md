# no-site

A Python CLI tool that finds local businesses without websites. Enter a location and radius, and it identifies businesses that lack a web presence - perfect for web developers looking for potential clients.

## How It Works

1. **Geocode** - Converts your location (zip code or neighborhood) to coordinates using Nominatim
2. **Fetch** - Queries OpenStreetMap for businesses in the area (shops, restaurants, offices, services)
3. **Check** - Searches the web to verify which businesses have dedicated websites
4. **Export** - Outputs a CSV of businesses without websites

## Installation

```bash
# Clone the repo
git clone https://github.com/jupppin/no-site.git
cd no-site

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Basic usage with zip code
python -m src.main --location "10001" --radius 1 --output businesses.csv

# Search by neighborhood
python -m src.main --location "SoHo, New York" --radius 0.5 --output soho.csv

# Limit results (recommended for large areas)
python -m src.main --location "Buffalo, NY" --radius 1 --limit 50 --output buffalo.csv
```

### Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--location` | `-l` | required | Zip code or neighborhood name |
| `--radius` | `-r` | 1.0 | Search radius in miles (max 3.1) |
| `--output` | `-o` | businesses.csv | Output CSV file path |
| `--limit` | `-n` | none | Max businesses to check |
| `--concurrency` | `-c` | 5 | Concurrent website checks |
| `--delay` | | 1.0 | Delay between checks (seconds) |
| `--verbose` | `-v` | false | Show detailed progress |
| `--quiet` | `-q` | false | Suppress output except errors |

## Output

The CSV contains:
- `business_name` - Name of the business
- `address` - Street address (when available)
- `phone` - Phone number (when available)
- `website_status` - `not_found` for businesses without websites

## Example Output

```
business_name,address,phone,website_status
Joe's Barber Shop,"123 Main St, Buffalo, NY",(716) 555-1234,not_found
Corner Deli,,,not_found
Main Street Laundromat,"456 Main St, Buffalo, NY",,not_found
```

## Tips

- **Start small** - Use `--limit 20` to test a new area first
- **Smaller radius** - Large areas (>1 mile) can have thousands of businesses
- **API limits** - The Overpass API may timeout during busy periods; just retry
- **Verify results** - Always double-check before reaching out to businesses

## Dependencies

- `aiohttp` - Async HTTP requests
- `click` - CLI framework
- `rich` - Terminal formatting
- `geopy` - Geocoding utilities

## License

MIT
