#!/usr/bin/env python3
"""Entry point: python3 -m partition_etl.cli ... or ./partition-etl ..."""
from partition_etl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
