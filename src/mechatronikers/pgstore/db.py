"""Connections to the central Postgres store (docs/13, D72).

The DSN comes from `MT_PG_DSN` (a libpq connection string or URI). It usually holds a password, so
it is never logged or printed: errors about it name the variable, and libpq messages pass through
`scrub` first.
"""

import os
import threading
from collections.abc import Hashable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import IsolationLevel
from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

DSN_ENV = "MT_PG_DSN"
DEFAULT_ORG = "org-default"  # the one organisation until OIDC (D73, D82); created by 0001
APPLICATION_NAME = "mechatronikers"

Connection = psycopg.Connection[dict[str, Any]]


class StoreConfigError(RuntimeError):
    """The store is not configured (no DSN)."""


def dsn_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    value = (os.environ if environ is None else environ).get(DSN_ENV, "").strip()
    return value or None


def scrub(message: str, dsn: str | None = None) -> str:
    """`message` without the DSN or its password, for errors that libpq builds from them."""
    dsn = dsn or dsn_from_env()
    if not dsn:
        return message
    secrets = [dsn]
    try:
        password = conninfo_to_dict(dsn).get("password")
    except psycopg.Error:
        password = None
    if password:
        secrets.append(str(password))
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "***")
    return message


def connect(
    dsn: str | None = None,
    *,
    autocommit: bool = True,
    connect_timeout: int = 10,
    **kwargs: Any,
) -> Connection:
    """Open a connection with dict rows.

    Autocommit by default: group statements with `with conn.transaction():`, which commits or
    rolls back as a whole (psycopg 3). Extra keyword arguments go to libpq (e.g. `options`).
    """
    dsn = dsn or dsn_from_env()
    if not dsn:
        raise StoreConfigError(f"no Postgres store configured: set {DSN_ENV}")
    try:
        conninfo_to_dict(dsn)
    except psycopg.ProgrammingError:
        # libpq quotes the token it could not parse, which can be the password
        raise StoreConfigError(
            f"{DSN_ENV} is not a valid connection string (percent-encode special characters "
            "such as % @ : / in the password of a postgresql:// URI)"
        ) from None
    kwargs.setdefault("application_name", APPLICATION_NAME)
    try:
        return psycopg.connect(
            dsn,
            autocommit=autocommit,
            row_factory=dict_row,
            connect_timeout=connect_timeout,
            **kwargs,
        )
    except psycopg.OperationalError as exc:
        raise psycopg.OperationalError(scrub(str(exc), dsn)) from None


class PoolTimeoutError(RuntimeError):
    """No connection of the pool became free in time."""


class Pool:
    """Reusable connections for the repositories, shared by threads (psycopg_pool does the same
    with more knobs; this keeps the `hub` extra at one package).

    - At most `max_size` connections are open. A caller waits up to `timeout` seconds for a free
      one, then gets PoolTimeoutError. One thread inside a review transaction that also reads
      events holds two.
    - An idle connection is checked before it is lent, and replaced if the server ended it
      (restart, failover, idle timeout), so calls after a database restart succeed.
    - Every connection runs READ COMMITTED, whatever the server's or role's default: the locking
      of the repositories needs a new snapshot for each statement after a lock wait. Each also
      has a `lock_timeout` and a `statement_timeout` (seconds; 0 = none), so a lock wait or a
      query fails instead of hanging a request for good.
    - Each `connection()` lends a connection to one caller at a time, so concurrent requests
      never share a transaction. Connections that come back broken, closed or inside a
      transaction are closed instead of kept.

    `pinned()` / `pin()` hold the connection of a transaction that is open on this thread under
    a key (the review repository uses its run), so every repository object of that key on the
    thread joins the transaction instead of waiting for its lock.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        max_size: int = 10,
        timeout: float = 30.0,
        lock_timeout: float = 10.0,
        statement_timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        dsn = dsn or dsn_from_env()
        if not dsn:
            raise StoreConfigError(f"no Postgres store configured: set {DSN_ENV}")
        if max_size < 1:
            raise ValueError("a pool needs at least one connection")
        self._dsn = dsn
        self._kwargs = kwargs
        self._timeout = timeout
        self._settings = {
            # also for statements outside an explicit transaction
            "default_transaction_isolation": "read committed",
            "lock_timeout": f"{round(lock_timeout * 1000)}ms",
            "statement_timeout": f"{round(statement_timeout * 1000)}ms",
        }
        self._slots = threading.BoundedSemaphore(max_size)
        self._idle: list[Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        self._local = threading.local()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        conn = self._take()
        try:
            yield conn
        finally:
            self._give(conn)

    def _connect(self) -> Connection:
        conn = connect(self._dsn, **self._kwargs)
        try:
            conn.isolation_level = IsolationLevel.READ_COMMITTED
            for name, value in self._settings.items():
                conn.execute("SELECT set_config(%s, %s, false)", (name, value))
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _alive(conn: Connection) -> bool:
        if conn.closed or conn.broken:
            return False
        try:
            conn.execute("")  # one round trip: finds a connection the server ended
        except psycopg.Error:
            conn.close()
            return False
        return True

    def _take(self) -> Connection:
        if self._closed:
            raise StoreConfigError("the connection pool is closed")
        if not self._slots.acquire(timeout=self._timeout):
            raise PoolTimeoutError(f"no database connection free within {self._timeout:g} s")
        try:
            while True:
                with self._lock:
                    if self._closed:
                        raise StoreConfigError("the connection pool is closed")
                    conn = self._idle.pop() if self._idle else None
                if conn is None:
                    return self._connect()
                if self._alive(conn):
                    return conn
        except BaseException:
            self._slots.release()
            raise

    def _give(self, conn: Connection) -> None:
        try:
            reusable = (
                not (conn.closed or conn.broken)
                and conn.info.transaction_status == TransactionStatus.IDLE
            )
            with self._lock:
                if reusable and not self._closed:
                    self._idle.append(conn)
                    return
            conn.close()
        finally:
            self._slots.release()

    def pinned(self, key: Hashable) -> Connection | None:
        """The connection of this thread's open transaction under `key`, if any."""
        pins: dict[Hashable, Connection] = getattr(self._local, "pins", {})
        return pins.get(key)

    @contextmanager
    def pin(self, key: Hashable, conn: Connection) -> Iterator[None]:
        pins: dict[Hashable, Connection] = self._local.__dict__.setdefault("pins", {})
        if key in pins:
            raise RuntimeError(f"a transaction for {key!r} is already open on this thread")
        pins[key] = conn
        try:
            yield
        finally:
            del pins[key]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()
