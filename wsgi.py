"""
WSGI entry point for Gunicorn on Render.
Usage (Render Start Command):
    gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 2
"""

import os
import sys

# FIX for Angel One 'smartapi-python' Linux case-sensitivity bug
for p in sys.path:
    smart_upper = os.path.join(p, "SmartApi")
    smart_lower = os.path.join(p, "smartapi")
    if os.path.isdir(smart_upper) and not os.path.exists(smart_lower):
        try:
            os.symlink(smart_upper, smart_lower)
        except OSError:
            pass

from app import app

if __name__ == "__main__":
    app.run()
