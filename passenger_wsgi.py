import sys
import os

# cPanel sets this — points to your virtualenv Python
INTERP = os.path.expanduser("~/virtualenv/lab/cs2-demo-tool/bin/python")
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Set working directory so relative paths (demos/, cache/, maps/) resolve correctly
os.chdir(os.path.dirname(__file__))

from server import app

# Passenger expects a WSGI-callable named 'application'
# FastAPI/Starlette apps are ASGI; we wrap with a2wsgi for Passenger compatibility
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
except ImportError:
    # Fallback: use asgiref SyncToAsync wrapper
    from asgiref.wsgi import WsgiToAsgi
    application = app
