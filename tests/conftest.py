"""Shared test fixtures and helpers."""

import os

import pytest

# Absolute path to docs/ directory containing test images
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs")


@pytest.fixture
def docs_dir():
    """Path to the docs/ directory with test manga images."""
    return DOCS_DIR
