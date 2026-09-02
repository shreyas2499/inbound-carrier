"""Entrypoint / front door. Gunicorn and the dev server import `app` from here
(see the Dockerfile). This file holds no logic — routes live in adapter.routes,
and everything they call lives in the layers beneath. Building `app` opens no
connection; the TMS client only dials when a request arrives.
"""
from adapter.app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
