import os
import sqlite3
from contextlib import closing

DB_PATH = os.getenv("DB_PATH", "ranked.db")
DEFAULT_RATING = 1000
K = 16  # points per pairwise comparison


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                rating INTEGER NOT NULL DEFAULT 1000,
                games_played INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard_message (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL
            )
            """
        )
        conn.commit()


def get_config(key: str, default: float) -> float:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_config(key: str, value: float):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?
            """,
            (key, value, value),
        )
        conn.commit()


def get_rating(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT rating FROM players WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return row[0]
        conn.execute(
            "INSERT INTO players (user_id, rating, games_played) VALUES (?, ?, 0)",
            (user_id, DEFAULT_RATING),
        )
        conn.commit()
        return DEFAULT_RATING


def apply_rating_changes(deltas: dict[int, int]):
    """deltas: {user_id: rating_change}. Also increments games_played for each."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        for user_id, delta in deltas.items():
            row = conn.execute(
                "SELECT rating FROM players WHERE user_id = ?", (user_id,)
            ).fetchone()
            current = row[0] if row else DEFAULT_RATING
            new_rating = current + delta
            conn.execute(
                """
                INSERT INTO players (user_id, rating, games_played)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    rating = ?,
                    games_played = games_played + 1
                """,
                (user_id, new_rating, new_rating),
            )
        conn.commit()


def get_leaderboard(limit: int = 10) -> list[tuple[int, int, int]]:
    """Returns list of (user_id, rating, games_played) sorted by rating desc."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            "SELECT user_id, rating, games_played FROM players "
            "ORDER BY rating DESC LIMIT ?",
            (limit,),
        ).fetchall()


def set_leaderboard_message(guild_id: int, channel_id: int, message_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO leaderboard_message (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = ?, message_id = ?
            """,
            (guild_id, channel_id, message_id, channel_id, message_id),
        )
        conn.commit()


def get_leaderboard_message(guild_id: int):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute(
            "SELECT channel_id, message_id FROM leaderboard_message WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row  # (channel_id, message_id) or None


def calculate_elo_changes(placements: list[int]) -> list[int]:
    """
    placements: list of user_ids in finishing order (index 0 = 1st place).
    Returns list of rating deltas in the same order, computed by comparing
    every pair: better placement counts as a 1v1 win over worse placement.
    Wins and losses are scaled independently via config (win_multiplier /
    loss_multiplier), so gains and losses don't have to be symmetric.
    """
    win_mult = get_config("win_multiplier", 1.0)
    loss_mult = get_config("loss_multiplier", 1.0)

    ratings = [get_rating(uid) for uid in placements]
    deltas = [0.0] * len(placements)

    for i in range(len(placements)):
        for j in range(len(placements)):
            if i == j:
                continue
            # i finished better than j (lower index = better placement)
            expected_i = 1 / (1 + 10 ** ((ratings[j] - ratings[i]) / 400))
            actual_i = 1 if i < j else 0
            raw = K * (actual_i - expected_i)
            deltas[i] += raw * win_mult if raw >= 0 else raw * loss_mult

    return [round(d) for d in deltas]
