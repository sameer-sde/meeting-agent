"""Vercel entry point: serves the Flask app in web/app.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.app import app  # noqa: E402,F401
