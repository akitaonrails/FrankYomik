"""Integration tests for webtoon scraper (URL parsing and smart-skip)."""

import pytest

from webtoon.scraper import parse_naver_url, _download_images, _guess_extension


class TestParseNaverUrl:
    def test_mobile_url(self):
        url = "https://m.comic.naver.com/webtoon/detail?titleId=747269&no=297"
        result = parse_naver_url(url)
        assert result["title_id"] == "747269"
        assert result["episode_no"] == "297"
        assert "m.comic.naver.com" in result["base_url"]

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
