"""System backup and restore controls.

Backups are portable PostgreSQL dumps plus the application encryption key.  The
runtime database URL is deliberately never written to an archive or to backup
metadata; the destination connection remains a deployment concern (compose
secret/environment) and the Backup screen provides a guarded live migration
path to a newly provisioned external PostgreSQL database.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.v2 import CONFIG_DIR
from app import db_bootstrap
from app.v11 import app, connection
from app.plex_secret import KEY_PATH

logger = logging.getLogger("uvicorn.error")
_backup_lock = threading.Lock()
_backup_running = False
_backup_state = {"running": False, "kind": "", "path": "", "message": "", "started_at": None, "finished_at": None}

FREQUENCIES = {"disabled": 0, "daily": 1, "every_other_day": 2, "weekly": 7}
DEFAULT_LOCATION = os.getenv("BACKUP_DIR", "/backup")


class BackupSchedule(BaseModel):
    frequency: Literal["disabled", "daily", "every_other_day", "weekly"] = "disabled"
    time: str = "03:00"


class BackupLocation(BaseModel):
    location: str = Field(min_length=1, max_length=1000)


class RestoreRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    confirm: Literal["RESTORE"]


class RemoteMigrationRequest(BaseModel):
    server_url: str = Field(min_length=1, max_length=1000)
    admin_user: str = Field(min_length=1, max_length=128)
    admin_password: str = Field(default="", max_length=1000)
    maintenance_database: str = Field(default="postgres", min_length=1, max_length=128)
    target_database: str = Field(default="videostreamedit", min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    app_user: str = Field(default="videostreamedit", min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    app_password: str = Field(min_length=1, max_length=1000)
    confirm: Literal["MIGRATE"]


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError):
        raise HTTPException(400, "Backup time must use HH:MM")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise HTTPException(400, "Backup time must use HH:MM")
    return hour, minute


def _ensure_tables() -> None:
    with connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS backup_settings (
            id INTEGER PRIMARY KEY, location TEXT NOT NULL DEFAULT '/backup',
            frequency TEXT NOT NULL DEFAULT 'disabled', time_of_day TEXT NOT NULL DEFAULT '03:00',
            last_run TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("INSERT OR IGNORE INTO backup_settings(id,location) VALUES(1,?)", (DEFAULT_LOCATION,))
        db.execute("""CREATE TABLE IF NOT EXISTS backup_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
            finished_at TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS backup_runs_recent ON backup_runs(id DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS migration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info', created_at TEXT NOT NULL
        )""")


def _migration_event(message: str, level: str = "info") -> None:
    try:
        with connection() as db:
            db.execute("INSERT INTO migration_events(message,level,created_at) VALUES(?,?,?)", (message[:4000], level, _now().isoformat(timespec="seconds")))
    except Exception as exc:
        logger.warning("backup event=migration_log_failed error=%s", str(exc).replace("\n", " ")[-300:])


def _safe_database_target() -> str:
    parsed = urllib.parse.urlsplit(db_bootstrap.effective_url())
    if not parsed.hostname:
        return "not configured"
    user = urllib.parse.unquote(parsed.username or "")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"postgresql://{user}@{host}:{parsed.port or 5432}/{parsed.path.lstrip('/')}"


@app.on_event("startup")
def initialize_backup() -> None:
    _ensure_tables()
    start_backup_scheduler()


def _settings() -> dict:
    with connection() as db:
        row = db.execute("SELECT location,frequency,time_of_day,last_run FROM backup_settings WHERE id=1").fetchone()
    if not row:
        return {"location": DEFAULT_LOCATION, "frequency": "disabled", "time": "03:00", "last_run": None}
    return {"location": str(row["location"] or DEFAULT_LOCATION), "frequency": str(row["frequency"] or "disabled"), "time": str(row["time_of_day"] or "03:00"), "last_run": row["last_run"]}


def _backup_files(location: str) -> list[dict]:
    directory = Path(location).expanduser()
    if not directory.is_dir():
        return []
    result = []
    for path in directory.glob("videostreamedit-backup-*.tar.gz"):
        try:
            stat = path.stat()
            result.append({"path": str(path), "name": path.name, "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")})
        except OSError:
            continue
    return sorted(result, key=lambda item: item["modified"], reverse=True)


def _record_run(kind: str, path: str, status: str, message: str, started: str, finished: str | None = None) -> None:
    with connection() as db:
        db.execute("INSERT INTO backup_runs(kind,path,status,message,started_at,finished_at) VALUES(?,?,?,?,?,?)", (kind, path, status, message[:4000], started, finished))


def _connection_parts() -> tuple[list[str], dict[str, str]]:
    return _connection_parts_for(os.getenv("DATABASE_URL", "").strip())


def _connection_parts_for(raw: str) -> tuple[list[str], dict[str, str]]:
    if not raw:
        raise RuntimeError("DATABASE_URL is not configured")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise RuntimeError("Backup requires a PostgreSQL DATABASE_URL")
    args = ["--host", parsed.hostname, "--port", str(parsed.port or 5432), "--username", urllib.parse.unquote(parsed.username or ""), "--dbname", urllib.parse.unquote(parsed.path.lstrip("/"))]
    query = urllib.parse.parse_qs(parsed.query)
    env = {"PGPASSWORD": urllib.parse.unquote(parsed.password or "")}
    # pg_dump/pg_restore use libpq environment settings, not --sslmode.
    for name, variable in (('sslmode', 'PGSSLMODE'), ('sslrootcert', 'PGSSLROOTCERT'), ('sslcert', 'PGSSLCERT'), ('sslkey', 'PGSSLKEY'), ('connect_timeout', 'PGCONNECT_TIMEOUT')):
        if query.get(name):
            env[variable] = query[name][0]
    return args, env


def _provision_remote(request: RemoteMigrationRequest) -> str:
    import psycopg
    from psycopg import sql
    maintenance = db_bootstrap.maintenance_url(request.server_url, request.admin_user, request.admin_password, request.maintenance_database)
    target = db_bootstrap.target_url(request.server_url, request.app_user, request.app_password, request.target_database)
    with psycopg.connect(maintenance, autocommit=True) as db:
        if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (request.app_user,)).fetchone():
            db.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(request.app_user), sql.Literal(request.app_password)))
        else:
            db.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(request.app_user), sql.Literal(request.app_password)))
        if db.execute("SELECT 1 FROM pg_database WHERE datname=%s", (request.target_database,)).fetchone():
            raise RuntimeError(f"Target database already exists: {request.target_database}. Choose an empty database name or remove it first.")
        db.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(request.target_database), sql.Identifier(request.app_user)))
    with psycopg.connect(target, connect_timeout=10):
        pass
    return target


def _run_migration(request: RemoteMigrationRequest) -> None:
    global _backup_running
    started = _now().isoformat(timespec="seconds")
    try:
        _migration_event("Migration started: preparing a consistent dump of the current database")
        with tempfile.TemporaryDirectory(prefix="vse-migration-") as temp:
            dump = Path(temp) / "database.dump"
            current_args, current_env = _connection_parts()
            env = os.environ.copy(); env.update(current_env)
            result = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(dump), *current_args], env=env, capture_output=True, text=True, timeout=3600)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "Current database dump failed").strip()[-4000:])
            _migration_event("Current database dump completed")
            target = _provision_remote(request)
            _migration_event("Remote PostgreSQL role and empty database created")
            target_args, target_env = _connection_parts_for(target)
            env = os.environ.copy(); env.update(target_env)
            result = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error", *target_args, str(dump)], env=env, capture_output=True, text=True, timeout=3600)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout or "Remote database restore failed").strip()[-4000:])
            _migration_event("Remote database restore completed and was verified")
        db_bootstrap.save_url(target)
        _migration_event("Encrypted remote connection saved; restart is required to switch the application")
        _migration_event(f"Current database target: {_safe_database_target()}")
        finished = _now().isoformat(timespec="seconds")
        _record_run("migration", target, "succeeded", "Remote database populated; restart the application to switch connections", started, finished)
        _set_state(running=False, kind="migration", path=target, message="Remote database ready; restart the application", finished_at=finished)
        logger.info("backup event=remote_migration_succeeded restart_required=true")
    except Exception as exc:
        _migration_event(f"Migration failed: {exc}", "error")
        finished = _now().isoformat(timespec="seconds")
        _record_run("migration", "", "failed", str(exc), started, finished)
        _set_state(running=False, kind="migration", path="", message=str(exc), finished_at=finished)
        logger.exception("backup event=remote_migration_failed")
    finally:
        _backup_running = False


def _set_state(**values) -> None:
    _backup_state.update(values)


def _run_create() -> None:
    global _backup_running
    started = _now().isoformat(timespec="seconds")
    settings = _settings()
    location = Path(settings["location"]).expanduser()
    path_text = ""
    try:
        location.mkdir(parents=True, exist_ok=True)
        if not os.access(location, os.W_OK):
            raise RuntimeError(f"Backup location is not writable: {location}")
        reserve_gb = float(os.getenv("WORKFLOW_RESERVED_GB", "2"))
        if shutil.disk_usage(location).free < reserve_gb * 1024**3:
            raise RuntimeError(f"Backup refused: free space is below the reserved {reserve_gb:g} GB")
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        archive = location / f"videostreamedit-backup-{stamp}.tar.gz"
        path_text = str(archive)
        args, env = _connection_parts()
        with tempfile.TemporaryDirectory(prefix="vse-backup-") as temp:
            dump = Path(temp) / "database.dump"
            merged_env = os.environ.copy(); merged_env.update(env)
            dump_result = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", *args, "--file", str(dump)], env=merged_env, capture_output=True, text=True, timeout=3600)
            if dump_result.returncode:
                detail = (dump_result.stderr or dump_result.stdout or "pg_dump failed").strip()
                raise RuntimeError(detail[-4000:])
            manifest = {"format": 1, "created_at": started, "database": "videostreamedit", "contains": ["postgresql_dump", "application_encryption_key"], "connection": "not included", "media": "not included", "transient_workflows": "not included"}
            temporary_archive = archive.with_suffix(archive.suffix + ".partial")
            with tarfile.open(temporary_archive, "w:gz") as tar:
                data = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
                import io
                info = tarfile.TarInfo("manifest.json"); info.size = len(data); info.mtime = int(_now().timestamp()); tar.addfile(info, io.BytesIO(data))
                tar.add(str(dump), arcname="database.dump")
                if KEY_PATH.is_file():
                    tar.add(str(KEY_PATH), arcname="config/plex-token.key")
            os.replace(temporary_archive, archive)
        files = _backup_files(str(location))
        for stale in files[10:]:
            try: Path(stale["path"]).unlink()
            except OSError: pass
        finished = _now().isoformat(timespec="seconds")
        _record_run("create", str(archive), "succeeded", "Backup created", started, finished)
        _set_state(running=False, kind="create", path=str(archive), message="Backup created", finished_at=finished)
        with connection() as db:
            db.execute("UPDATE backup_settings SET last_run=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (finished,))
        logger.info("backup event=created path=%s retained=%d", str(archive), min(len(files), 10))
    except Exception as exc:
        finished = _now().isoformat(timespec="seconds")
        _record_run("create", path_text, "failed", str(exc), started, finished)
        _set_state(running=False, kind="create", path=path_text, message=str(exc), finished_at=finished)
        logger.exception("backup event=create_failed path=%s", path_text)
    finally:
        _backup_running = False


def _run_restore(path: str) -> None:
    global _backup_running
    started = _now().isoformat(timespec="seconds")
    source = Path(path).expanduser()
    try:
        if not source.is_file() or source.suffixes[-2:] != [".tar", ".gz"]:
            raise RuntimeError("Choose a .tar.gz VideoStreamEdit backup")
        args, env = _connection_parts(); merged_env = os.environ.copy(); merged_env.update(env)
        with tempfile.TemporaryDirectory(prefix="vse-restore-") as temp:
            temp_path = Path(temp)
            with tarfile.open(source, "r:gz") as tar:
                names = set(tar.getnames())
                if "manifest.json" not in names or "database.dump" not in names:
                    raise RuntimeError("The selected archive is not a valid VideoStreamEdit backup")
                manifest = json.loads(tar.extractfile("manifest.json").read())
                if manifest.get("format") != 1:
                    raise RuntimeError("Unsupported backup format")
                member = tar.getmember("database.dump"); member.name = "database.dump"; tar.extract(member, temp_path)
                if "config/plex-token.key" in names:
                    key_member = tar.getmember("config/plex-token.key"); key_member.name = "plex-token.key"; tar.extract(key_member, temp_path)
            restore_result = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error", *args, str(temp_path / "database.dump")], env=merged_env, capture_output=True, text=True, timeout=3600)
            if restore_result.returncode:
                detail = (restore_result.stderr or restore_result.stdout or "pg_restore failed").strip()
                raise RuntimeError(detail[-4000:])
            restored_key = temp_path / "plex-token.key"
            if restored_key.is_file():
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                os.replace(restored_key, KEY_PATH)
                os.chmod(KEY_PATH, 0o600)
        finished = _now().isoformat(timespec="seconds")
        _record_run("restore", str(source), "succeeded", "Database restored; restart the application", started, finished)
        _set_state(running=False, kind="restore", path=str(source), message="Database restored; restart the application", finished_at=finished)
        logger.info("backup event=restored path=%s restart_required=true", str(source))
    except Exception as exc:
        finished = _now().isoformat(timespec="seconds")
        _record_run("restore", str(source), "failed", str(exc), started, finished)
        _set_state(running=False, kind="restore", path=str(source), message=str(exc), finished_at=finished)
        logger.exception("backup event=restore_failed path=%s", str(source))
    finally:
        _backup_running = False


def _start(kind: str, path: str = "", request: RemoteMigrationRequest | None = None) -> dict:
    global _backup_running
    with _backup_lock:
        if _backup_running:
            raise HTTPException(409, "A backup operation is already running")
        _backup_running = True
        started = _now().isoformat(timespec="seconds")
        _set_state(running=True, kind=kind, path=path, message="Preparing…", started_at=started, finished_at=None)
        if kind == "create":
            worker = _run_create
        elif kind == "restore":
            worker = lambda: _run_restore(path)
        else:
            worker = lambda: _run_migration(request)  # type: ignore[arg-type]
        threading.Thread(target=worker, name=f"vse-backup-{kind}", daemon=True).start()
    return {"accepted": True, "status": "running"}


@app.get("/api/v99/backup/status")
def backup_status() -> dict:
    _ensure_tables()
    settings = _settings()
    try: backups = _backup_files(settings["location"])
    except Exception: backups = []
    with connection() as db:
        history = [dict(row) for row in db.execute("SELECT id,kind,path,status,message,started_at,finished_at FROM backup_runs ORDER BY id DESC LIMIT 20").fetchall()]
    with connection() as db:
        migration_log = [dict(row) for row in db.execute("SELECT id,message,level,created_at FROM migration_events ORDER BY id DESC LIMIT 100").fetchall()]
    migration_log.insert(0, {"id": 0, "message": f"Current database target: {_safe_database_target()}", "level": "current", "created_at": _now().isoformat(timespec="seconds")})
    return {"running": bool(_backup_state["running"]), "state": dict(_backup_state), "location": settings["location"], "schedule": {"frequency": settings["frequency"], "time": settings["time"], "last_run": settings["last_run"], "next_run": _next_run(settings["frequency"], settings["time"])}, "backups": backups, "history": history, "migration_log": migration_log}


@app.put("/api/v99/backup/location")
def backup_location(request: BackupLocation) -> dict:
    location = str(Path(request.location).expanduser())
    if not location.startswith("/"):
        raise HTTPException(400, "Backup location must be an absolute path visible inside the container")
    Path(location).mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute("UPDATE backup_settings SET location=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (location,))
    return {"location": location}


@app.put("/api/v99/backup/schedule")
def backup_schedule(request: BackupSchedule) -> dict:
    _parse_time(request.time)
    with connection() as db:
        db.execute("UPDATE backup_settings SET frequency=?,time_of_day=?,updated_at=CURRENT_TIMESTAMP WHERE id=1", (request.frequency, request.time))
    return {"frequency": request.frequency, "time": request.time, "next_run": _next_run(request.frequency, request.time)}


@app.post("/api/v99/backup/create")
def create_backup() -> dict:
    return _start("create")


@app.post("/api/v99/backup/restore")
def restore_backup(request: RestoreRequest) -> dict:
    return _start("restore", request.path)


@app.post("/api/v99/backup/migrate")
def migrate_database(request: RemoteMigrationRequest) -> dict:
    """Copy the live database to a newly provisioned remote PostgreSQL database."""
    return _start("migration", request=request)


@app.get("/api/v99/system-info")
def system_info() -> dict:
    """Return a non-secret operational summary for Setup → Tasks → Info."""
    def storage(path: str) -> dict:
        try:
            stat = shutil.disk_usage(path)
            return {"path": path, "used_bytes": stat.total - stat.free, "free_bytes": stat.free, "total_bytes": stat.total}
        except OSError:
            return {"path": path, "used_bytes": 0, "free_bytes": 0, "total_bytes": 0, "available": False}
    parsed = urllib.parse.urlsplit(db_bootstrap.effective_url())
    target = {"host": parsed.hostname or "", "port": parsed.port or 5432, "database": parsed.path.lstrip("/"), "user": urllib.parse.unquote(parsed.username or "")}
    try:
        with connection() as db:
            row = db.execute("SELECT inet_server_addr() AS address, inet_server_port() AS port, current_database() AS database, current_user AS username").fetchone()
            target["connected_address"] = str(row["address"] or "") if row else ""
            target["connected_port"] = int(row["port"] or 0) if row else 0
    except Exception as exc:
        target["connection_error"] = str(exc)[:500]
    return {"application": "VideoStreamEdit", "database": target, "storage": [storage(path) for path in ("/config", "/data", "/backup", "/media")], "backup_location": _settings()["location"], "runtime": {"database_backend": os.getenv("DATABASE_BACKEND", "sqlite"), "workflow_reserved_gb": os.getenv("WORKFLOW_RESERVED_GB", "2"), "workflow_min_free_gb": os.getenv("WORKFLOW_MIN_FREE_GB", "5")}}


def _next_run(frequency: str, time_value: str) -> str | None:
    if frequency == "disabled": return None
    hour, minute = _parse_time(time_value); now = _now(); candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now: candidate += timedelta(days=FREQUENCIES[frequency])
    return candidate.isoformat(timespec="minutes")


def run_backup_scheduler() -> None:
    logger.info("backup event=scheduler_started")
    while True:
        try:
            settings = _settings(); frequency = settings["frequency"]
            if frequency != "disabled" and settings["last_run"]:
                last = datetime.fromisoformat(str(settings["last_run"])).astimezone()
                due = (_now() - last).total_seconds() >= FREQUENCIES[frequency] * 86400 and _now().strftime("%H:%M") >= settings["time"]
            else:
                due = frequency != "disabled" and _now().strftime("%H:%M") >= settings["time"]
            if due and not _backup_running:
                _start("create")
        except Exception as exc:
            logger.warning("backup event=scheduler_failed error=%s", str(exc).replace("\n", " ")[-500:])
        threading.Event().wait(30)


def start_backup_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=run_backup_scheduler, name="vse-backup-scheduler", daemon=True)
        _scheduler_thread.start()


_scheduler_thread: threading.Thread | None = None
