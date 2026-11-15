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


def test_only_watchers_have_no_retries(dagbag):
    """Падать без повтора — через AirflowFailException, а не retries=0.

    Иначе задачу без ретраев валит любой сбой запуска: 14.11 после включения
    компьютера sync_sources не стартовал, и пропал целый запуск ingest_vk.
    Без ретраев остаются только сторожа (watcher): они и так всегда падают.
    """
    for dag_id, dag in dagbag.dags.items():
        for task in dag.tasks:
            if task.retries == 0:
                assert task.trigger_rule == "one_failed", f"{dag_id}.{task.task_id}: нет ретраев"


def test_ingest_vk_structure(dagbag):
    dag = dagbag.dags["ingest_vk"]  # get_dag() лезет в метабазу, здесь она не нужна
    assert set(dag.task_ids) == {"sync_sources", "list_sources", "fetch_source", "emit_asset", "watcher"}
    assert dag.get_task("list_sources").upstream_task_ids == {"sync_sources"}
    assert dag.get_task("emit_asset").upstream_task_ids == {"fetch_source"}
    assert dag.get_task("watcher").upstream_task_ids == {"sync_sources", "list_sources", "fetch_source", "emit_asset"}
    assert [a.name for a in dag.get_task("emit_asset").outlets] == ["raw_vk_posts"]


def test_ingest_tg_structure(dagbag):
    dag = dagbag.dags["ingest_tg"]
    assert set(dag.task_ids) == {"sync_sources", "list_sources", "fetch_channel", "emit_asset", "watcher"}
    assert dag.get_task("list_sources").upstream_task_ids == {"sync_sources"}
    assert dag.get_task("emit_asset").upstream_task_ids == {"fetch_channel"}
    assert [a.name for a in dag.get_task("emit_asset").outlets] == ["raw_tg_posts"]


def test_ingest_sites_structure(dagbag):
    dag = dagbag.dags["ingest_sites"]
    assert set(dag.task_ids) == {"sync_sources", "list_sources", "fetch_site", "emit_asset", "watcher"}
    assert dag.get_task("emit_asset").upstream_task_ids == {"fetch_site"}
    assert [a.name for a in dag.get_task("emit_asset").outlets] == ["raw_site_events"]


def test_process_events_runs_on_either_platform(dagbag):
    dag = dagbag.dags["process_events"]
    condition = dag.timetable.asset_condition  # AssetAny: хватит любого из двух
    assert type(condition).__name__ == "AssetAny"
    assert {a.name for a in condition.objects} == {"raw_vk_posts", "raw_tg_posts", "raw_site_events"}


def test_process_events_structure(dagbag):
    dag = dagbag.dags["process_events"]
    assert set(dag.task_ids) == {
        "extract_events",
        "load_site_events",
        "validate_quality",
        "resolve_venues",
        "dedupe_events",
    }
    assert dag.get_task("validate_quality").upstream_task_ids == {"extract_events", "load_site_events"}
    assert dag.get_task("resolve_venues").upstream_task_ids == {"validate_quality"}
    assert dag.get_task("dedupe_events").upstream_task_ids == {"resolve_venues"}
    assert [a.name for a in dag.get_task("dedupe_events").outlets] == ["catalog_events"]


def test_build_site_structure(dagbag):
    dag = dagbag.dags["build_site"]
    assert set(dag.task_ids) == {"render_site", "deploy_site"}
    assert dag.get_task("deploy_site").upstream_task_ids == {"render_site"}


def test_source_health_structure(dagbag):
    dag = dagbag.dags["source_health"]
    assert set(dag.task_ids) == {"check_sources", "alert"}
    assert dag.get_task("alert").upstream_task_ids == {"check_sources"}


def test_archive_structure(dagbag):
    dag = dagbag.dags["archive"]
    assert set(dag.task_ids) == {"archive_past", "trim_ingest_log"}
    assert dag.get_task("archive_past").upstream_task_ids == set()  # задачи независимы


def test_publish_structure(dagbag):
    dag = dagbag.dags["publish"]
    assert set(dag.task_ids) == {"sync_targets", "list_targets", "publish_digest", "watcher"}
    assert dag.get_task("list_targets").upstream_task_ids == {"sync_targets"}
    assert dag.get_task("publish_digest").upstream_task_ids == {"list_targets"}
    assert set(dag.params) == {"issue_date", "force"}
