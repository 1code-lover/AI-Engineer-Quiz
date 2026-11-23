"""Shared test configuration.

Set the throwaway data directory before any test module imports ``keepstack``:
``config`` reads the environment once at import time. Using ``setdefault``
keeps a caller-provided directory (for example while debugging) intact and
guarantees tests never write into the repository's ``data/``.
"""
import os
import tempfile

os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-"))
os.environ.setdefault("KEEPSTACK_AI_ENABLED", "false")
os.environ.setdefault("KEEPSTACK_OCR_ENABLED", "false")
