"""DAG-и импортируются без ошибок и устроены так, как задумано."""

from pathlib import Path

import pytest

pytest.importorskip("airflow")

from airflow.dag_processing.dagbag import DagBag

DAGS_DIR = Path(__file__).resolve().parents[1] / "dags"


@pytest.fixture(scope="module")
def dagbag():
    return DagBag(dag_folder=str(DAGS_DIR))


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_every_dag_has_tags_retries_and_no_catchup(dagbag):
    for dag_id, dag in dagbag.dags.items():
        assert dag.tags, f"{dag_id}: нет тегов"
        assert dag.catchup is False, f"{dag_id}: catchup должен быть выключен"
        assert dag.default_args.get("retries", 0) >= 1, f"{dag_id}: нет ретраев"


def test_ingest_vk_structure(dagbag):
    dag = dagbag.dags["ingest_vk"]  # get_dag() лезет в метабазу, здесь она не нужна
    assert set(dag.task_ids) == {"sync_sources", "list_sources", "fetch_source", "emit_asset", "watcher"}
    assert dag.get_task("list_sources").upstream_task_ids == {"sync_sources"}
    assert dag.get_task("emit_asset").upstream_task_ids == {"fetch_source"}
    assert dag.get_task("watcher").upstream_task_ids == {"sync_sources", "list_sources", "fetch_source", "emit_asset"}
    assert [a.name for a in dag.get_task("emit_asset").outlets] == ["raw_vk_posts"]


def test_process_events_structure(dagbag):
    dag = dagbag.dags["process_events"]
    assert set(dag.task_ids) == {"extract_events", "validate_quality", "resolve_venues", "dedupe_events"}
    assert dag.get_task("validate_quality").upstream_task_ids == {"extract_events"}
    assert dag.get_task("resolve_venues").upstream_task_ids == {"validate_quality"}
    assert dag.get_task("dedupe_events").upstream_task_ids == {"resolve_venues"}
    assert [a.name for a in dag.get_task("dedupe_events").outlets] == ["catalog_events"]


def test_build_site_structure(dagbag):
    dag = dagbag.dags["build_site"]
    assert set(dag.task_ids) == {"render_site", "deploy_site"}
    assert dag.get_task("deploy_site").upstream_task_ids == {"render_site"}
