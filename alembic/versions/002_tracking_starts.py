"""add tracking start timestamps

Revision ID: 002_tracking_starts
Revises: 001_initial
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_tracking_starts"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("watched_pairs", sa.Column("big_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("watched_pairs", sa.Column("cd_started_at", sa.DateTime(timezone=True), nullable=True))
    # Backfill from timestamp_added where flags already set
    op.execute(
        """
        UPDATE watched_pairs
        SET big_started_at = timestamp_added
        WHERE flag_big = true AND big_started_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE watched_pairs
        SET cd_started_at = timestamp_added
        WHERE flag_cd = true AND cd_started_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("watched_pairs", "cd_started_at")
    op.drop_column("watched_pairs", "big_started_at")
