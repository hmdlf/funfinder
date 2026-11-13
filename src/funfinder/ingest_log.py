"""Журнал загрузок meta.ingest_log: кто, когда, сколько, с какой ошибкой."""


def log_start(conn, *, dag_id: str, run_id: str, source_id: int, attempt: int) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO meta.ingest_log (dag_id, run_id, source_id, status, attempt)
            VALUES (%s, %s, %s, 'running', %s)
            ON CONFLICT (dag_id, run_id, source_id) DO UPDATE
               SET status = 'running', attempt = EXCLUDED.attempt, started_at = now(),
                   finished_at = NULL, error = NULL,
                   pages = NULL, fetched = NULL, inserted = NULL, updated = NULL
            """,
            (dag_id, run_id, source_id, attempt),
        )


def log_finish(
    conn,
    *,
    dag_id: str,
    run_id: str,
    source_id: int,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    stats = stats or {}
    conn.rollback()  # если основная работа упала посреди транзакции
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE meta.ingest_log
               SET status = %s, finished_at = now(), error = %s,
                   pages = %s, fetched = %s, inserted = %s, updated = %s
             WHERE dag_id = %s AND run_id = %s AND source_id = %s
            """,
            (
                status,
                error[:2000] if error else None,
                stats.get("pages"),
                stats.get("fetched"),
                stats.get("inserted"),
                stats.get("updated"),
                dag_id,
                run_id,
                source_id,
            ),
        )


def run_totals(conn, *, dag_id: str, run_id: str) -> dict:
    """Итоги запуска по всем источникам."""
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'success'),
                   count(*) FILTER (WHERE status <> 'success'),
                   coalesce(sum(inserted), 0),
                   coalesce(sum(updated), 0)
              FROM meta.ingest_log
             WHERE dag_id = %s AND run_id = %s
            """,
            (dag_id, run_id),
        )
        ok, not_ok, inserted, updated = cur.fetchone()
    return {"sources_ok": ok, "sources_failed": not_ok, "inserted": int(inserted), "updated": int(updated)}
