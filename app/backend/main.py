"""ASGI entry point for the local audiobook web application."""

from .api import create_app

app = create_app()
