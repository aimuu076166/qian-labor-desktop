from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from qian_labor.models.core import Base


class Database:
    def __init__(self, url: str, *, create_schema: bool = False) -> None:
        self.url = url
        self.path: Path | None = None
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        engine_options: dict[str, object] = {
            "connect_args": connect_args,
            "pool_pre_ping": True,
        }
        if url in {"sqlite:///:memory:", "sqlite+pysqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        self.engine = create_engine(url, **engine_options)
        if url.startswith("sqlite"):
            event.listen(
                self.engine,
                "connect",
                lambda connection, _record: connection.execute("PRAGMA foreign_keys=ON"),
            )
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )
        if create_schema:
            Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def create_database(url: str, *, create_schema: bool = False) -> Database:
    return Database(url, create_schema=create_schema)


def create_desktop_database(data_dir: Path) -> Database:
    path = (data_dir / "qian-labor.db").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = create_database(f"sqlite+pysqlite:///{path.as_posix()}", create_schema=True)
    database.path = path
    return database
