"""Phase 17: real PostgreSQL-level cross-user isolation tests, run
against the actual restricted skincare_app role (app_db_pool), not
mocked and not run as the superuser. Covers user_profiles and
consent_events -- the two tables RLS was first enabled on (see
migration 7b38b717546e) -- plus, further down, users and
refresh_tokens (P0-1, migration feb038fd05bd), each proven against
their own genuinely different policy mechanism rather than the plain
user_id-equality pattern the first four tests cover.
"""
import uuid

import pytest

from app.security.tokens import generate_refresh_token


async def _create_user(db_pool, email):
    row = await db_pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING id",
        email,
    )
    return row["id"]


async def test_user_a_cannot_read_user_b_profile(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-a@test.com")
    user_b = await _create_user(db_pool, "rls-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    b_profile = dict(DEFAULT_PROFILE)
    b_profile["experience_level"] = "advanced"
    await upsert_profile(app_db_pool, user_b, b_profile)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            # Explicitly asking for B's row by ID, as A -- RLS must
            # still return nothing, regardless of what the WHERE
            # clause itself says.
            row = await conn.fetchrow("SELECT * FROM user_profiles WHERE user_id = $1", user_b)

    assert row is None


async def test_user_a_cannot_update_user_b_profile(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-upd-a@test.com")
    user_b = await _create_user(db_pool, "rls-upd-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    await upsert_profile(app_db_pool, user_b, dict(DEFAULT_PROFILE))

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                "UPDATE user_profiles SET has_sensitive_skin = true WHERE user_id = $1", user_b
            )

    assert result.endswith(" 0")  # zero rows affected

    b_row = await db_pool.fetchrow("SELECT has_sensitive_skin FROM user_profiles WHERE user_id = $1", user_b)
    assert b_row["has_sensitive_skin"] is False  # unchanged


async def test_user_a_cannot_delete_user_b_consent_event(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-del-a@test.com")
    user_b = await _create_user(db_pool, "rls-del-b@test.com")

    from app.db.consent_repository import record_consent

    await record_consent(app_db_pool, user_b, "facial_analysis", "1.0", "testing")

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute("DELETE FROM consent_events WHERE user_id = $1", user_b)

    assert result.endswith(" 0")

    b_count = await db_pool.fetchval("SELECT count(*) FROM consent_events WHERE user_id = $1", user_b)
    assert b_count == 1  # B's row survived A's delete attempt


async def test_pooled_connection_reuse_does_not_leak_context_between_users(db_pool, app_db_pool):
    """The core promise of using SET LOCAL (via set_config's third
    arg): context is scoped to one transaction, not the underlying
    connection. Proven here on a single, explicitly reused connection
    -- not two different pool.acquire() calls that might already land
    on different physical connections."""
    user_a = await _create_user(db_pool, "rls-reuse-a@test.com")
    user_b = await _create_user(db_pool, "rls-reuse-b@test.com")

    from app.db.profile_repository import upsert_profile, DEFAULT_PROFILE

    a_profile = dict(DEFAULT_PROFILE)
    a_profile["experience_level"] = "advanced"
    await upsert_profile(app_db_pool, user_a, a_profile)

    async with app_db_pool.acquire() as conn:
        # Transaction 1: context set to A, A's row correctly visible.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            row = await conn.fetchrow("SELECT experience_level FROM user_profiles WHERE user_id = $1", user_a)
            assert row["experience_level"] == "advanced"

        # Transaction 2: same physical connection, no new context set.
        # If SET LOCAL's scoping didn't work, this might still see A's
        # context (real leak). It must instead see nothing at all --
        # current_setting() returns NULL post-commit, which the RLS
        # policy correctly treats as "no identity", not "still A".
        async with conn.transaction():
            leaked = await conn.fetchrow(
                "SELECT experience_level FROM user_profiles WHERE user_id = $1", user_a
            )
            assert leaked is None

        # Transaction 3: now correctly scoped to B on the SAME reused
        # connection -- proves the connection itself isn't broken,
        # just that context doesn't persist across transactions.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_b))
            b_row = await conn.fetchrow("SELECT user_id FROM user_profiles WHERE user_id = $1", user_a)
            assert b_row is None  # B's context still can't see A's row


# --- users / refresh_tokens (P0-1, migration feb038fd05bd) ----------------
#
# Neither table fits the plain `user_id = current_setting(...)` pattern
# above -- both have a real pre-authentication lookup with no session
# identity yet. These tests prove the two different mechanisms each
# table actually got: a SECURITY DEFINER function for the users/email
# lookup (email is caller-supplied, not a secret, so it can't be a
# session-context key), and a second GUC keyed on the token_hash itself
# for refresh_tokens (a token_hash *is* an unguessable bearer secret,
# so proof of possession of it is a valid access boundary).


async def _create_refresh_token(db_pool, user_id, *, family_id=None, token_hash=None):
    family_id = family_id or uuid.uuid4()
    if token_hash is None:
        _, token_hash = generate_refresh_token()
    row = await db_pool.fetchrow(
        """
        INSERT INTO refresh_tokens (user_id, family_id, token_hash, expires_at)
        VALUES ($1, $2, $3, now() + interval '1 day')
        RETURNING family_id, token_hash
        """,
        user_id, family_id, token_hash,
    )
    return row["family_id"], row["token_hash"]


async def test_user_a_cannot_read_user_b_row_by_id(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-users-a@test.com")
    user_b = await _create_user(db_pool, "rls-users-b@test.com")

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_b)

    assert row is None


async def test_user_a_cannot_disable_user_b_account(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-disable-a@test.com")
    user_b = await _create_user(db_pool, "rls-disable-b@test.com")

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                "UPDATE users SET is_active = false, deleted_at = now() WHERE id = $1", user_b
            )

    assert result.endswith(" 0")

    b_row = await db_pool.fetchrow("SELECT is_active, deleted_at FROM users WHERE id = $1", user_b)
    assert b_row["is_active"] is True
    assert b_row["deleted_at"] is None


async def test_direct_select_by_email_is_blocked_pre_auth(db_pool, app_db_pool):
    """Without the SECURITY DEFINER function, a plain SELECT by email --
    the shape /login used before migration feb038fd05bd -- must return
    nothing: no app.current_user_id exists yet at that point in the
    real flow, and email isn't a valid session-context key (the caller
    controls it), so it must not become one."""
    await _create_user(db_pool, "rls-directselect@test.com")

    async with app_db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", "rls-directselect@test.com"
        )

    assert row is None


async def test_login_lookup_function_bypasses_rls_for_exact_email(db_pool, app_db_pool):
    """The one sanctioned pre-auth path: login_lookup_by_email runs as
    its (superuser) owner, so it works with no session context set at
    all -- proving /login can still find its own row through the
    function even though the direct SELECT above cannot."""
    user_id = await _create_user(db_pool, "rls-lookupfn@test.com")
    await db_pool.execute("UPDATE users SET password_hash = 'realhash' WHERE id = $1", user_id)

    async with app_db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM login_lookup_by_email($1)", "rls-lookupfn@test.com"
        )

    assert row is not None
    assert row["id"] == user_id
    assert row["password_hash"] == "realhash"


async def test_login_lookup_function_returns_nothing_for_unknown_email(app_db_pool):
    async with app_db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM login_lookup_by_email($1)", "no-such-user@test.com"
        )
    assert row is None


async def test_user_a_cannot_read_user_b_refresh_token_by_user_id(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-rt-a@test.com")
    user_b = await _create_user(db_pool, "rls-rt-b@test.com")
    await _create_refresh_token(db_pool, user_b)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            row = await conn.fetchrow("SELECT id FROM refresh_tokens WHERE user_id = $1", user_b)

    assert row is None


async def test_user_a_cannot_revoke_user_b_refresh_tokens(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-revoke-a@test.com")
    user_b = await _create_user(db_pool, "rls-revoke-b@test.com")
    await _create_refresh_token(db_pool, user_b)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(user_a))
            result = await conn.execute(
                "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_b
            )

    assert result.endswith(" 0")

    b_row = await db_pool.fetchrow("SELECT revoked_at FROM refresh_tokens WHERE user_id = $1", user_b)
    assert b_row["revoked_at"] is None


async def test_possessing_a_raw_token_hash_reveals_only_that_exact_row(db_pool, app_db_pool):
    """Proof of possession of a refresh token's hash is a valid access
    boundary on its own, without app.current_user_id ever being set --
    this is exactly the pre-identity shape /refresh and /logout use."""
    user_a = await _create_user(db_pool, "rls-hash-a@test.com")
    family_id, token_hash = await _create_refresh_token(db_pool, user_a)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", token_hash)
            row = await conn.fetchrow("SELECT user_id FROM refresh_tokens WHERE token_hash = $1", token_hash)

    assert row is not None
    assert row["user_id"] == user_a


async def test_token_hash_context_does_not_reveal_a_different_hash(db_pool, app_db_pool):
    user_a = await _create_user(db_pool, "rls-hash-wrong-a@test.com")
    _, real_hash = await _create_refresh_token(db_pool, user_a)
    _, unrelated_hash = generate_refresh_token()

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            # Context set to a hash that was never issued -- proves the
            # policy checks the *value*, not merely "is something set".
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", unrelated_hash)
            row = await conn.fetchrow("SELECT user_id FROM refresh_tokens WHERE token_hash = $1", real_hash)

    assert row is None


async def test_token_hash_context_does_not_reveal_other_rows_in_same_family(db_pool, app_db_pool):
    """Possessing one rotated-out token's hash must not, by itself,
    expose the rest of that family's rows -- only the exact row whose
    hash was presented. Family-wide visibility requires the user_id
    branch (proven separately once user_id is known), not the hash
    branch alone."""
    user_a = await _create_user(db_pool, "rls-hash-family-a@test.com")
    family_id, old_hash = await _create_refresh_token(db_pool, user_a)
    _, new_hash = await _create_refresh_token(db_pool, user_a, family_id=family_id)

    async with app_db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_token_hash', $1, true)", old_hash)
            row = await conn.fetchrow("SELECT id FROM refresh_tokens WHERE token_hash = $1", new_hash)
            family_rows = await conn.fetch("SELECT id FROM refresh_tokens WHERE family_id = $1", family_id)

    assert row is None
    assert len(family_rows) == 1  # only the row matching old_hash is visible
