"""Regression guard for a mistake made, and then caught, on this same
branch: migration `9815eb266923` (the billing-privilege-boundary
migration) is already merged into `master` via PR #2. A first attempt
at closing its hardcoded-`LOGIN`-password gap edited that migration in
place, changing its `CREATE ROLE skincare_billing WITH LOGIN PASSWORD
...` to `WITH NOLOGIN`. That edit is a real bug: Alembic identifies a
migration purely by its recorded revision id in `alembic_version` and
never re-runs one a database has already applied, so any database that
had already applied `9815eb266923` -- i.e. every real deployment past
the PR #2 merge -- would never see the edited text at all and would
stay stuck with the original hardcoded-password `LOGIN` role forever.
The fix only ever "worked" for a database created fresh after the
edit, which is precisely the kind of gap a fresh-schema-only test suite
would never catch.

`9815eb266923` has been restored verbatim to its merged-master
contents and must never be edited again. The actual LOGIN -> NOLOGIN
transition now lives in a new, separate, forward-only migration,
`1367b870bdcd`. This file proves that directly against the real
migration files (not a hand-copied paraphrase of their SQL, which
could silently drift out of sync with the actual migrations):

1. `9815eb266923`'s own source still creates `skincare_billing` LOGIN
   -- it was never edited to perform the NOLOGIN transition itself.
2. `1367b870bdcd`'s own source performs the NOLOGIN transition,
   embeds no password anywhere, and fails loudly rather than
   fabricating the role if it's unexpectedly missing.
3. Executing each migration's actual captured SQL, in revision order,
   against a real role reproduces the LOGIN -> NOLOGIN transition
   exactly as claimed -- proven inside a transaction that is always
   rolled back, so the real, shared `skincare_billing` role (and its
   `skincare_billing_runtime` membership, used by every other billing
   test in this suite) is never actually touched by this file.
4. The separately-provisioned `skincare_billing_runtime` runtime login
   is unaffected by any of the above: it can still connect, still
   inherits `skincare_billing`'s privileges, and is not itself a
   superuser/RLS-bypass role either.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"

HISTORICAL_MIGRATION_FILE = "9815eb266923_transfer_contract_and_billing_privilege.py"
NOLOGIN_MIGRATION_FILE = "1367b870bdcd_billing_role_nologin_transition.py"


def _load_migration_module(filename: str):
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captured_upgrade_statements(filename: str):
    """Imports the real migration file and runs its own upgrade()
    against a capturing stand-in for alembic.op.execute -- no database
    connection, no side effects -- returning exactly the SQL strings
    that migration would issue against a real one. This is what ties
    every assertion below to the migrations' actual, current source
    rather than to a copy that could quietly go stale."""
    module = _load_migration_module(filename)
    captured = []
    with patch("alembic.op.execute", side_effect=lambda sql, *a, **kw: captured.append(sql)):
        module.upgrade()
    return captured


def _statements_mentioning(statements, *substrings):
    return [s for s in statements if all(sub in s for sub in substrings)]


# ---------------------------------------------------------------------------
# 1 & 2: the migration SOURCE FILES themselves say the right thing --
# fails with no database involved if either migration is ever edited
# back into the mistake this guards against.
# ---------------------------------------------------------------------------


def test_historical_migration_still_creates_a_login_role():
    """The direct regression guard: if a future change edits
    9815eb266923 to create skincare_billing NOLOGIN (redoing the exact
    mistake an earlier attempt on this branch made), this fails."""
    statements = _captured_upgrade_statements(HISTORICAL_MIGRATION_FILE)
    create_role_statements = _statements_mentioning(statements, "CREATE ROLE skincare_billing")
    assert len(create_role_statements) == 1
    assert "LOGIN PASSWORD" in create_role_statements[0]
    assert "NOLOGIN" not in create_role_statements[0]


def test_nologin_transition_lives_in_the_new_forward_migration_and_embeds_no_password():
    statements = _captured_upgrade_statements(NOLOGIN_MIGRATION_FILE)
    alter_role_statements = _statements_mentioning(statements, "ALTER ROLE skincare_billing")
    assert len(alter_role_statements) == 1
    assert "NOLOGIN" in alter_role_statements[0]
    assert not any("PASSWORD" in s for s in statements)


def test_nologin_migration_fails_closed_if_role_is_missing_instead_of_fabricating_one():
    statements = _captured_upgrade_statements(NOLOGIN_MIGRATION_FILE)
    guard_statements = _statements_mentioning(statements, "RAISE EXCEPTION")
    assert len(guard_statements) == 1
    assert "skincare_billing" in guard_statements[0]
    # Never silently creates the role it expects to already exist.
    assert not any("CREATE ROLE" in s for s in statements)


# ---------------------------------------------------------------------------
# 3: the upgrade PATH itself, against a real role -- proves the
# captured SQL above really performs the LOGIN -> NOLOGIN transition
# when actually executed. Runs entirely inside one transaction that is
# always rolled back in a `finally`, regardless of outcome, so this
# never leaves a trace on the real, shared skincare_billing role.
# ---------------------------------------------------------------------------


async def _role_state(conn, rolname="skincare_billing"):
    return await conn.fetchrow(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
        "FROM pg_roles WHERE rolname = $1",
        rolname,
    )


async def test_upgrade_path_transitions_an_existing_login_role_to_nologin(db_pool):
    """Simulates the real, and only realistic, scenario this guards
    against: a database that already applied 9815eb266923
    (skincare_billing already exists, LOGIN, exactly as that
    migration's own IF-NOT-EXISTS-guarded CREATE ROLE left it) now
    applies 1367b870bdcd. The drop-then-recreate below uses
    9815eb266923's own captured CREATE ROLE statement verbatim, so it
    exercises the identical code path a genuinely fresh database's
    first-ever application of that migration would -- proving a fresh
    install and an existing database being upgraded converge on the
    exact same final role state, since both pass through this same
    statement and the same subsequent transition statement."""
    historical_statements = _captured_upgrade_statements(HISTORICAL_MIGRATION_FILE)
    role_creation_sql = _statements_mentioning(historical_statements, "CREATE ROLE skincare_billing")[0]
    nologin_statements = _captured_upgrade_statements(NOLOGIN_MIGRATION_FILE)
    guard_sql = _statements_mentioning(nologin_statements, "RAISE EXCEPTION")[0]
    transition_sql = _statements_mentioning(nologin_statements, "ALTER ROLE skincare_billing", "NOLOGIN")[0]

    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            # Simulate "already applied 9815eb266923, LOGIN": drop the
            # real role (safe -- this whole block is rolled back
            # below) and recreate it via that migration's own literal
            # SQL, exactly as it would exist on any deployment that
            # merged PR #2 and has not yet applied 1367b870bdcd.
            await conn.execute("DROP OWNED BY skincare_billing")
            await conn.execute("DROP ROLE skincare_billing")
            await conn.execute(role_creation_sql)

            historical_state = await _role_state(conn)
            assert historical_state["rolcanlogin"] is True
            assert historical_state["rolsuper"] is False
            assert historical_state["rolcreatedb"] is False
            assert historical_state["rolcreaterole"] is False
            assert historical_state["rolbypassrls"] is False

            # 1367b870bdcd's own guard must not raise against a role
            # that does exist -- only against a genuinely missing one.
            await conn.execute(guard_sql)
            await conn.execute(transition_sql)

            final_state = await _role_state(conn)
            assert final_state["rolcanlogin"] is False
            assert final_state["rolsuper"] is False
            assert final_state["rolcreatedb"] is False
            assert final_state["rolcreaterole"] is False
            assert final_state["rolbypassrls"] is False
        finally:
            await tx.rollback()


async def test_nologin_migration_guard_raises_against_a_genuinely_missing_role(db_pool):
    """The impossible-schema-state case: skincare_billing does not
    exist at all when 1367b870bdcd runs. Must fail loudly, never
    silently fabricate a fresh (and therefore un-audited, possibly
    mis-privileged) role to paper over it."""
    nologin_statements = _captured_upgrade_statements(NOLOGIN_MIGRATION_FILE)
    guard_sql = _statements_mentioning(nologin_statements, "RAISE EXCEPTION")[0]

    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("DROP OWNED BY skincare_billing")
            await conn.execute("DROP ROLE skincare_billing")

            with pytest.raises(asyncpg.exceptions.RaiseError):
                await conn.execute(guard_sql)
        finally:
            await tx.rollback()


# ---------------------------------------------------------------------------
# 4: the separately-provisioned runtime login is unaffected.
# ---------------------------------------------------------------------------


async def test_billing_runtime_role_connects_and_inherits_billing_privileges(billing_db_pool, db_pool):
    """skincare_billing losing its own LOGIN capability (this file's
    whole point) must not affect skincare_billing_runtime -- login
    capability and role membership are independent properties in
    Postgres. Connecting through billing_db_pool at all is already
    proof of "can connect"; the membership and privileged-read checks
    below make "inherits skincare_billing's privileges" explicit
    rather than merely implied by every other billing test that
    happens to use this same fixture."""
    current_user = await billing_db_pool.fetchval("SELECT current_user")
    assert current_user == "skincare_billing_runtime"

    is_member = await db_pool.fetchval(
        "SELECT pg_has_role('skincare_billing_runtime', 'skincare_billing', 'MEMBER')"
    )
    assert is_member is True

    # A real read through the runtime login against a table only
    # skincare_billing (not the default PUBLIC) has any grant on --
    # succeeds only because membership actually confers the privilege.
    await billing_db_pool.fetch("SELECT 1 FROM revenuecat_webhook_events LIMIT 0")

    runtime_role = await db_pool.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'skincare_billing_runtime'"
    )
    assert runtime_role is not None
    assert runtime_role["rolsuper"] is False
    assert runtime_role["rolbypassrls"] is False
