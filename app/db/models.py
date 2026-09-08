from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class WatchedPair(Base):
    __tablename__ = "watched_pairs"
    __table_args__ = (UniqueConstraint("coin", "exchange", name="uq_watched_coin_exchange"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. BTC/USDT
    flag_big: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag_cd: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    big_threshold_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp_added: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=utcnow
    )

    delta_state: Mapped[DeltaState | None] = relationship(back_populates="pair", uselist=False)
    candles: Mapped[list[OhlcCandle]] = relationship(back_populates="pair")
    buckets: Mapped[list[DeltaBucket]] = relationship(back_populates="pair")


class OhlcCandle(Base):
    __tablename__ = "ohlc_candles"
    __table_args__ = (
        UniqueConstraint("pair_id", "timeframe", "open_time", name="uq_ohlc_pair_tf_time"),
        Index("ix_ohlc_pair_tf", "pair_id", "timeframe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("watched_pairs.id", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pair: Mapped[WatchedPair] = relationship(back_populates="candles")


class DeltaState(Base):
    __tablename__ = "delta_state"

    pair_id: Mapped[int] = mapped_column(
        ForeignKey("watched_pairs.id", ondelete="CASCADE"), primary_key=True
    )
    cum_delta: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    pair: Mapped[WatchedPair] = relationship(back_populates="delta_state")


class DeltaBucket(Base):
    __tablename__ = "delta_buckets"
    __table_args__ = (
        UniqueConstraint("pair_id", "bucket_start", "bucket_seconds", name="uq_bucket"),
        Index("ix_bucket_pair_start", "pair_id", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("watched_pairs.id", ondelete="CASCADE"))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    buy_vol: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sell_vol: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notional_sum: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pair: Mapped[WatchedPair] = relationship(back_populates="buckets")


class SentAlert(Base):
    __tablename__ = "sent_alerts"
    __table_args__ = (
        UniqueConstraint("pair_id", "alert_type", "fingerprint", name="uq_alert_fp"),
        Index("ix_alert_pair_type_time", "pair_id", "alert_type", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("watched_pairs.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    payload: Mapped[str | None] = mapped_column(String(2048), nullable=True)
