"""Trusted pytest plugin; selects candidate source without importing its config."""

import os
import sys


def pytest_configure(config):
    sys.path.insert(0, os.environ["CANDIDATE_ROOT"])
