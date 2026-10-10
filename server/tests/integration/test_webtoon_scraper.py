"""Integration tests for webtoon scraper (URL parsing and smart-skip)."""

from unittest.mock import AsyncMock, patch

import pytest

from webtoon.scraper import (
    _download_images,
    _guess_extension,
    download_episode,
    parse_naver_url,
)

pytestmark = pytest.mark.integration


class TestParseNaverUrl:
    def test_mobile_url(self):
        url = "https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297"
        result = parse_naver_url(url)
        assert result["title_id"] == "747269"
        assert result["episode_no"] == "297"
        assert result["base_url"] == "https://m.comic.naver.com"

    def test_desktop_url(self):
        url = "https://comic.naver.com/webtoon/detail?titleId=747269&no=297"
        result = parse_naver_url(url)
        assert result["title_id"] == "747269"
        assert result["episode_no"] == "297"

    def test_missing_title_id_raises(self):
        with pytest.raises(ValueError, match="titleId"):
            parse_naver_url("https://comic.naver.com/webtoon/list")

    def test_missing_episode_no(self):
        url = "https://comic.naver.com/webtoon/detail?titleId=747269"
        result = parse_naver_url(url)
        assert result["title_id"] == "747269"
        assert result["episode_no"] is None

    @pytest.mark.parametrize("url", [
        "https://m.comic.naver.com.evil.example/webtoon/detail?titleId=747269&no=297",
        "https://evil.example/?next=m.comic.naver.com&titleId=747269&no=297",
        "http://comic.naver.com/webtoon/detail?titleId=747269&no=297",
        "https://comic.naver.com:8443/webtoon/detail?titleId=747269&no=297",
        "https://user@comic.naver.com/webtoon/detail?titleId=747269&no=297",
    ])
    def test_rejects_non_naver_urls(self, url):
        with pytest.raises(ValueError, match="HTTPS Naver"):
            parse_naver_url(url)

    @pytest.mark.parametrize("query", [
        "titleId=../outside&no=297",
        "titleId=747269&no=../../outside",
        "titleId=747269&no=not-a-number",
    ])
    def test_rejects_unsafe_identifiers(self, query):
        with pytest.raises(ValueError, match="numeric"):
            parse_naver_url(f"https://comic.naver.com/webtoon/detail?{query}")

    def test_browser_receives_canonical_url(self, tmp_path):
        url = "https://comic.naver.com/other?titleId=747269&no=297&extra=ignored"
        with patch("webtoon.scraper._output_dir_for_episode", return_value=str(tmp_path)), \
             patch("webtoon.scraper._browser_get_urls", new_callable=AsyncMock) as browser:
            browser.return_value = ([], "test")
            assert download_episode(url) == []
        browser.assert_awaited_once_with(
            "https://comic.naver.com/webtoon/detail?titleId=747269&no=297")


class TestGuessExtension:
    def test_jpg(self):
        assert _guess_extension("https://example.com/img/001.jpg") == ".jpg"

    def test_png(self):
        assert _guess_extension("https://example.com/img/001.png") == ".png"

    def test_webp(self):
        assert _guess_extension("https://example.com/img/001.webp") == ".webp"

    def test_default_jpg(self):
        assert _guess_extension("https://example.com/img/001") == ".jpg"


class TestSmartSkip:
    def test_skips_existing_file(self, tmp_path):
        """Smart-skip should not re-download files that already exist."""
        # Create a pre-existing file
        existing = tmp_path / "001.jpg"
        existing.write_bytes(b"fake image data")

        # _download_images with a URL that would fail if actually downloaded
        result = _download_images(
            ["https://example.com/fake.jpg"],
            str(tmp_path),
            referer="https://example.com",
        )
        assert len(result) == 1
        assert result[0] == str(existing)
        # File content should be unchanged (not re-downloaded)
        assert existing.read_bytes() == b"fake image data"
