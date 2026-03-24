"""ASGI entrypoint for AMC."""

from app.wiring import create_app

# Uvicorn target: `uvicorn main:app --app-dir src --reload`
app = create_app()

