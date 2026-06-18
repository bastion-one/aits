"""Structured verification result: ``Ok`` | ``Err``.

Verification primitives never raise on tamper findings; they return ``Err`` with
forensic detail (record kind, key, property violated, expected, actual). Real
exceptions remain for programmer errors.
"""

from dataclasses import dataclass
from typing import Any, Union


@dataclass(frozen=True)
class Ok:
    @property
    def is_ok(self) -> bool:
        return True

    @property
    def is_err(self) -> bool:
        return False

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class Err:
    record_kind: str
    record_key: Any
    property_violated: str
    expected: Any
    actual: Any
    message: str

    @property
    def is_ok(self) -> bool:
        return False

    @property
    def is_err(self) -> bool:
        return True

    def __bool__(self) -> bool:
        return False


VerificationResult = Union[Ok, Err]
