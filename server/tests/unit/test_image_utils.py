"""Unit tests for image utility functions."""

import numpy as np
from PIL import Image

from kindle.image_utils import (
    crop_region_pil,
    clear_text_in_region,
    pil_to_cv2,
    cv2_to_pil,
    normalize_bbox,
)


class TestCropRegionPil:
    def test_crops_correct_region(self):
        img = Image.new("RGB", (100, 100), color=(255, 0, 0))
        cropped = crop_region_pil(img, (10, 10, 50, 50))
        assert cropped.size == (40, 40)

    def test_crop_preserves_content(self):
        img = Image.new("RGB", (100, 100), color=(0, 255, 0))
        cropped = crop_region_pil(img, (0, 0, 10, 10))
        assert cropped.getpixel((0, 0)) == (0, 255, 0)


class TestClearTextInRegion:
    def test_fills_white(self):
        img = Image.new("RGB", (100, 100), color=(0, 0, 0))
        clear_text_in_region(img, (10, 10, 50, 50))
        assert img.getpixel((30, 30)) == (255, 255, 255)

    def test_outside_region_unchanged(self):
        img = Image.new("RGB", (100, 100), color=(0, 0, 0))
        clear_text_in_region(img, (10, 10, 50, 50))
        assert img.getpixel((0, 0)) == (0, 0, 0)


class TestColorConversion:
    def test_pil_to_cv2_and_back(self):
        img = Image.new("RGB", (10, 10), color=(128, 64, 32))
        cv_img = pil_to_cv2(img)
        assert cv_img.shape == (10, 10, 3)
        # OpenCV is BGR
        assert cv_img[0, 0, 0] == 32   # B
        assert cv_img[0, 0, 2] == 128  # R
        result = cv2_to_pil(cv_img)
        assert result.getpixel((0, 0)) == (128, 64, 32)


class TestNormalizeBbox:
    def test_full_range(self):
        bbox = normalize_bbox([0, 0, 999, 999], 1000, 1000)
        assert bbox == (0, 0, 1000, 1000)

    def test_clamps_to_bounds(self):
        bbox = normalize_bbox([0, 0, 1500, 1500], 100, 100)
        assert bbox[2] <= 100
        assert bbox[3] <= 100
