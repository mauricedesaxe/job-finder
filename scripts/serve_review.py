# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false
from __future__ import annotations

import psycopg
from fasthtml.common import serve

from job_finder.config import DatabaseSettings
from job_finder.review import create_review_app, postgres_review_service

settings = DatabaseSettings.from_environment()
service = postgres_review_service(lambda: psycopg.connect(settings.postgres_dsn, autocommit=True))
app = create_review_app(service)

if __name__ == "__main__":
    serve()
