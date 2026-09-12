"""enforce catalog evidence immutability and append-only audit at the privilege level

Revision ID: e5277cf2f0ee
Revises: 13c1fff1867e
Create Date: 2026-09-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5277cf2f0ee'
down_revision: Union[str, Sequence[str], None] = '13c1fff1867e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Independent review's second blocker on this branch: migration
    `13c1fff1867e` granted `skincare_catalog_admin` blanket table-wide
    `SELECT, INSERT, UPDATE` on every catalog/staging table, including
    `catalog_import_records`, `catalog_audit_log`, and
    `catalog_formulation_provenance`. `app/db/catalog_admin_repository.py`
    never *chooses* to mutate `raw_payload`/`payload_sha256`/
    `external_record_id`/`batch_id`/`created_at`, never issues an
    `UPDATE` against `catalog_audit_log` or `catalog_formulation_
    provenance` at all -- but that is application convention, not a
    security/integrity boundary. A compromised catalog-admin process,
    or anyone with direct access to that credential, could otherwise
    rewrite durable evidence, provenance, or audit history outright,
    contradicting this pass's own architectural claims (immutable raw
    import, append-only audit log, permanent provenance).

    This migration makes the database itself enforce those claims,
    narrowing (never broadening) `skincare_catalog_admin`'s existing
    privileges:

    - `catalog_import_records`: table-wide `UPDATE` is revoked and
      replaced with a column-level `UPDATE` grant covering only the
      fields the pipeline legitimately mutates after insert (`status`,
      `normalized_payload`, `validation_errors`, `review_reason_codes`,
      `formulation_id`, `superseded_formulation_id`, `updated_at`) --
      matching `app/db/catalog_admin_repository.py::update_import_
      record`'s own field list exactly. `raw_payload`, `payload_sha256`,
      `batch_id`, `external_record_id`, and `created_at` have no
      `UPDATE` grant at all; an attempt to change any of them now fails
      with `InsufficientPrivilegeError` at the database level,
      regardless of what application code does or doesn't choose to
      do. `SELECT`/`INSERT` are unchanged (still table-wide -- there is
      no column-level concern for those here).
    - `catalog_audit_log`: `UPDATE` is revoked entirely. `DELETE` was
      never granted by `13c1fff1867e` in the first place, so this
      migration only needs to close the `UPDATE` gap to make the table
      genuinely append-only -- `SELECT`/`INSERT` are unchanged.
    - `catalog_formulation_provenance`: `UPDATE` is revoked entirely,
      for the same reason -- no field on this table is legitimately
      mutable after insert (a reformulation creates a *new* provenance
      row for the *new* formulation; it never edits an existing one --
      see `app/domain/catalog_publication_service.py`). `DELETE` was,
      again, never granted. `SELECT`/`INSERT` are unchanged.

    See `tests/database/test_catalog_evidence_immutability.py` for the
    real-role proof of every one of these boundaries, both the
    "legitimate staging updates still work" half and the "durable
    evidence/audit/provenance cannot be rewritten" half.
    """
    op.execute("REVOKE UPDATE ON catalog_import_records FROM skincare_catalog_admin")
    op.execute("""
        GRANT UPDATE (
            status, normalized_payload, validation_errors, review_reason_codes,
            formulation_id, superseded_formulation_id, updated_at
        ) ON catalog_import_records TO skincare_catalog_admin
    """)

    op.execute("REVOKE UPDATE ON catalog_audit_log FROM skincare_catalog_admin")
    op.execute("REVOKE UPDATE ON catalog_formulation_provenance FROM skincare_catalog_admin")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("GRANT UPDATE ON catalog_formulation_provenance TO skincare_catalog_admin")
    op.execute("GRANT UPDATE ON catalog_audit_log TO skincare_catalog_admin")

    op.execute("""
        REVOKE UPDATE (
            status, normalized_payload, validation_errors, review_reason_codes,
            formulation_id, superseded_formulation_id, updated_at
        ) ON catalog_import_records FROM skincare_catalog_admin
    """)
    op.execute("GRANT UPDATE ON catalog_import_records TO skincare_catalog_admin")
