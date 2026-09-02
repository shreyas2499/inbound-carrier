"""Flask entrypoint for the TMS adapter.

Phase 0: only /health exists, so the service is deployable from day one. The
load tools (search_loads / get_load / book_load / evaluate_offer) land in
Phase 3, once the socket client (Phase 2) is in place.

Flask's synchronous model is a deliberate fit: the adapter's job is to wrap the
TMS's blocking TCP socket calls in HTTP, and a plain request -> open socket ->
respond flow reads top to bottom with no async machinery.
"""
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


if __name__ == "__main__":
    # Local dev server only. Docker runs this under gunicorn (see Dockerfile).
    app.run(host="0.0.0.0", port=8000)
