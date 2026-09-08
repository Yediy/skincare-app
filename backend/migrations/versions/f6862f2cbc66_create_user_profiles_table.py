"""create user_profiles table

Revision ID: f6862f2cbc66
Revises: e5f9be879f83
Create Date: 2026-09-08 01:15:24.577221

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6862f2cbc66'
down_revision: Union[str, Sequence[str], None] = 'e5f9be879f83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE user_profiles (
            user_id UUID PRIMARY KEY REFERENCES users(id),
            has_sensitive_skin BOOLEAN NOT NULL DEFAULT false,
            experience_level VARCHAR(20) NOT NULL DEFAULT 'beginner',
            max_routine_steps INTEGER NOT NULL DEFAULT 10,
            is_pregnant BOOLEAN NOT NULL DEFAULT false,
            is_nursing BOOLEAN NOT NULL DEFAULT false,
            -- Plain text arrays for now, deliberately not normalized --
            -- this is a placeholder representation until a real
            -- ingredient/allergen entity model exists (tracked as a
            -- known follow-up, not silently treated as final).
            allergies TEXT[] NOT NULL DEFAULT '{}',
            avoid_ingredients TEXT[] NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE user_profiles")
