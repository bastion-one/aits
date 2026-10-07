"""The commit-log head read is bounded, so append cost does not grow with the log."""

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app import ledger
from app.models import LineageTag


def test_append_reads_only_the_head_entry(engine: Engine, session: Session) -> None:
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    when = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for step in ("first", "second"):
        event.listen(engine, "before_cursor_execute", capture)
        try:
            ledger.record(
                session,
                LineageTag(actor_id="a", step_id=step, transformation="t", occurred_at=when),
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

    head_reads = [s for s in statements if "FROM commit_log" in s and "ORDER BY" in s]
    assert len(head_reads) == 2
    assert all("LIMIT" in s for s in head_reads), head_reads
