#!/usr/bin/env python3
"""Start the lightweight local AWS review dashboard."""

import logging
import os

from dashboard_app import create_app


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.getenv("DASHBOARD_PORT", "8501")),
        debug=False,
        use_reloader=False,
        threaded=False,
    )
