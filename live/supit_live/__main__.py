import uvicorn

from .server import PORT

uvicorn.run("supit_live.server:app", host="127.0.0.1", port=PORT, log_level="warning", timeout_graceful_shutdown=2)
