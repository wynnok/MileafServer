"""Executable with redacted configuration errors."""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from mileaf.app import create_app
from mileaf.config import Settings


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join("MILEAF_" + str(error["loc"][0]).upper() for error in exc.errors())
        print(f"Invalid or missing environment variables: {fields}", file=sys.stderr)
        raise SystemExit(1) from None
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
    host, _, port = settings.listen_address.rpartition(":")
    uvicorn.run(
        create_app(settings),
        host=host.strip("[]"),
        port=int(port),
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
