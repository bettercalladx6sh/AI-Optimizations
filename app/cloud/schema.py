"""JSON Schema validation for evidence documents (JSON Schema draft 2020-12).

Provider-neutral: each evidence type ships its own schema file (e.g. the EC2 schema in
app/cloud/aws/ec2/schemas/) and validates with validate_document(). Every problem is
returned, not just the first, each with a JSONPath-style location such as
$.instances[0].metrics.cpu_utilization.cumulative.p95.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.auth.common import AuthError


class EvidenceValidationError(AuthError):
    """Raised when an evidence document or a schema file is invalid."""


@dataclass(frozen=True)
class ValidationIssue:
    """One problem found in a document: where it is and what is wrong."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def json_path(parts: Any) -> str:
    """Return e.g. '$.instances[0].metrics' for the path parts ['instances', 0, 'metrics']."""
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


@lru_cache(maxsize=None)
def load_validator(schema_path: Path) -> Draft202012Validator:
    """Load a schema file once, check that it is a valid draft 2020-12 schema, and build its validator."""
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except FileNotFoundError:
        raise EvidenceValidationError(f"schema file not found: {schema_path}") from None
    except (json.JSONDecodeError, SchemaError) as exc:
        raise EvidenceValidationError(f"schema file {schema_path.name} is invalid: {exc}") from exc
    return Draft202012Validator(schema)


def validate_document(document: Any, schema_path: Path) -> list[ValidationIssue]:
    """Validate a document against a schema file; return every issue (empty when valid)."""
    validator = load_validator(schema_path)
    errors = sorted(validator.iter_errors(document), key=lambda error: (list(map(str, error.absolute_path)), error.message))
    return [ValidationIssue(json_path(error.absolute_path), error.message) for error in errors]