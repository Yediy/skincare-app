"""add cleanup functions for overdue ephemeral images

Revision ID: 071fab81f0ac
Revises: b034483cb876
Create Date: 2026-09-10 00:00:07.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '071fab81f0ac'
down_revision: Union[str, Sequence[str], None] = 'b034483cb876'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Part IV, Phase 22's safety-net cleanup mechanism needs to find
    overdue ephemeral images *across every user*, but analysis_requests
    has row-level security scoping every ordinary query to one
    app.current_user_id -- a legitimate cross-cutting operational need,
    same shape as migration feb038fd05bd's login_lookup_by_email:
    narrow, single-purpose SECURITY DEFINER functions the restricted
    skincare_app role can call, rather than granting it a general RLS
    bypass (which would let a compromised API request read every
    user's analysis_requests, not just run this one sweep).

    find_overdue_ephemeral_images(): returns up to `batch_limit` rows
    whose image_expires_at has passed and whose image_object_key is
    still set (i.e. not yet cleaned up). clear_image_reference(): nulls
    out image_object_key/image_expires_at for one request once its
    object has actually been deleted from storage -- called only after
    a real, confirmed deletion, never before (see
    app/workers/image_cleanup.py).
    """
    op.execute("""
        CREATE FUNCTION find_overdue_ephemeral_images(batch_limit INT)
        RETURNS TABLE(id UUID, user_id UUID, image_object_key TEXT)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id, user_id, image_object_key
            FROM analysis_requests
            WHERE image_object_key IS NOT NULL AND image_expires_at < now()
            ORDER BY image_expires_at
            LIMIT batch_limit
        $$
    """)
    op.execute("""
        CREATE FUNCTION clear_image_reference(target_id UUID)
        RETURNS VOID
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            UPDATE analysis_requests SET image_object_key = NULL, image_expires_at = NULL WHERE id = target_id
        $$
    """)
    op.execute("GRANT EXECUTE ON FUNCTION find_overdue_ephemeral_images(INT) TO skincare_app")
    op.execute("GRANT EXECUTE ON FUNCTION clear_image_reference(UUID) TO skincare_app")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("REVOKE EXECUTE ON FUNCTION clear_image_reference(UUID) FROM skincare_app")
    op.execute("REVOKE EXECUTE ON FUNCTION find_overdue_ephemeral_images(INT) FROM skincare_app")
    op.execute("DROP FUNCTION IF EXISTS clear_image_reference(UUID)")
    op.execute("DROP FUNCTION IF EXISTS find_overdue_ephemeral_images(INT)")
