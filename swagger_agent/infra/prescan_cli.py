"""Standalone CLI for testing the prescan module.

Usage:
    python -m swagger_agent.infra.prescan_cli /path/to/repo
    python -m swagger_agent.infra.prescan_cli /path/to/repo --scratchpad
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from swagger_agent.infra.prescan import run_prescan, prescan_to_scratchpad


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic pre-scan on a codebase")
    parser.add_argument("target_dir", help="Path to the codebase to analyze")
    parser.add_argument("--scratchpad", action="store_true", help="Show the generated scratchpad")
    args = parser.parse_args()

    target_dir = os.path.abspath(args.target_dir)
    if not os.path.isdir(target_dir):
        print(f"Error: {target_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    t0 = time.monotonic()
    result = run_prescan(target_dir)
    elapsed = (time.monotonic() - t0) * 1000

    print(f"Framework:  {result.framework or '(none)'}")
    print(f"Language:   {result.language or '(none)'}")
    print(f"Routes:     {len(result.route_files)} file(s)")
    for rf in result.route_files:
        print(f"  - {rf}")
    print(f"Servers:    {', '.join(result.servers) or '(none)'}")
    print(f"Base path:  {result.base_path or '(none)'}")
    print(f"Time:       {elapsed:.1f}ms")
    print()
    print("Notes:")
    for note in result.notes:
        print(f"  - {note}")

    if args.scratchpad:
        print()
        print("=" * 60)
        print("GENERATED SCRATCHPAD:")
        print("=" * 60)
        print(prescan_to_scratchpad(result))


if __name__ == "__main__":
    main()
