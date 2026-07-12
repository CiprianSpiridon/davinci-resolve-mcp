"""
davinci_resolve_mcp.store.db — SQLite "DB-as-truth" store + schema/migration.

This module owns the single on-disk persistence provider for the offline /
advanced tooling. It uses only the Python standard library (:mod:`sqlite3`) so
it has no native-extension dependency, and it never connects to DaVinci
Resolve — it operates purely on a local database file.

The database holds the resolved entity tree plus pipeline history:

    * ``projects``     — top-level project rows (name, resolve ref, metadata)
    * ``clips``        — media/timeline clips belonging to a project
    * ``grades``       — colour grades attached to a clip (CDL/DRX payloads)
    * ``runs``         — pipeline run history for a project
    * ``stages``       — per-run ordered stage results (a run's DAG execution)
    * ``provenance``   — append-only lineage events (what produced/changed what)

Design notes
------------
* The schema version is tracked with ``PRAGMA user_version``. :func:`open_db`
  creates the schema on first use and is idempotent on re-open: migrating an
  already-current database is a no-op.
* All value round-trips are typed: JSON-typed columns (``*_json``) are
  serialised/deserialised transparently, and integers/reals/text keep their
  SQLite affinity.
* Every failure surfaces as :class:`StoreError` — a UNIQUE / constraint
  violation, a corrupt or locked database, or a malformed row are all wrapped
  so callers never see a raw :class:`sqlite3.Error`.

The module is deliberately import-light and holds no module-level Resolve
imports; it is safe to import with no Resolve instance running.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "StoreError",
    "Store",
    "open_db",
    "SCHEMA_VERSION",
]

# Current on-disk schema version. Bump when adding a migration step below.
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StoreError(Exception):
    """Raised for any store-level failure.

    Wraps the underlying :class:`sqlite3.Error` (constraint violations,
    corruption, lock contention, malformed JSON, …) so callers of this module
    only ever have to catch one exception type.
    """


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Ordered DDL applied when creating (or migrating up to) SCHEMA_VERSION. Each
# statement is CREATE ... IF NOT EXISTS so applying it against an already-current
# database is a harmless no-op.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id   TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        resolve_ref  TEXT,
        meta_json    TEXT,
        created_at   TEXT,
        updated_at   TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name ON projects(name)",
    """
    CREATE TABLE IF NOT EXISTS clips (
        clip_id      TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        name         TEXT,
        source_path  TEXT,
        resolve_ref  TEXT,
        frame_start  INTEGER,
        frame_end    INTEGER,
        meta_json    TEXT,
        created_at   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clips_project ON clips(project_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_project_source ON clips(project_id, source_path)",
    """
    CREATE TABLE IF NOT EXISTS grades (
        grade_id      TEXT PRIMARY KEY,
        clip_id       TEXT NOT NULL REFERENCES clips(clip_id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        payload_json  TEXT,
        content_hash  TEXT,
        verified      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_grades_clip ON grades(clip_id)",
    "CREATE INDEX IF NOT EXISTS idx_grades_hash ON grades(content_hash)",
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id      TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        profile     TEXT,
        status      TEXT,
        spec_hash   TEXT,
        created_at  TEXT,
        updated_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id)",
    """
    CREATE TABLE IF NOT EXISTS stages (
        run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        stage_index  INTEGER NOT NULL,
        stage        TEXT NOT NULL,
        gate         TEXT,
        status       TEXT,
        tool         TEXT,
        config_hash  TEXT,
        result_json  TEXT,
        ran_at       TEXT,
        PRIMARY KEY (run_id, stage_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provenance (
        event_id     TEXT PRIMARY KEY,
        run_id       TEXT,
        project_id   TEXT,
        stage        TEXT,
        tool         TEXT,
        config_hash  TEXT,
        actor        TEXT,
        result       TEXT,
        target       TEXT,
        at           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prov_project ON provenance(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_prov_run ON provenance(run_id)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC timestamp (ISO 8601, seconds) used when a caller passes no ``now``."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dumps(value: Any) -> Optional[str]:
    """JSON-encode ``value`` for a ``*_json`` column (``None`` stays ``NULL``)."""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"could not serialise JSON value: {exc}") from exc


def _loads(text: Optional[str]) -> Any:
    """Decode a ``*_json`` column back to a Python object (``NULL`` → ``None``)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"corrupt JSON column value: {exc}") from exc


def _row_to_dict(row: Optional[sqlite3.Row], json_cols: Sequence[str] = ()) -> Optional[dict]:
    """Convert a :class:`sqlite3.Row` to a plain dict, hydrating JSON columns."""
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()}
    for col in json_cols:
        if col in data:
            data[col] = _loads(data[col])
    return data


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """A typed, migration-aware handle over one on-disk SQLite database.

    Construct via :func:`open_db`, not directly. The object holds an open
    :class:`sqlite3.Connection` (``conn``) and remembers the file it was opened
    from (``path``). All public methods translate any underlying
    :class:`sqlite3.Error` into :class:`StoreError`.
    """

    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self.conn = conn
        self.path = path

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """The database's current ``PRAGMA user_version``."""
        try:
            cur = self.conn.execute("PRAGMA user_version")
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            raise StoreError(f"could not read schema version: {exc}") from exc

    # -- low-level exec ----------------------------------------------------

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise StoreError(f"constraint violation: {exc}") from exc
        except sqlite3.Error as exc:
            raise StoreError(f"database error: {exc}") from exc

    def _commit(self) -> None:
        try:
            self.conn.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"commit failed: {exc}") from exc

    # -- projects ----------------------------------------------------------

    def upsert_project(
        self,
        project_id: str,
        name: str,
        resolve_ref: Optional[str] = None,
        meta: Any = None,
        now: Optional[str] = None,
    ) -> dict:
        """Insert or update a project row (keyed on ``project_id``)."""
        if not project_id:
            raise StoreError("project_id is required")
        if not name:
            raise StoreError("project name is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO projects (project_id, name, resolve_ref, meta_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                name        = excluded.name,
                resolve_ref = excluded.resolve_ref,
                meta_json   = excluded.meta_json,
                updated_at  = excluded.updated_at
            """,
            (project_id, name, resolve_ref, _dumps(meta), ts, ts),
        )
        self._commit()
        return self.get_project(project_id)  # type: ignore[return-value]

    def get_project(self, project_id: str) -> Optional[dict]:
        """Return a project row (JSON-hydrated) or ``None``."""
        cur = self._execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
        return _row_to_dict(cur.fetchone(), ("meta_json",))

    def list_projects(self) -> list[dict]:
        """Return all projects ordered by name."""
        cur = self._execute("SELECT * FROM projects ORDER BY name")
        return [_row_to_dict(r, ("meta_json",)) for r in cur.fetchall()]  # type: ignore[misc]

    # -- clips -------------------------------------------------------------

    def upsert_clip(
        self,
        clip_id: str,
        project_id: str,
        name: Optional[str] = None,
        source_path: Optional[str] = None,
        resolve_ref: Optional[str] = None,
        frame_start: Optional[int] = None,
        frame_end: Optional[int] = None,
        meta: Any = None,
        now: Optional[str] = None,
    ) -> dict:
        """Insert or update a clip row (keyed on ``clip_id``).

        Raises :class:`StoreError` on a UNIQUE ``(project_id, source_path)``
        collision or an unknown ``project_id`` foreign key.
        """
        if not clip_id:
            raise StoreError("clip_id is required")
        if not project_id:
            raise StoreError("project_id is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO clips
                (clip_id, project_id, name, source_path, resolve_ref,
                 frame_start, frame_end, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(clip_id) DO UPDATE SET
                project_id  = excluded.project_id,
                name        = excluded.name,
                source_path = excluded.source_path,
                resolve_ref = excluded.resolve_ref,
                frame_start = excluded.frame_start,
                frame_end   = excluded.frame_end,
                meta_json   = excluded.meta_json
            """,
            (
                clip_id,
                project_id,
                name,
                source_path,
                resolve_ref,
                frame_start,
                frame_end,
                _dumps(meta),
                ts,
            ),
        )
        self._commit()
        return self.get_clip(clip_id)  # type: ignore[return-value]

    def get_clip(self, clip_id: str) -> Optional[dict]:
        """Return a clip row (JSON-hydrated) or ``None``."""
        cur = self._execute("SELECT * FROM clips WHERE clip_id = ?", (clip_id,))
        return _row_to_dict(cur.fetchone(), ("meta_json",))

    def list_clips(self, project_id: Optional[str] = None) -> list[dict]:
        """Return clips, optionally filtered to one project, ordered by id."""
        if project_id is None:
            cur = self._execute("SELECT * FROM clips ORDER BY project_id, clip_id")
        else:
            cur = self._execute(
                "SELECT * FROM clips WHERE project_id = ? ORDER BY clip_id",
                (project_id,),
            )
        return [_row_to_dict(r, ("meta_json",)) for r in cur.fetchall()]  # type: ignore[misc]

    # -- grades ------------------------------------------------------------

    def upsert_grade(
        self,
        grade_id: str,
        clip_id: str,
        kind: str,
        payload: Any = None,
        content_hash: Optional[str] = None,
        verified: bool = False,
        now: Optional[str] = None,
    ) -> dict:
        """Insert or update a grade row (keyed on ``grade_id``)."""
        if not grade_id:
            raise StoreError("grade_id is required")
        if not clip_id:
            raise StoreError("clip_id is required")
        if not kind:
            raise StoreError("grade kind is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO grades
                (grade_id, clip_id, kind, payload_json, content_hash, verified, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(grade_id) DO UPDATE SET
                clip_id      = excluded.clip_id,
                kind         = excluded.kind,
                payload_json = excluded.payload_json,
                content_hash = excluded.content_hash,
                verified     = excluded.verified
            """,
            (grade_id, clip_id, kind, _dumps(payload), content_hash, 1 if verified else 0, ts),
        )
        self._commit()
        return self.get_grade(grade_id)  # type: ignore[return-value]

    def get_grade(self, grade_id: str) -> Optional[dict]:
        """Return a grade row (JSON-hydrated, ``verified`` as bool) or ``None``."""
        cur = self._execute("SELECT * FROM grades WHERE grade_id = ?", (grade_id,))
        row = _row_to_dict(cur.fetchone(), ("payload_json",))
        if row is not None:
            row["verified"] = bool(row.get("verified"))
        return row

    def list_grades(self, clip_id: str) -> list[dict]:
        """Return grades for a clip, oldest first."""
        cur = self._execute(
            "SELECT * FROM grades WHERE clip_id = ? ORDER BY created_at, grade_id",
            (clip_id,),
        )
        out = []
        for r in cur.fetchall():
            row = _row_to_dict(r, ("payload_json",))
            row["verified"] = bool(row.get("verified"))  # type: ignore[union-attr]
            out.append(row)
        return out

    # -- runs --------------------------------------------------------------

    def upsert_run(
        self,
        run_id: str,
        project_id: str,
        profile: Optional[str] = None,
        status: str = "planned",
        spec_hash: Optional[str] = None,
        now: Optional[str] = None,
    ) -> dict:
        """Insert or update a pipeline run (keyed on ``run_id``)."""
        if not run_id:
            raise StoreError("run_id is required")
        if not project_id:
            raise StoreError("project_id is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO runs (run_id, project_id, profile, status, spec_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                project_id = excluded.project_id,
                profile    = excluded.profile,
                status     = excluded.status,
                spec_hash  = excluded.spec_hash,
                updated_at = excluded.updated_at
            """,
            (run_id, project_id, profile, status, spec_hash, ts, ts),
        )
        self._commit()
        return self.get_run(run_id)  # type: ignore[return-value]

    def set_run_status(self, run_id: str, status: str, now: Optional[str] = None) -> None:
        """Update a run's status and ``updated_at`` timestamp."""
        self._execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, now or _now_iso(), run_id),
        )
        self._commit()

    def get_run(self, run_id: str, with_stages: bool = True) -> Optional[dict]:
        """Return a run row, with its ordered ``stages`` attached by default."""
        cur = self._execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        run = _row_to_dict(cur.fetchone())
        if run is None:
            return None
        if with_stages:
            run["stages"] = self.list_stages(run_id)
        return run

    def list_runs(self, project_id: str) -> list[dict]:
        """Return a project's runs, newest first."""
        cur = self._execute(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC, run_id",
            (project_id,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]  # type: ignore[misc]

    # -- stages ------------------------------------------------------------

    def upsert_stage(
        self,
        run_id: str,
        stage_index: int,
        stage: str,
        status: str = "pending",
        gate: Optional[str] = None,
        tool: Optional[str] = None,
        config_hash: Optional[str] = None,
        result: Any = None,
        now: Optional[str] = None,
    ) -> dict:
        """Insert or update a run's stage (keyed on ``(run_id, stage_index)``)."""
        if not run_id:
            raise StoreError("run_id is required")
        if stage_index is None:
            raise StoreError("stage_index is required")
        if not stage:
            raise StoreError("stage name is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO stages
                (run_id, stage_index, stage, gate, status, tool, config_hash, result_json, ran_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, stage_index) DO UPDATE SET
                stage       = excluded.stage,
                gate        = excluded.gate,
                status      = excluded.status,
                tool        = excluded.tool,
                config_hash = excluded.config_hash,
                result_json = excluded.result_json,
                ran_at      = excluded.ran_at
            """,
            (run_id, int(stage_index), stage, gate, status, tool, config_hash, _dumps(result), ts),
        )
        self._commit()
        return self.get_stage(run_id, int(stage_index))  # type: ignore[return-value]

    def get_stage(self, run_id: str, stage_index: int) -> Optional[dict]:
        """Return one stage row (JSON-hydrated) or ``None``."""
        cur = self._execute(
            "SELECT * FROM stages WHERE run_id = ? AND stage_index = ?",
            (run_id, int(stage_index)),
        )
        return _row_to_dict(cur.fetchone(), ("result_json",))

    def list_stages(self, run_id: str) -> list[dict]:
        """Return a run's stages in execution order."""
        cur = self._execute(
            "SELECT * FROM stages WHERE run_id = ? ORDER BY stage_index",
            (run_id,),
        )
        return [_row_to_dict(r, ("result_json",)) for r in cur.fetchall()]  # type: ignore[misc]

    # -- provenance --------------------------------------------------------

    def record_provenance(
        self,
        event_id: str,
        project_id: Optional[str] = None,
        run_id: Optional[str] = None,
        stage: Optional[str] = None,
        tool: Optional[str] = None,
        config_hash: Optional[str] = None,
        actor: str = "system",
        result: Optional[str] = None,
        target: Optional[str] = None,
        now: Optional[str] = None,
    ) -> dict:
        """Append (or replace) a provenance event (keyed on ``event_id``)."""
        if not event_id:
            raise StoreError("event_id is required")
        ts = now or _now_iso()
        self._execute(
            """
            INSERT INTO provenance
                (event_id, run_id, project_id, stage, tool, config_hash, actor, result, target, at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                run_id      = excluded.run_id,
                project_id  = excluded.project_id,
                stage       = excluded.stage,
                tool        = excluded.tool,
                config_hash = excluded.config_hash,
                actor       = excluded.actor,
                result      = excluded.result,
                target      = excluded.target,
                at          = excluded.at
            """,
            (event_id, run_id, project_id, stage, tool, config_hash, actor, result, target, ts),
        )
        self._commit()
        cur = self._execute("SELECT * FROM provenance WHERE event_id = ?", (event_id,))
        return _row_to_dict(cur.fetchone())  # type: ignore[return-value]

    def list_provenance(
        self,
        project_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[dict]:
        """Return provenance events for a run or a project (chronological)."""
        if run_id is not None:
            cur = self._execute(
                "SELECT * FROM provenance WHERE run_id = ? ORDER BY at, event_id",
                (run_id,),
            )
        elif project_id is not None:
            cur = self._execute(
                "SELECT * FROM provenance WHERE project_id = ? ORDER BY at, event_id",
                (project_id,),
            )
        else:
            cur = self._execute("SELECT * FROM provenance ORDER BY at, event_id")
        return [_row_to_dict(r) for r in cur.fetchall()]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Migration + open
# ---------------------------------------------------------------------------


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes (each statement is IF NOT EXISTS)."""
    for stmt in _SCHEMA_STATEMENTS:
        conn.execute(stmt)


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate the connection's database up to :data:`SCHEMA_VERSION`.

    Idempotent: a database already at the current version has its (all
    IF-NOT-EXISTS) DDL re-applied as a no-op and its ``user_version`` left
    unchanged.
    """
    cur = conn.execute("PRAGMA user_version")
    current = int(cur.fetchone()[0])
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"database schema version {current} is newer than supported "
            f"version {SCHEMA_VERSION}; upgrade the tooling"
        )
    # v0 -> v1: initial schema. Future versions add further guarded steps here.
    _apply_schema(conn)
    if current != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def open_db(path: str, timeout: float = 30.0) -> Store:
    """Open (creating if needed) the SQLite store at ``path`` and migrate it.

    The schema is created on first use and brought up to
    :data:`SCHEMA_VERSION`. Re-opening the same path is idempotent — the
    migration no-ops at the current version.

    Args:
        path: Filesystem path to the SQLite database file.
        timeout: Seconds to wait for a lock before failing (default 30).

    Returns:
        An open :class:`Store` whose ``path`` attribute echoes ``path``.

    Raises:
        StoreError: If the path is empty, the file is corrupt, the database is
            locked, or the schema is newer than this tooling supports.
    """
    if not path:
        raise StoreError("open_db requires a non-empty database path")
    try:
        conn = sqlite3.connect(path, timeout=timeout)
    except sqlite3.Error as exc:
        raise StoreError(f"could not open database at {path!r}: {exc}") from exc

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # Touch the schema early so a corrupt/incompatible file fails here
        # (wrapped) rather than on the first CRUD call.
        conn.execute("SELECT 1")
        _migrate(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise StoreError(f"could not initialise database at {path!r}: {exc}") from exc
    except StoreError:
        conn.close()
        raise

    return Store(conn, path)
