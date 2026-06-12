"""
constants.py — Package-wide constants for StudySync CLI.

PRODUCTION_SERVER_URL is the single source of truth for the default backend.
It is used as the fallback in every network call so users who install via
`pip install studysync` can run `study join <TOKEN>` immediately — no
--server flag required.

Advanced users hosting their own backend can override this at any point:
    study workspace create my-ws --server http://192.168.1.42:8000
    study join <TOKEN>           --server https://my-own-instance.com
The chosen URL is persisted to ~/.study/config.json after the first use,
so subsequent commands pick it up automatically.
"""

# The public StudySync backend.  Update this constant when the production
# deployment URL changes and bump the package version accordingly.
PRODUCTION_SERVER_URL: str = "https://studysync-backend-pfft.onrender.com"
