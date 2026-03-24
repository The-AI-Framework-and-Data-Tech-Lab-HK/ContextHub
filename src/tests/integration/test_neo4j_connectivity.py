"""Optional Neo4j connectivity (I-03 prerequisite). Skip when not configured."""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.integration]


def _neo4j_uri() -> str | None:
    return os.environ.get("AMC_NEO4J_URI") or os.environ.get("NEO4J_URI")


@pytest.mark.skipif(not _neo4j_uri(), reason="AMC_NEO4J_URI or NEO4J_URI not set")
def test_neo4j_driver_can_run_return_1() -> None:
    from neo4j import GraphDatabase

    uri = _neo4j_uri()
    user = os.environ.get("AMC_NEO4J_USER") or os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("AMC_NEO4J_PASSWORD") or os.environ.get("NEO4J_PASSWORD", "")
    database = os.environ.get("AMC_NEO4J_DATABASE") or os.environ.get("NEO4J_DATABASE", "neo4j")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            row = session.run("RETURN 1 AS n").single()
            assert row and row["n"] == 1
    finally:
        driver.close()
