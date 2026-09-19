#!/usr/bin/env python3
"""Thin entry over engine.server. Installed as-is; the proxy lives in engine/."""

from engine.server import main

if __name__ == "__main__":
    main()
