import requests
import logging
import re
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# Headers to simulate a real browser and avoid bot-blocker 403s
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-419,es;q=0.8,en-US;q=0.5,en;q=0.3",
}

def scrape_product_page(url: str) -> tuple[str, str | None]:
    """
    Fetches the raw HTML from a product URL.
    Returns (html_text, primary_image_url).
    Primary image is detected by scanning <meta og:image> or <img> tags near h1/title.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        html = response.text

        # 1. Try Open Graph image (most reliable on e-commerce sites)
        og_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
        if not og_match:
            og_match = re.search(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.IGNORECASE)

        image_url = og_match.group(1) if og_match else None

        # 2. Fallback: look for first large product img tag
        if not image_url:
            img_match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+\.(jpg|jpeg|png|webp))["\']', html, re.IGNORECASE)
            if img_match:
                image_url = img_match.group(1)

        return html, image_url

    except requests.exceptions.RequestException as e:
        logger.error(f"Error scraping URL {url}: {e}")
        return None, None


def download_image_from_url(image_url: str) -> tuple[bytes | None, str | None]:
    """
    Downloads an image from a direct URL.
    Returns (image_bytes, content_type).
    """
    if not image_url:
        return None, None
    try:
        response = requests.get(image_url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "image/jpeg")
        return response.content, content_type
    except Exception as e:
        logger.error(f"Error downloading image from {image_url}: {e}")
        return None, None
