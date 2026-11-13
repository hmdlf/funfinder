"""Загрузка стены одного сообщества VK в raw.vk_posts.

Как выбираются посты
--------------------
Стена отдаётся от новых к старым (закреплённый пост — первым, вне порядка).
Идём по страницам и берём пост, если:
- его id больше водяного знака (максимального id, который уже есть в базе) —
  это новый пост;
- или он опубликован в окне перепроверки (по умолчанию 7 дней) — организаторы
  часто правят анонсы: переносят дату, меняют цену.
На первом посте, который не подходит ни под одно условие, останавливаемся.

При первой загрузке водяного знака нет, и берём всё за initial_lookback_days.

Запись идемпотентна: INSERT ... ON CONFLICT по (owner_id, post_id).
Строка обновляется, только если поменялся текст (content_hash), поэтому
повторный запуск ничего не меняет.
"""

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta

from psycopg2.extras import Json, execute_values

log = logging.getLogger(__name__)

# Поля, по которым в ответе VK можно опознать человека. В raw их не храним:
# персональные данные третьих лиц проекту не нужны ни на каком слое.
_PERSON_KEYS = ("signer_id", "from_id", "created_by")


@dataclass(frozen=True)
class IngestSettings:
    initial_lookback_days: int = 30
    # Неделя обычно умещается в одну страницу из 100 постов, то есть
    # перепроверка правок почти ничего не стоит.
    recheck_days: int = 7
    max_pages: int = 10
    page_size: int = 100  # максимум wall.get
    wall_filter: str = "owner"

    @classmethod
    def from_overrides(cls, overrides: dict | None) -> "IngestSettings":
        overrides = overrides or {}
        known = {f.name for f in fields(cls)}
        unknown = set(overrides) - known
        if unknown:
            raise ValueError(f"неизвестные настройки {sorted(unknown)}, допустимы {sorted(known)}")
        settings = cls(**overrides)
        if settings.wall_filter not in ("owner", "all"):
            raise ValueError("wall_filter: owner или all")
        if not 1 <= settings.page_size <= 100:
            raise ValueError("page_size: от 1 до 100")
        for name in ("initial_lookback_days", "recheck_days", "max_pages"):
            if getattr(settings, name) < 1:
                raise ValueError(f"{name} должно быть положительным")
        return settings


@dataclass
class IngestResult:
    pages: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class SourceUnavailable(Exception):
    """Сообщество закрыто, удалено или заблокировано."""


_HANDLE_RE = re.compile(r"^(?:https?://)?(?:m\.)?vk\.(?:com|ru)/", re.IGNORECASE)


def normalize_handle(raw: str) -> str:
    """'https://vk.com/Lipetsk_Afisha/' → 'lipetsk_afisha'."""
    handle = _HANDLE_RE.sub("", raw.strip()).strip("/").lstrip("@")
    handle = handle.split("?", 1)[0].split("#", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_.]+", handle):
        raise ValueError(f"не похоже на адрес сообщества VK: {raw!r}")
    return handle.lower()


def sanitize_payload(post: dict) -> dict:
    """Копия поста без идентификаторов людей (в том числе внутри репостов).

    Отрицательные id — это сообщества, их оставляем: они нужны, чтобы
    понимать, чей пост перепостили.
    """

    def clean(item: dict) -> dict:
        out = {}
        for key, value in item.items():
            if key in _PERSON_KEYS and isinstance(value, int) and value > 0:
                continue
            out[key] = value
        if "copy_history" in out:
            out["copy_history"] = [clean(c) for c in out["copy_history"]]
        return out

    return clean(post)


def post_texts(post: dict) -> tuple[str, str]:
    text = post.get("text") or ""
    repost_text = "\n\n".join(c.get("text") or "" for c in post.get("copy_history") or []).strip()
    return text, repost_text


def content_hash(text: str, repost_text: str) -> str:
    return hashlib.sha256(f"{text}\x00{repost_text}".encode()).hexdigest()


