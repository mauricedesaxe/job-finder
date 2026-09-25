from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from job_finder.benchmarks.identity import canonical_digest


def test_canonical_digest_preserves_benchmark_identity_format() -> None:
    value = {
        "z": [Decimal("1.20"), datetime(2026, 9, 25, tzinfo=UTC)],
        "a": UUID("12345678-1234-5678-1234-567812345678"),
    }

    assert canonical_digest(value) == (
        "a524cda0576148a8daf8ae7970ca010bf38ea0c6004e2cbe2c65b8dc58a13822"
    )
