"""Website checker component for verifying if businesses have dedicated websites."""

import asyncio
import re
from urllib.parse import quote_plus

import aiohttp

from .models import Business


# Domains that indicate directory/social listings, not dedicated business websites
EXCLUDED_DOMAINS = {
    # Social media
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "tiktok.com",
    "youtube.com",
    "pinterest.com",
    # Business directories
    "yelp.com",
    "yellowpages.com",
    "whitepages.com",
    "manta.com",
    "bbb.org",
    "angieslist.com",
    "homeadvisor.com",
    "thumbtack.com",
    "houzz.com",
    # Review sites
    "tripadvisor.com",
    "trustpilot.com",
    "glassdoor.com",
    # Maps and local listings
    "google.com",
    "maps.google.com",
    "bing.com",
    "mapquest.com",
    "apple.com",
    # Food delivery / reservations
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
    "postmates.com",
    "opentable.com",
    "seamless.com",
    # General directories
    "chamberofcommerce.com",
    "dnb.com",
    "buzzfile.com",
    "corporationwiki.com",
    "opencorporates.com",
    "zoominfo.com",
    "crunchbase.com",
    # Search engines and aggregators
    "duckduckgo.com",
    "yahoo.com",
    "ask.com",
}

# Patterns for website builder subdomains (these ARE valid business websites)
WEBSITE_BUILDER_PATTERNS = [
    r"\.wixsite\.com$",
    r"\.squarespace\.com$",
    r"\.weebly\.com$",
    r"\.godaddysites\.com$",
    r"\.square\.site$",
    r"\.carrd\.co$",
    r"\.webflow\.io$",
    r"\.shopify\.com$",
    r"\.bigcartel\.com$",
    r"\.wordpress\.com$",
]

DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
REQUEST_TIMEOUT = 10
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _extract_urls_from_html(html: str) -> list[str]:
    """Extract result URLs from DuckDuckGo HTML search results.

    DuckDuckGo HTML returns results with links in the format:
    <a rel="nofollow" class="result__a" href="...">
    """
    # Pattern to find result links
    pattern = r'class="result__a"[^>]*href="([^"]+)"'
    matches = re.findall(pattern, html)

    # Also try alternative pattern for result URLs
    alt_pattern = r'<a[^>]*class="[^"]*result[^"]*"[^>]*href="([^"]+)"'
    alt_matches = re.findall(alt_pattern, html)

    # DuckDuckGo sometimes wraps URLs in redirects, extract actual URL
    urls = []
    for url in matches + alt_matches:
        # Handle DuckDuckGo redirect URLs
        if "uddg=" in url:
            # Extract the actual URL from the redirect
            uddg_match = re.search(r'uddg=([^&]+)', url)
            if uddg_match:
                from urllib.parse import unquote
                urls.append(unquote(uddg_match.group(1)))
        elif url.startswith("http"):
            urls.append(url)

    return list(set(urls))  # Remove duplicates


def _extract_domain(url: str) -> str:
    """Extract the domain from a URL."""
    # Remove protocol
    domain = re.sub(r'^https?://', '', url)
    # Remove path
    domain = domain.split('/')[0]
    # Remove www prefix
    domain = re.sub(r'^www\.', '', domain)
    return domain.lower()


def _is_excluded_domain(url: str) -> bool:
    """Check if URL is from an excluded domain (directories, social media, etc.)."""
    domain = _extract_domain(url)

    # Check exact matches and subdomain matches
    for excluded in EXCLUDED_DOMAINS:
        if domain == excluded or domain.endswith('.' + excluded):
            return True

    return False


def _is_website_builder_site(url: str) -> bool:
    """Check if URL is a website builder subdomain (counts as valid website)."""
    domain = _extract_domain(url)

    for pattern in WEBSITE_BUILDER_PATTERNS:
        if re.search(pattern, domain):
            return True

    return False


def _is_likely_business_website(url: str, business_name: str) -> bool:
    """Determine if a URL is likely the business's own website.

    Returns True if:
    - URL is from a website builder (wix, squarespace, etc.)
    - URL is not from an excluded domain AND domain contains business name keywords
    """
    if _is_excluded_domain(url):
        return False

    if _is_website_builder_site(url):
        return True

    # For custom domains, check if domain relates to business name
    domain = _extract_domain(url)

    # Extract meaningful words from business name (ignore common words)
    ignore_words = {
        "the", "and", "of", "in", "at", "to", "for", "a", "an",
        "llc", "inc", "corp", "co", "company", "business", "services",
        "restaurant", "cafe", "shop", "store", "salon", "studio",
    }

    name_words = [
        word.lower() for word in re.findall(r'\w+', business_name)
        if len(word) > 2 and word.lower() not in ignore_words
    ]

    # Check if any significant word from business name appears in domain
    domain_clean = domain.replace('-', '').replace('.', '')
    for word in name_words:
        if word in domain_clean:
            return True

    # If we have a non-excluded domain that's not clearly a match,
    # still consider it potentially valid (benefit of the doubt)
    # This catches cases where business name differs from domain
    return True


async def check_website(business: Business) -> bool:
    """Check if a business has a dedicated website.

    Searches DuckDuckGo for the business and analyzes results to determine
    if the business has its own website (not just social media or directory listings).

    Args:
        business: Business object with name and address information.

    Returns:
        True if the business appears to have a dedicated website.
        False if no website found, only directory listings, or on error.
    """
    # Build search query using business name and address for specificity
    # Use address to help disambiguate businesses with common names
    search_query = f"{business.name} {business.address}"

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # Perform the search
            params = {"q": search_query}
            async with session.post(DUCKDUCKGO_HTML_URL, data=params) as response:
                if response.status != 200:
                    return False

                html = await response.text()

            # Extract URLs from search results
            urls = _extract_urls_from_html(html)

            if not urls:
                return False

            # Check if any URL appears to be the business's own website
            for url in urls[:10]:  # Check top 10 results
                if _is_likely_business_website(url, business.name):
                    if not _is_excluded_domain(url):
                        return True

            return False

    except (aiohttp.ClientError, asyncio.TimeoutError, Exception):
        # Return False on any error - safer for finding businesses WITHOUT websites
        return False


async def check_websites_batch(
    businesses: list[Business],
    concurrency: int = 5,
    delay: float = 1.0,
) -> list[Business]:
    """Check websites for multiple businesses with rate limiting.

    Args:
        businesses: List of Business objects to check.
        concurrency: Maximum concurrent requests.
        delay: Delay between batches in seconds.

    Returns:
        List of Business objects with has_website field updated.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def check_with_semaphore(business: Business) -> Business:
        async with semaphore:
            has_website = await check_website(business)
            business.has_website = has_website
            await asyncio.sleep(delay)  # Rate limiting
            return business

    tasks = [check_with_semaphore(b) for b in businesses]
    results = await asyncio.gather(*tasks)

    return list(results)
