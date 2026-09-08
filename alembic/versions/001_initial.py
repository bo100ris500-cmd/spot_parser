"""create initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-09-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watched_pairs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("coin", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("flag_big", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("flag_cd", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("big_threshold_usd", sa.Float(), nullable=True),
        sa.Column("timestamp_added", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("coin", "exchange", name="uq_watched_coin_exchange"),
    )
    op.create_index("ix_watched_pairs_coin", "watched_pairs", ["coin"])
    op.create_index("ix_watched_pairs_exchange", "watched_pairs", ["exchange"])

    op.create_table(
        "ohlc_candles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pair_id"], ["watched_pairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pair_id", "timeframe", "open_time", name="uq_ohlc_pair_tf_time"),
    )
    op.create_index("ix_ohlc_pair_tf", "ohlc_candles", ["pair_id", "timeframe"])

    op.create_table(
        "delta_state",
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("cum_delta", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["pair_id"], ["watched_pairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pair_id"),
    )

    op.create_table(
        "delta_buckets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False),
        sa.Column("buy_vol", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sell_vol", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notional_sum", sa.Float(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["pair_id"], ["watched_pairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pair_id", "bucket_start", "bucket_seconds", name="uq_bucket"),
    )
    op.create_index("ix_bucket_pair_start", "delta_buckets", ["pair_id", "bucket_start"])

    op.create_table(
        "sent_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pair_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.String(length=2048), nullable=True),
        sa.ForeignKeyConstraint(["pair_id"], ["watched_pairs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pair_id", "alert_type", "fingerprint", name="uq_alert_fp"),
    )
    op.create_index("ix_alert_pair_type_time", "sent_alerts", ["pair_id", "alert_type", "sent_at"])


def downgrade() -> None:
    op.drop_table("sent_alerts")
    op.drop_table("delta_buckets")
    op.drop_table("delta_state")
    op.drop_table("ohlc_candles")
    op.drop_table("watched_pairs")
