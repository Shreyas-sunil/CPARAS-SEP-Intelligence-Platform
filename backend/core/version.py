"""
Application identity — the ONE place the CPARAS version number lives.

Read by the FastAPI app metadata (main.py, shown in /api/docs), the health
endpoint (/api/health "version") and log lines. The frontend shows its own copy
(frontend/src/platform/products.ts PLATFORM_VERSION and frontend/package.json);
keep all three equal when releasing.
"""

SERVICE_NAME = "cparas-platform"
APP_VERSION = "0.5.0"
