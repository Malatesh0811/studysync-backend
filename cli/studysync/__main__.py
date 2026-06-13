"""Allow `python -m studysync` as a fallback when `study` is not on PATH."""
from studysync.main import app

if __name__ == "__main__":
    app()