def collect_posts(
    fetch_page: Callable[[int, int], list[dict]],
    *,
    watermark: int | None,
    cutoff: datetime,
    settings: IngestSettings,
) -> tuple[list[dict], int]:
    """Листает стену и возвращает посты для записи и число страниц.

    fetch_page(offset, count) — обёртка над wall.get; так функцию можно
    проверить без сети.
    """
    posts: dict[int, dict] = {}  # по id: закреплённый пост может прийти дважды
    pages = 0
    for page_no in range(settings.max_pages):
        items = fetch_page(page_no * settings.page_size, settings.page_size)
        pages += 1
        stop = False
        for post in items:
            published = datetime.fromtimestamp(post["date"], tz=UTC)
            is_new = watermark is not None and post["id"] > watermark
            if is_new or published >= cutoff:
                posts[post["id"]] = post
            elif not post.get("is_pinned"):
                stop = True
                break
        if stop or len(items) < settings.page_size:
            break
    else:
        log.warning(
            "Достигнут лимит в %d страниц, более старые посты не просмотрены. "
            "Если это первая загрузка, уменьшите initial_lookback_days или увеличьте max_pages.",
            settings.max_pages,
        )
    return list(posts.values()), pages


def resolve_source(client, conn, source: dict) -> dict:
    """Узнаёт числовой id сообщества при первой загрузке и сохраняет его."""
    if source.get("external_id"):
        return source
    group = client.get_group(source["handle"])
    if group.get("deactivated"):
        raise SourceUnavailable(f"{source['handle']}: сообщество {group['deactivated']}")
    if group.get("is_closed"):
        raise SourceUnavailable(f"{source['handle']}: сообщество закрытое, стена недоступна")
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE meta.sources SET external_id = %s, title = %s, updated_at = now() WHERE source_id = %s",
            (group["id"], group.get("name"), source["source_id"]),
        )
    return {**source, "external_id": group["id"], "title": group.get("name")}


_UPSERT_SQL = """
INSERT INTO raw.vk_posts (
    owner_id, post_id, source_id, published_at, text, repost_text,
    url, is_pinned, payload, content_hash, ingest_run_id
)
VALUES %s
ON CONFLICT (owner_id, post_id) DO UPDATE
   SET source_id     = EXCLUDED.source_id,
       published_at  = EXCLUDED.published_at,
       text          = EXCLUDED.text,
       repost_text   = EXCLUDED.repost_text,
       url           = EXCLUDED.url,
       is_pinned     = EXCLUDED.is_pinned,
       payload       = EXCLUDED.payload,
       content_hash  = EXCLUDED.content_hash,
       ingest_run_id = EXCLUDED.ingest_run_id,
       changed_at    = now()
 WHERE raw.vk_posts.content_hash IS DISTINCT FROM EXCLUDED.content_hash
RETURNING (xmax = 0) AS inserted
"""
# RETURNING возвращает только вставленные и реально обновлённые строки.
# xmax = 0 у свежевставленной строки и не 0 у обновлённой — так их и различаем.


def _to_row(post: dict, owner_id: int, source_id: int, run_id: str) -> tuple:
    text, repost_text = post_texts(post)
    return (
        owner_id,
        post["id"],
        source_id,
        datetime.fromtimestamp(post["date"], tz=UTC),
        text,
        repost_text,
        f"https://vk.com/wall{owner_id}_{post['id']}",
        bool(post.get("is_pinned")),
        Json(sanitize_payload(post)),
        content_hash(text, repost_text),
        run_id,
    )


def ingest_source(
    client,
    conn,
    source: dict,
    *,
    run_id: str,
    settings: IngestSettings | None = None,
    now: datetime | None = None,
) -> IngestResult:
    settings = settings or IngestSettings.from_overrides(source.get("settings"))
    now = now or datetime.now(UTC)
    owner_id = -abs(int(source["external_id"]))

    with conn, conn.cursor() as cur:
        cur.execute("SELECT max(post_id) FROM raw.vk_posts WHERE owner_id = %s", (owner_id,))
        watermark = cur.fetchone()[0]

    days = settings.initial_lookback_days if watermark is None else settings.recheck_days
    cutoff = now - timedelta(days=days)

    posts, pages = collect_posts(
        lambda offset, count: client.wall_get(owner_id, offset=offset, count=count, wall_filter=settings.wall_filter),
        watermark=watermark,
        cutoff=cutoff,
        settings=settings,
    )
    result = IngestResult(pages=pages, fetched=len(posts))
    if not posts:
        return result

    rows = [_to_row(p, owner_id, source["source_id"], run_id) for p in posts]
    with conn, conn.cursor() as cur:
        returned = execute_values(cur, _UPSERT_SQL, rows, fetch=True)
    result.inserted = sum(1 for (inserted,) in returned if inserted)
    result.updated = len(returned) - result.inserted
    log.info(
        "%s: страниц %d, получено %d, новых %d, изменённых %d (водяной знак %s)",
        source["handle"],
        result.pages,
        result.fetched,
        result.inserted,
        result.updated,
        watermark,
    )
    return result
