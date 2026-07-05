"""
WSGI entry point for Gunicorn on Render.
Usage (Render Start Command):
    gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 1 --threads 2
"""

from app import app

if __name__ == "__main__":
    app.run()
