"""Naver Webtoon scraper using nodriver (stealth browser).

Two-phase download:
1. Browser phase: nodriver loads page, scrolls to trigger lazy loading,
   extracts image URLs via JS evaluation.
2. HTTP phase: Download images with requests + Referer header (faster than CDP).

nodriver is the successor to undetected-chromedriver: async, direct CDP,
stealth by default (no webdriver flag, no automation detection).
Limitation: must run headed (headless is buggy with nodriver).
"""

import asyncio
import json
import logging
import os
from urllib.parse import parse_qs, urlparse

import requests

from .config import DATA_DIR, DOWNLOAD_TIMEOUT, SCROLL_PAUSE

log = logging.getLogger(__name__)


def parse_naver_url(url: str) -> dict:
    """Extract titleId and episode number from a Naver Webtoon URL.

    Supports both mobile and desktop URLs:
      https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297
      https://comic.naver.com/webtoon/detail?titleId=747269&no=297
    """
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    title_id = params.get("titleId", [None])[0]
    episode_no = params.get("no", [None])[0]

    if not title_id:
        raise ValueError(f"Could not extract titleId from URL: {url}")

    return {
        "title_id": title_id,
        "episode_no": episode_no,
        "base_url": f"{parsed.scheme}://{parsed.netloc}",
    }


def _output_dir_for_episode(title_id: str, episode_no: str | None) -> str:
    """Build output directory path for a specific episode."""
    parts = [DATA_DIR, title_id]
    if episode_no:
        parts.append(f"chapter_{episode_no}")
    return os.path.join(*parts)


async def _scroll_and_extract_urls(page) -> list[str]:
    """Scroll the page to trigger lazy loading, then extract image URLs."""
    # Wait for initial content to render
    await page.sleep(3)

    # Scroll incrementally to trigger lazy loading
    prev_height = 0
    stable_count = 0

    for _ in range(200):  # Safety limit
        await page.evaluate("window.scrollBy(0, 800)")
        await page.sleep(SCROLL_PAUSE)

        curr_height = await page.evaluate("document.documentElement.scrollHeight")
        if curr_height == prev_height:
            stable_count += 1
            if stable_count >= 5:
                break
        else:
            stable_count = 0
        prev_height = curr_height

    # Scroll back through the page in chunks to trigger any remaining lazy images
    await page.evaluate("window.scrollTo(0, 0)")
    await page.sleep(1)
    total_height = await page.evaluate("document.documentElement.scrollHeight")
    for y in range(0, total_height, 1000):
        await page.evaluate(f"window.scrollTo(0, {y})")
        await page.sleep(0.3)
    await page.sleep(2)

    # Extract image URLs via JS — return as JSON string to avoid nodriver
    # object serialization issues with complex return types
    urls_json = await page.evaluate("""
        JSON.stringify(
            Array.from(document.querySelectorAll('img.toon_image'))
                .map(img => img.src || img.dataset.src || '')
                .filter(src => src && src.startsWith('http') && !src.includes('bg_transparency'))
        )
    """)
    try:
        urls = json.loads(urls_json)
    except (json.JSONDecodeError, TypeError):
        urls = []

    if not urls:
        log.warning("No image URLs found on page")

    return urls


async def _browser_get_urls(url: str) -> tuple[list[str], str]:
    """Use nodriver to load page and extract image URLs.

    Returns (image_urls, user_agent) — the UA is captured from the real browser
    so the HTTP download phase sends the exact same fingerprint.
    """
    import nodriver as uc

    config = uc.Config()
    config.sandbox = False  # Required for some environments
    config.headless = True
    config.add_argument("--disable-gpu")
    config.add_argument("--disable-software-rasterizer")
    browser = await uc.start(config=config)
    try:
        page = await browser.get(url)
        urls = await _scroll_and_extract_urls(page)
        # Capture the browser's real UA for download phase
        ua = await page.evaluate("navigator.userAgent")
        return urls, ua or ""
    finally:
        browser.stop()


def _download_images(urls: list[str], out_dir: str, referer: str,
                     user_agent: str = "") -> list[str]:
    """Download images via HTTP with Referer header. Smart-skip existing files.

    Returns list of saved file paths.
    """
    os.makedirs(out_dir, exist_ok=True)
    saved = []

    _ua = user_agent or (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    headers = {
        "Referer": referer,
        "User-Agent": _ua,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Ch-Ua-Platform": '"Linux"',
    }

    for i, img_url in enumerate(urls):
        # Determine filename
        ext = _guess_extension(img_url)
        filename = f"{i + 1:03d}{ext}"
        filepath = os.path.join(out_dir, filename)

        # Smart skip: don't re-download existing files
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            log.debug("Skipping existing: %s", filename)
            saved.append(filepath)
            continue

        try:
            resp = requests.get(img_url, headers=headers, timeout=DOWNLOAD_TIMEOUT)
            resp.raise_for_status()

            with open(filepath, "wb") as f:
                f.write(resp.content)

            saved.append(filepath)
            log.info("Downloaded: %s (%d bytes)", filename, len(resp.content))
        except Exception as e:
            log.warning("Failed to download %s: %s", img_url, e)

    return saved


def _guess_extension(url: str) -> str:
    """Guess file extension from URL."""
    path = urlparse(url).path.lower()
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    return ".jpg"


def download_episode(url: str) -> list[str]:
    """Download all images from a Naver Webtoon episode.

    Args:
        url: Full Naver Webtoon episode URL.

    Returns:
        List of saved image file paths.
    """
    parsed = parse_naver_url(url)
    out_dir = _output_dir_for_episode(parsed["title_id"], parsed["episode_no"])

    log.info("Downloading episode: titleId=%s, no=%s", parsed["title_id"], parsed["episode_no"])
    log.info("Output directory: %s", out_dir)

    # Phase 1: Browser extracts image URLs + real User-Agent
    image_urls, browser_ua = asyncio.run(_browser_get_urls(url))
    log.info("Found %d images", len(image_urls))

    if not image_urls:
        return []

    # Phase 2: HTTP download with matching browser fingerprint
    referer = f"{parsed['base_url']}/webtoon/detail?titleId={parsed['title_id']}"
    return _download_images(image_urls, out_dir, referer, user_agent=browser_ua)


def download_chapter_range(url: str, start: int, end: int) -> dict[int, list[str]]:
    """Download multiple chapters from a Naver Webtoon series.

    Args:
        url: Any episode URL (used to extract titleId and base URL).
        start: First chapter number.
        end: Last chapter number (inclusive).

    Returns:
        Dict mapping chapter number to list of saved file paths.
    """
    parsed = parse_naver_url(url)
    results = {}

    for chapter_no in range(start, end + 1):
        chapter_url = (
            f"{parsed['base_url']}/webtoon/detail"
            f"?titleId={parsed['title_id']}&no={chapter_no}"
        )
        log.info("=== Chapter %d ===", chapter_no)
        try:
            files = download_episode(chapter_url)
            results[chapter_no] = files
        except Exception as e:
            log.error("Failed to download chapter %d: %s", chapter_no, e)
            results[chapter_no] = []

    return results
