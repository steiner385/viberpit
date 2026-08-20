"""Unit tests for the pure helpers in scripts/ccswitch-sqlite-to-pg.py.

_combine_where and _row_coercers are the entire fix for two migration defects
(the SQL OR-precedence hazard in the retention predicate, and the JSON/DateTime
coercion needed because rows are read via the raw sqlite3 driver rather than
through SQLAlchemy's type decoders). Both are pure functions — no database
needed, just import the module directly.

The script's filename is hyphenated (not a valid module name) and its bottom
`if __name__ == "__main__": asyncio.run(main())` guard is what makes a plain
import safe — without that guard, importing the module would attempt to run
the full migration against pytest's own argv.
"""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ccswitch-sqlite-to-pg.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("ccswitch_sqlite_to_pg", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so dataclass-style / typing forward
    # refs inside the module (if any) can resolve it by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_module():
    return _load_migration_module()


# --------------------------------------------------------------------------- #
# _combine_where
# --------------------------------------------------------------------------- #

def test_combine_where_empty_base_just_wraps_extra(migration_module):
    assert migration_module._combine_where("", "x IS NOT NULL") == " WHERE x IS NOT NULL"


def test_combine_where_parenthesizes_or_predicate(migration_module):
    """The precedence hazard: _retention_where returns a WHERE clause built from
    several ORs (`outcome IS NULL OR created_ts IS NULL OR outcome IN (...) OR
    created_ts >= ...`). Appending an extra AND condition without parenthesizing
    the existing OR-chain would change which rows the extra condition actually
    scopes — `A OR B OR C AND D` binds as `A OR B OR (C AND D)`, not
    `(A OR B OR C) AND D`. _combine_where must wrap the base clause in
    parens before ANDing on the extra condition."""
    base = migration_module._retention_where("ci_test_result", raw_days=7.0)
    assert " OR " in base  # sanity: this is in fact an OR-chain

    combined = migration_module._combine_where(base, '"col" IS NOT NULL')

    # The OR-chain must be wrapped in its own parens, ANDed with the extra
    # condition — not just concatenated, which would silently break precedence.
    condition = base.strip()[len("WHERE "):]
    assert combined == f" WHERE ({condition}) AND \"col\" IS NOT NULL"

    # Simulate SQL boolean evaluation to prove the parenthesized form actually
    # changes behavior vs. naive concatenation for a row that would otherwise
    # slip through: outcome IS NULL (true) OR ... AND extra (false) — without
    # parens, the OR short-circuits and the AND is only applied to the last
    # disjunct, letting a NULL-outcome/NULL-extra row match when it should not.
    def eval_naive(outcome_is_null, created_ts_is_null, extra_true):
        # A OR B OR C AND D  ==  A OR B OR (C AND D)
        return outcome_is_null or created_ts_is_null or (False and extra_true)

    def eval_parenthesized(outcome_is_null, created_ts_is_null, extra_true):
        # (A OR B OR C) AND D
        return (outcome_is_null or created_ts_is_null or False) and extra_true

    # outcome IS NULL true, extra condition false: naive form matches (bug),
    # parenthesized form correctly excludes it.
    assert eval_naive(True, False, False) is True
    assert eval_parenthesized(True, False, False) is False


def test_combine_where_non_or_base_still_parenthesizes(migration_module):
    """Even a base clause without an OR gets the same (…) AND … treatment —
    _combine_where doesn't special-case the OR precedence hazard, it always
    parenthesizes, which is safe for any base clause."""
    combined = migration_module._combine_where(" WHERE a = 1", "b = 2")
    assert combined == " WHERE (a = 1) AND b = 2"


# --------------------------------------------------------------------------- #
# _row_coercers
# --------------------------------------------------------------------------- #

class _FakeCol:
    def __init__(self, name, type_):
        self.name = name
        self.type = type_


class _FakeTable:
    def __init__(self, columns):
        self.columns = columns


def _coercer_for(migration_module, col_type, col_name="col"):
    table = _FakeTable([_FakeCol(col_name, col_type)])
    coercers = migration_module._row_coercers(table)
    assert len(coercers) == 1
    name, fn = coercers[0]
    assert name == col_name
    return fn


def test_row_coercers_leaves_none_untouched_datetime_aware(migration_module):
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=True))
    assert fn(None) is None


def test_row_coercers_leaves_none_untouched_datetime_naive(migration_module):
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=False))
    assert fn(None) is None


def test_row_coercers_leaves_none_untouched_json(migration_module):
    from sqlalchemy.types import JSON
    fn = _coercer_for(migration_module, JSON())
    assert fn(None) is None


def test_row_coercers_json_parses_string_but_passes_through_dict(migration_module):
    from sqlalchemy.types import JSON
    fn = _coercer_for(migration_module, JSON())

    assert fn('{"a": 1}') == {"a": 1}
    assert fn("[1, 2, 3]") == [1, 2, 3]

    # Already-parsed dict/list must pass through WITHOUT double-decoding —
    # json.loads on a dict would raise TypeError, and on a list-of-strings
    # would misinterpret it as more JSON text.
    already_dict = {"a": 1}
    already_list = [1, 2, 3]
    assert fn(already_dict) is already_dict
    assert fn(already_list) is already_list


def test_row_coercers_datetime_aware_column_attaches_utc_tzinfo(migration_module):
    """SQLite's DATETIME storage strips tzinfo on write, so a raw TEXT value
    read back for a timezone=True column arrives naive — _row_coercers must
    re-attach UTC tzinfo (every writer in this codebase uses UTC)."""
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=True))

    got = fn("2024-01-02T03:04:05")
    assert got == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert got.tzinfo is not None


def test_row_coercers_datetime_aware_column_preserves_explicit_offset(migration_module):
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=True))

    got = fn("2024-01-02T03:04:05+00:00")
    assert got == datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_row_coercers_datetime_naive_column_strips_tzinfo(migration_module):
    """The reverse case: a timezone=False column must come back naive even if
    the TEXT happens to carry an offset — asyncpg rejects an aware datetime
    bound to `timestamp without time zone`."""
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=False))

    got = fn("2024-01-02T03:04:05+00:00")
    assert got == datetime(2024, 1, 2, 3, 4, 5)
    assert got.tzinfo is None


def test_row_coercers_datetime_naive_column_leaves_already_naive_alone(migration_module):
    from sqlalchemy import DateTime
    fn = _coercer_for(migration_module, DateTime(timezone=False))

    got = fn("2024-01-02T03:04:05")
    assert got == datetime(2024, 1, 2, 3, 4, 5)
    assert got.tzinfo is None


def test_row_coercers_ignores_non_datetime_non_json_columns(migration_module):
    """A String/Integer/etc. column gets no coercer at all — raw TEXT/int
    passes straight through to asyncpg unmodified."""
    from sqlalchemy import Integer, String
    table = _FakeTable([_FakeCol("a", String()), _FakeCol("b", Integer())])
    coercers = migration_module._row_coercers(table)
    assert coercers == []
