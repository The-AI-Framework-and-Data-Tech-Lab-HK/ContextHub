"""Reset AMC storage state: local content, pgvector table, and Neo4j graph data."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from psycopg import connect
from psycopg.rows import dict_row
from psycopg.sql import Identifier, SQL

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from app.config import load_settings  # noqa: E402


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def _reset_content(root: Path, *, dry_run: bool) -> dict[str, Any]:
    before_exists = root.exists()
    before_files = _count_files(root)
    if dry_run:
        return {
            "target": str(root),
            "before_exists": before_exists,
            "before_files": before_files,
            "deleted": False,
            "recreated": False,
            "dry_run": True,
        }
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return {
        "target": str(root),
        "before_exists": before_exists,
        "before_files": before_files,
        "deleted": before_exists,
        "recreated": True,
        "dry_run": False,
    }


def _reset_pgvector(*, dsn: str, schema: str, table: str, dry_run: bool) -> dict[str, Any]:
    if not dsn.strip():
        return {"enabled": False, "reason": "AMC_PGVECTOR_DSN is empty"}
    with connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                SQL("SELECT count(*) AS c FROM {}.{}").format(Identifier(schema), Identifier(table))
            )
            before = int((cur.fetchone() or {}).get("c") or 0)
            if not dry_run:
                cur.execute(SQL("TRUNCATE TABLE {}.{}").format(Identifier(schema), Identifier(table)))
            cur.execute(
                SQL("SELECT count(*) AS c FROM {}.{}").format(Identifier(schema), Identifier(table))
            )
            after = int((cur.fetchone() or {}).get("c") or 0)
    return {
        "enabled": True,
        "schema": schema,
        "table": table,
        "before_rows": before,
        "after_rows": after,
        "dry_run": dry_run,
    }


def _reset_neo4j(
    *,
    uri: str,
    user: str,
    password: str,
    database: str,
    dry_run: bool,
) -> dict[str, Any]:
    if not (uri.strip() and user.strip() and password.strip()):
        return {"enabled": False, "reason": "AMC_NEO4J_URI/USER/PASSWORD incomplete"}
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=database) as session:
            before = session.run(
                """
                MATCH (t:AMCTrajectory)
                WITH count(t) AS trajectories
                MATCH (n:AMCNode)
                RETURN trajectories, count(n) AS nodes
                """
            ).single()
            before_trajectories = int((before or {}).get("trajectories") or 0)
            before_nodes = int((before or {}).get("nodes") or 0)
            if not dry_run:
                session.run("MATCH (n:AMCNode) DETACH DELETE n")
                session.run("MATCH (t:AMCTrajectory) DETACH DELETE t")
            after = session.run(
                """
                MATCH (t:AMCTrajectory)
                WITH count(t) AS trajectories
                MATCH (n:AMCNode)
                RETURN trajectories, count(n) AS nodes
                """
            ).single()
            after_trajectories = int((after or {}).get("trajectories") or 0)
            after_nodes = int((after or {}).get("nodes") or 0)
    finally:
        driver.close()
    return {
        "enabled": True,
        "database": database,
        "before_trajectories": before_trajectories,
        "before_nodes": before_nodes,
        "after_trajectories": after_trajectories,
        "after_nodes": after_nodes,
        "dry_run": dry_run,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(config_path=args.config_path)
    content_root = Path(args.content_root or settings.storage.localfs_root)
    summary: dict[str, Any] = {
        "dry_run": bool(args.dry_run),
        "content": {},
        "pgvector": {},
        "neo4j": {},
    }
    errors: list[str] = []

    try:
        summary["content"] = _reset_content(content_root, dry_run=bool(args.dry_run))
    except Exception as exc:
        errors.append(f"content reset failed: {type(exc).__name__}: {exc}")

    try:
        summary["pgvector"] = _reset_pgvector(
            dsn=settings.pgvector_dsn,
            schema=settings.pgvector_schema,
            table=settings.pgvector_table,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        errors.append(f"pgvector reset failed: {type(exc).__name__}: {exc}")

    try:
        summary["neo4j"] = _reset_neo4j(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        errors.append(f"neo4j reset failed: {type(exc).__name__}: {exc}")

    summary["errors"] = errors
    summary["ok"] = len(errors) == 0
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reset_amc_storage",
        description="Reset AMC data/content + pgvector + neo4j storage data.",
    )
    parser.add_argument("--config-path", default=None, help="Optional config YAML path")
    parser.add_argument("--content-root", default=None, help="Override local content root path")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; do not mutate storage")
    parser.add_argument("--yes", action="store_true", help="Confirm destructive reset")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.dry_run and not args.yes:
        print(
            "Refusing destructive reset without confirmation. Re-run with --yes (or use --dry-run).",
            file=sys.stderr,
        )
        return 2
    summary = run(args)
    if args.pretty:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
