"""Provision/check real project-local PostgreSQL; never print secret DSNs.

Run with `uv run --locked python scripts/setup_db.py --install --migrate both`
once, then omit --install. Windows uses only the Ubuntu WSL distro; keep a
Windows-launched Linux command alive while using the DB (startup scripts do so).
Ubuntu administration uses sudo -n (or root). Existing data/secrets are preserved.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
PORT = 55432
DATABASES = (("sourcedesk", "sourcedesk_app"), ("sourcedesk_test", "sourcedesk_test_app"))


def admin_command(*args: str) -> list[str]:
    script = ROOT / "scripts" / "setup-postgres-wsl.sh"
    if platform.system() == "Windows":
        # WSL paths are derived without shell interpolation, including spaces.
        converted = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "wslpath", "-a", script.as_posix()],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return ["wsl", "-d", "Ubuntu", "-u", "root", "--", "bash", converted, *args]
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return [*prefix, "bash", str(script), *args]


def admin_sql(database: str, statement: str) -> str:
    # Password statements use stdin, not argv/environment/log output. Never
    # echo stderr: psql diagnostics can repeat a failing SQL statement.
    result = subprocess.run(
        admin_command("--psql", database),
        input=statement,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("PostgreSQL administration failed; credentials/SQL withheld.")
    return result.stdout.strip()


def write_private(path: Path, values: dict[str, str]) -> None:
    # Restrict access at creation, before any credentials are written. O_EXCL
    # also refuses existing files/symlinks, preserving credentials on reruns.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def database_host() -> str:
    if platform.system() != "Windows":
        return "127.0.0.1"
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "hostname", "-I"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()[0]


def local_environment() -> dict[str, str]:
    env_file = ROOT / ".env"
    test_file = ROOT / ".env.test"
    if not env_file.exists():
        urls = [
            f"postgresql+psycopg://{role}:{secrets.token_urlsafe(32)}@{database_host()}:{PORT}/{db}"
            for db, role in DATABASES
        ]
        write_private(
            env_file,
            {
                "SOURCEDESK_DATABASE_URL": urls[0],
                "SOURCEDESK_TEST_DATABASE_URL": urls[1],
                "SOURCEDESK_JWT_SECRET": secrets.token_urlsafe(48),
                "SOURCEDESK_PROVIDER": "fake",
                "SOURCEDESK_UPLOAD_DIR": ".local/uploads",
            },
        )
    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    if not test_file.exists():
        if not values.get("SOURCEDESK_TEST_DATABASE_URL"):
            raise RuntimeError("Existing .env lacks SOURCEDESK_TEST_DATABASE_URL; not overwriting.")
        # Keep application/test URLs distinct for the safety check in conftest.
        write_private(
            test_file,
            {
                "SOURCEDESK_DATABASE_URL": values["SOURCEDESK_DATABASE_URL"],
                "SOURCEDESK_TEST_DATABASE_URL": values["SOURCEDESK_TEST_DATABASE_URL"],
                "SOURCEDESK_JWT_SECRET": secrets.token_urlsafe(48),
                "SOURCEDESK_PROVIDER": "fake",
                "SOURCEDESK_UPLOAD_DIR": ".local/test-uploads",
            },
        )
    return values


def parse_project_url(url: str, database: str, role: str):
    parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != database_host()
        or parsed.port != PORT
        or parsed.path != f"/{database}"
        or parsed.username != role
        or not parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "Provisioning requires the dedicated SourceDesk URL; not modifying another DB."
        )
    return parsed


def refresh_wsl_host(values: dict[str, str]) -> dict[str, str]:
    """Refresh only exact private SourceDesk URLs, never credentials/settings."""
    host = database_host()
    if platform.system() != "Windows":
        return values
    updates = []
    for filename in (".env", ".env.test"):
        path = ROOT / filename
        # Preserve comments, quotes, unrelated settings, and original newlines.
        content = path.read_bytes().decode("utf-8")
        file_values = dotenv_values(path)
        for key, (database, role) in zip(
            ("SOURCEDESK_DATABASE_URL", "SOURCEDESK_TEST_DATABASE_URL"), DATABASES, strict=True
        ):
            old = file_values.get(key)
            if not old:
                raise RuntimeError("Cannot refresh incomplete local environment.")
            parsed = urlparse(old)
            if (
                parsed.scheme != "postgresql+psycopg"
                or parsed.port != PORT
                or parsed.path != f"/{database}"
                or parsed.username != role
                or not parsed.password
                or parsed.query
                or parsed.fragment
                or not ipaddress.ip_address(parsed.hostname).is_private
            ):
                raise RuntimeError("Refusing to refresh a non-project database URL.")
            updated = old.replace(f"@{parsed.hostname}:{PORT}/", f"@{host}:{PORT}/", 1)
            pattern = rf"(?m)^(?:export[ \t]+)?[ \t]*{re.escape(key)}[ \t]*=[^\r\n]*"

            def replace_line(match, old=old, updated=updated):
                line = match.group(0)
                if old not in line:
                    raise RuntimeError("Cannot safely refresh interpolated database URL.")
                return line.replace(old, updated, 1)

            content, count = re.subn(pattern, replace_line, content)
            if count != 1:
                raise RuntimeError("Cannot safely refresh duplicate or absent database URL.")
            if filename == ".env":
                values[key] = updated
        updates.append((path, content))
    # Validate both files before making any changes. Never rotate credentials.
    for path, content in updates:
        if path.read_bytes() != content.encode("utf-8"):
            path.write_bytes(content.encode("utf-8"))
    return values


def provision(values: dict[str, str]) -> None:
    for (database, role), key in zip(
        DATABASES,
        (
            "SOURCEDESK_DATABASE_URL",
            "SOURCEDESK_TEST_DATABASE_URL",
        ),
        strict=True,
    ):
        parsed = parse_project_url(values[key], database, role)
        exists = admin_sql(
            "postgres",
            sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {};")
            .format(sql.Literal(role))
            .as_string(),
        )
        if not exists:
            admin_sql(
                "postgres",
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;"
                )
                .format(sql.Identifier(role), sql.Literal(parsed.password))
                .as_string(),
            )
        exists = admin_sql(
            "postgres",
            sql.SQL("SELECT 1 FROM pg_database WHERE datname = {};")
            .format(sql.Literal(database))
            .as_string(),
        )
        if not exists:
            admin_sql(
                "postgres",
                sql.SQL("CREATE DATABASE {} OWNER {};")
                .format(sql.Identifier(database), sql.Identifier(role))
                .as_string(),
            )
        owner = admin_sql(
            "postgres",
            sql.SQL("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = {};")
            .format(sql.Literal(database))
            .as_string(),
        )
        if owner != role:
            raise RuntimeError("Existing database has unexpected owner; refusing changes.")
        admin_sql(
            "postgres",
            sql.SQL(
                "REVOKE CONNECT ON DATABASE {} FROM PUBLIC; GRANT CONNECT ON DATABASE {} TO {};"
            )
            .format(sql.Identifier(database), sql.Identifier(database), sql.Identifier(role))
            .as_string(),
        )
        admin_sql(database, "CREATE EXTENSION IF NOT EXISTS vector;")


def verify(values: dict[str, str]) -> list[dict]:
    reports = []
    urls = [values[key] for key in ("SOURCEDESK_DATABASE_URL", "SOURCEDESK_TEST_DATABASE_URL")]
    if urls[0] == urls[1] or "test" not in urlparse(urls[1]).path:
        raise RuntimeError(
            "Application and test databases must be separate; test DB name must contain test."
        )
    for label, url in zip(("application", "test"), urls, strict=True):
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=10
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version(), current_database(), current_user")
                version, database, role = cur.fetchone()
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Real pgvector extension is missing.")
                vector_version = row[0]
                cur.execute("SELECT '[1,2,3]'::vector <-> '[4,6,3]'::vector")
                distance = cur.fetchone()[0]
                if distance != 5.0:
                    raise RuntimeError("pgvector distance verification failed.")
                cur.execute(
                    "CREATE TEMP TABLE sourcedesk_vector_probe (embedding vector(1536)) ON COMMIT DROP"
                )
                basis = "[1," + ",".join(["0"] * 1535) + "]"
                other = "[0,1," + ",".join(["0"] * 1534) + "]"
                cur.execute("INSERT INTO sourcedesk_vector_probe VALUES (%s::vector)", (basis,))
                cur.execute(
                    "SELECT vector_dims(embedding), embedding <=> %s::vector FROM sourcedesk_vector_probe",
                    (other,),
                )
                dimensions, cosine_distance = cur.fetchone()
                if dimensions != 1536 or cosine_distance != 1.0:
                    raise RuntimeError(
                        "1536-dimensional vector storage/distance verification failed."
                    )
                cur.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
                if any(cur.fetchone()):
                    raise RuntimeError("Application role has unexpected elevated privileges.")
                cur.execute(
                    "SELECT roleid::regrole::text FROM pg_auth_members "
                    "WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
                )
                inherited_roles = [row[0] for row in cur.fetchall()]
                if inherited_roles:
                    raise RuntimeError("Project role has unexpected role memberships.")
                other_database = "sourcedesk_test" if label == "application" else "sourcedesk"
                cur.execute(
                    "SELECT has_database_privilege(current_user, %s, 'CONNECT')", (other_database,)
                )
                cross_database_connect = cur.fetchone()[0]
                if cross_database_connect:
                    raise RuntimeError("Project roles must not connect to each other's database.")
                reports.append(
                    {
                        "target": label,
                        "database": database,
                        "role": role,
                        "postgresql": version,
                        "pgvector": vector_version,
                        "l2_distance": distance,
                        "dimensions": dimensions,
                        "cosine_distance": cosine_distance,
                        "elevated_privileges": False,
                        "inherited_roles": inherited_roles,
                        "cross_database_connect": cross_database_connect,
                    }
                )
    return reports


def migrate(values: dict[str, str], target: str) -> None:
    """Upgrade only the named project databases, preserving all existing data."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = set(ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads())
    if not heads:
        raise RuntimeError("No Alembic revisions found; refusing unversioned schema setup.")
    for label, key, (database, role) in zip(
        ("application", "test"),
        ("SOURCEDESK_DATABASE_URL", "SOURCEDESK_TEST_DATABASE_URL"),
        DATABASES,
        strict=True,
    ):
        if target not in ("both", label):
            continue
        url = values[key]
        parse_project_url(url, database, role)
        # A separate connection holds a database-scoped migration lock while
        # Alembic runs. Two API startups cannot race schema creation.
        with psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1),
            connect_timeout=10,
            autocommit=True,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(hashtext('sourcedesk_alembic_v1'))")
                if not cur.fetchone()[0]:
                    raise RuntimeError(f"Another migration is running for {label}; retry later.")
                env = dict(os.environ)
                env.update(values)
                env["SOURCEDESK_DATABASE_URL"] = url
                result = subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                )
                if result.returncode:
                    raise RuntimeError(
                        f"Migration failed for {label}; sensitive diagnostics withheld."
                    )
                cur.execute("SELECT version_num FROM alembic_version")
                if {row[0] for row in cur.fetchall()} != heads:
                    raise RuntimeError(f"Alembic revision verification failed for {label}.")
                cur.execute(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'public.chunks'::regclass AND attname = 'embedding' "
                    "AND NOT attisdropped"
                )
                if cur.fetchone() != ("vector(1536)",):
                    raise RuntimeError(
                        f"Migrated vector column has unexpected dimensions in {label}."
                    )
        print(
            f"Migration verified: {label}; revisions={','.join(sorted(heads))}; embedding=vector(1536)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true", help="Install pinned Ubuntu packages before provisioning"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing URLs only; no persistent database or environment writes",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run pytest after DB checks with explicit isolated test URL",
    )
    parser.add_argument(
        "--refresh-wsl-host",
        action="store_true",
        help="Refresh private WSL IP in existing project URLs, preserving secrets",
    )
    parser.add_argument(
        "--migrate",
        choices=("application", "test", "both"),
        help="Upgrade Alembic on the selected dedicated database(s) and verify the schema",
    )
    args = parser.parse_args()
    if args.check and (args.install or args.refresh_wsl_host or args.migrate):
        parser.error("--check cannot be combined with options that modify environment/schema")
    os.chdir(ROOT)
    try:
        if args.check:
            values = {
                key: value
                for key, value in dotenv_values(ROOT / ".env").items()
                if value is not None
            }
            for key in ("SOURCEDESK_DATABASE_URL", "SOURCEDESK_TEST_DATABASE_URL"):
                if os.environ.get(key):
                    values[key] = os.environ[key]
        else:
            action = "--install" if args.install else "--start"
            if platform.system() == "Windows":
                action += "-windows"
            subprocess.run(admin_command(action), check=True)
            values = local_environment()
            if platform.system() == "Windows" or args.refresh_wsl_host:
                values = refresh_wsl_host(values)
            provision(values)
        reports = verify(values)
        if args.migrate:
            migrate(values, args.migrate)
        print(json.dumps({"native_python": platform.python_version(), "checks": reports}, indent=2))
        if not args.check:
            report = ROOT / ".local" / "database-verification.json"
            report.parent.mkdir(exist_ok=True)
            report.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
        if args.test:
            env = dict(os.environ)
            env.update(values)
            return subprocess.run([sys.executable, "-m", "pytest", "-q"], env=env).returncode
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (KeyError, ValueError, psycopg.Error, OSError, subprocess.CalledProcessError):
        # Driver/OS errors can contain a URL/password. Never serialize exception details.
        print(
            "Database setup/check failed (connection details withheld). Check native cluster state, project env files, and installation prerequisites.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
