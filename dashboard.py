#!/usr/bin/env python3
"""Start the lightweight local AWS review dashboard."""

from dashboard_app import create_app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8501, debug=False, use_reloader=False, threaded=False)
