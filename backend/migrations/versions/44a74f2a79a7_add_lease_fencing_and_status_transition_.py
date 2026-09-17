"""add lease fencing (claim_token/processing_claim_token) and a DB-enforced
analysis_requests status-transition guard

Revision ID: 44a74f2a79a7
Revises: e421ed4cf053
Create Date: 2026-09-16 00:00:00.000000

System Integrity Gate V1, sections 1-3. Two independent but related
races this migration closes at the schema level, not just in
application code:

1. Queue lease fencing (`jobs.claim_token`). Before this migration,
   PostgresJobQueue identified the current owner of a claimed job only
   by `id` + `status = 'claimed'`. A worker whose `claimed_until` lease
   expired and was reclaimed by a second worker could still call
   `extend_visibility()`/`acknowledge()`/`fail()` successfully, because
   nothing distinguished "the original claimant" from "whoever claims
   next" -- both satisfy `status = 'claimed'`. `claim_token` is a fresh,
   randomly generated UUID minted atomically by every successful
   claim/reclaim; every mutating statement now also requires the
   caller's token to match the row's *current* token, so a stale
   worker's calls are rejected once a newer claim has installed a new
   one. See app/queue/base.py (JobLeaseLostError) and
   app/queue/postgres_queue.py.

2. Execution-level fencing (`analysis_requests.processing_claim_token`).
   Queue-level fencing alone does not protect a worker that is already
   inside CV compute when its lease expires -- it can still be running
   when a second worker legitimately reclaims the underlying job.
   `processing_claim_token` records which claim token is the *current*
   owner of this request's in-progress execution (installed by
   mark_processing() on every QUEUED->PROCESSING or legitimate
   PROCESSING->PROCESSING re-entry); commit_analysis_result() and
   mark_failed() both require it to match before doing anything
   terminal (persisting a result, releasing quota, deleting the image).
   See app/domain/analysis_execution_service.py
   (AnalysisExecutionLeaseLostError).

3. Status-transition monotonicity. Repository methods previously
   updated `analysis_requests.status` by id with no constraint on the
   row's *previous* status -- nothing but application convention
   stopped a stale write from moving a COMPLETED/FAILED/CANCELLED
   (terminal) request to any other status. This migration adds a
   narrow BEFORE UPDATE trigger enforcing the documented state graph
   (RECEIVED -> QUEUED -> PROCESSING -> COMPLETED, with FAILED/CANCELLED
   reachable from any non-terminal state, and PROCESSING -> PROCESSING
   allowed for a legitimate re-entrant claim) at the database level --
   real enforcement, not just Python discipline. The trigger only fires
   when `status` actually changes (`WHEN (OLD.status IS DISTINCT FROM
   NEW.status)`), so a same-status write (the idempotent terminal
   no-op case) is never evaluated against the graph at all.

Forward-only: this migration does not touch or rewrite
9db3e5856a79/4e5cda3a6bb0/b034483cb876 -- see those migrations'
own docstrings for the columns/policies this one builds on top of.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '44a74f2a79a7'
down_revision: Union[str, Sequence[str], None] = 'e421ed4cf053'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE jobs ADD COLUMN claim_token UUID")
    op.execute("ALTER TABLE analysis_requests ADD COLUMN processing_claim_token UUID")

    op.execute("""
        CREATE FUNCTION enforce_analysis_request_status_transition() RETURNS TRIGGER AS $$
        BEGIN
            -- A terminal row's status may never move again -- this
            -- function only runs at all when status is actually
            -- changing (see the trigger's WHEN clause below), so a
            -- same-status idempotent write on a terminal row never
            -- reaches here.
            IF OLD.status IN ('COMPLETED', 'FAILED', 'CANCELLED') THEN
                RAISE EXCEPTION
                    'analysis_requests %: cannot transition out of terminal status % (attempted -> %)',
                    OLD.id, OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;

            IF NOT (
                (OLD.status = 'RECEIVED' AND NEW.status IN ('QUEUED', 'FAILED', 'CANCELLED'))
                OR (OLD.status = 'QUEUED' AND NEW.status IN ('PROCESSING', 'FAILED', 'CANCELLED'))
                OR (OLD.status = 'PROCESSING' AND NEW.status IN ('COMPLETED', 'FAILED', 'CANCELLED'))
            ) THEN
                RAISE EXCEPTION
                    'analysis_requests %: invalid status transition % -> %',
                    OLD.id, OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    op.execute("""
        CREATE TRIGGER analysis_requests_status_transition_guard
            BEFORE UPDATE ON analysis_requests
            FOR EACH ROW
            WHEN (OLD.status IS DISTINCT FROM NEW.status)
            EXECUTE FUNCTION enforce_analysis_request_status_transition()
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS analysis_requests_status_transition_guard ON analysis_requests")
    op.execute("DROP FUNCTION IF EXISTS enforce_analysis_request_status_transition()")
    op.execute("ALTER TABLE analysis_requests DROP COLUMN IF EXISTS processing_claim_token")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS claim_token")
