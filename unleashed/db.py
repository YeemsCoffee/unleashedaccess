"""Database access: a local SQLite file, or a shared PostgreSQL server via DATABASE_URL.

SQL is written once with `?` placeholders and portable syntax (ON CONFLICT
upserts, RETURNING); this module adapts placeholders and the few schema types
that differ, and returns rows as dicts on both.
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from urllib.parse import quote


def is_postgres_url(url):
    return bool(url) and url.split(":", 1)[0] in ("postgres", "postgresql")


def database_url_from_env(env):
    """DATABASE_URL, or a PostgreSQL URL assembled from AWS-style settings.

    Checked in order: DATABASE_URL; DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD;
    Elastic Beanstalk's RDS_HOSTNAME/RDS_PORT/RDS_DB_NAME/RDS_USERNAME/RDS_PASSWORD;
    and DB_SECRET, the JSON of an RDS secret from Secrets Manager (username,
    password, and optionally host, port, dbname). Separate settings avoid having to
    URL-encode generated passwords, which often contain symbols.
    """
    if env.get("DATABASE_URL", "").strip():
        return env["DATABASE_URL"].strip()
    secret = {}
    if env.get("DB_SECRET", "").strip():
        try:
            secret = json.loads(env["DB_SECRET"])
        except ValueError as e:
            raise ValueError("DB_SECRET must be the secret's JSON text") from e

    def pick(names, secret_key, default=None):
        for name in names:
            if env.get(name, "").strip():
                return env[name].strip()
        return secret.get(secret_key) or default

    host = pick(("DB_HOST", "RDS_HOSTNAME"), "host")
    if not host:
        return ""
    user = pick(("DB_USER", "RDS_USERNAME"), "username", "postgres")
    password = pick(("DB_PASSWORD", "RDS_PASSWORD"), "password", "")
    port = pick(("DB_PORT", "RDS_PORT"), "port", 5432)
    name = pick(("DB_NAME", "RDS_DB_NAME"), "dbname", "postgres")
    sslmode = env.get("DB_SSLMODE", "").strip() or "require"  # RDS expects encrypted connections
    return (f"postgresql://{quote(str(user), safe='')}:{quote(str(password), safe='')}"
            f"@{host}:{port}/{quote(str(name), safe='')}?sslmode={sslmode}")


class Database:
    def __init__(self, url):
        """`url`: postgresql://... for PostgreSQL; a file path or sqlite:///path for SQLite."""
        self.url = url
        self.dialect = "postgres" if is_postgres_url(url) else "sqlite"
        self._pool = None
        if self.dialect == "postgres":
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            # One direct attempt first: the pool retries quietly and would only report
            # "couldn't get a connection", hiding a wrong password, database name or host.
            with psycopg.connect(_postgres_url(url), connect_timeout=15):
                pass
            pool_args = {}
            if hasattr(ConnectionPool, "check_connection"):
                pool_args["check"] = ConnectionPool.check_connection  # drop dead idle connections
            self._pool = ConnectionPool(
                _postgres_url(url), min_size=1, max_size=10, open=True,
                # prepare_threshold=None: poolers like PgBouncer/Supavisor reject prepared statements
                kwargs={"row_factory": dict_row, "prepare_threshold": None,
                        "client_encoding": "utf8"}, **pool_args)
        else:
            self.path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url

    @property
    def shared(self):
        """True for a database other machines may also be using."""
        return self.dialect == "postgres"

    @property
    def label(self):
        if self.dialect == "sqlite":
            return f"SQLite file {os.path.basename(self.path)} (this computer only)"
        host = re.sub(r"^[^@]*@", "", self.url.split("://", 1)[1]).split("/", 1)[0]
        return f"PostgreSQL at {host} (shared)"

    @contextmanager
    def connect(self):
        """A connection that commits on success and rolls back on error."""
        if self._pool is not None:
            with self._pool.connection() as raw:
                yield _Connection(raw, "postgres")
            return
        raw = sqlite3.connect(self.path, timeout=30)
        raw.row_factory = sqlite3.Row
        try:
            yield _Connection(raw, "sqlite")
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def query(self, sql, args=()):
        with self.connect() as conn:
            return conn.query(sql, args)

    def execute(self, sql, args=()):
        with self.connect() as conn:
            return conn.execute(sql, args)

    def executemany(self, sql, rows):
        with self.connect() as conn:
            conn.executemany(sql, rows)

    def init_schema(self, path):
        with open(path, encoding="utf-8") as f:
            script = f.read()
        statements = [s.strip() for s in re.sub(r"--[^\n]*", "", script).split(";") if s.strip()]
        with self.connect() as conn:
            if self.dialect == "sqlite":
                conn.raw.execute("PRAGMA journal_mode=WAL")  # pages read while the sync writes
            for statement in statements:
                if self.dialect == "postgres":
                    statement = statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                                                  "BIGSERIAL PRIMARY KEY")
                conn.raw.execute(statement)

    def require_schema(self):
        """For view-only instances, which may connect with a read-only login."""
        try:
            self.query("SELECT COUNT(*) AS n FROM ul_sync_runs")
        except Exception as e:
            raise RuntimeError(
                "The shared database has no Unleashed Access tables yet. Start the instance that "
                "has the Unleashed API key first (it creates them), then start this one.") from e

    def close(self):
        if self._pool is not None:
            self._pool.close()


class _Connection:
    def __init__(self, raw, dialect):
        self.raw = raw
        self.dialect = dialect

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql, args=()):
        """Run a statement; returns the number of rows it changed."""
        return self.raw.execute(self._sql(sql), tuple(args)).rowcount

    def executemany(self, sql, rows):
        rows = [tuple(r) for r in rows]
        if rows:
            self.raw.cursor().executemany(self._sql(sql), rows)

    def query(self, sql, args=()):
        return [dict(r) for r in self.raw.execute(self._sql(sql), tuple(args)).fetchall()]

    def query_one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None


def _postgres_url(url):
    # Heroku-style "postgres://" is accepted by libpq, but normalise for clarity.
    return "postgresql://" + url.split("://", 1)[1]
