"""Persistence layer: the SignalRepository protocol and its Postgres backend."""

from storage.postgres import PostgresSignalRepository, psycopg_dsn
from storage.repository import MARKET_GROUP, ScoreRecord, SignalRepository

__all__ = [
    "MARKET_GROUP",
    "PostgresSignalRepository",
    "ScoreRecord",
    "SignalRepository",
    "psycopg_dsn",
]
