"""ASGI entry point and development server command."""

from rat.api import create_app

app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("rat.main:app", host="127.0.0.1", port=8000, reload=False)
