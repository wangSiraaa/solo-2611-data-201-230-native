"""Package / module entry point: ``python -m provenance_etl {...}``."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
