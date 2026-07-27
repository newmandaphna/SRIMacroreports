"""Production entry point for Replit deployments.

uvicorn --factory is not supported by Replit's publishing system, so this module
exposes a pre-built app instance. The factory (app/main.py:create_app) is still
used for tests and for local dev via --factory.
"""

from app.main import create_app

app = create_app()
