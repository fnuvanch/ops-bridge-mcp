"""Launch the Ops Console: ``python -m console [--port 8300] [--itsm-url URL]``."""

from __future__ import annotations

import argparse

import uvicorn

from console.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Ops Bridge console")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (keep it local).")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument(
        "--itsm-url",
        default=None,
        help="Use a separately running mock ITSM (e.g. http://127.0.0.1:8100). "
        "Omit to run the mock in-process.",
    )
    args = parser.parse_args()
    print(f"Ops console on http://{args.host}:{args.port}  (ITSM: {args.itsm_url or 'embedded'})")
    uvicorn.run(create_app(itsm_url=args.itsm_url), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
