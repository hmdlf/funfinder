"""deploy_site: собранная папка → сервер по SSH, с атомарным переключением версий.

Раскладка на сервере:

    /var/www/matingu/
      releases/20261113-213000/   каждая выкладка — отдельная папка
      releases/20261113-180500/
      current -> releases/20261113-213000   nginx отдаёт отсюда

Новая версия заливается в свою папку целиком, потом ссылка current
переключается одной командой (mv -T атомарен). Посетитель никогда не видит
полузалитый сайт, а откатиться можно, переставив ссылку на прошлую папку.
Хранятся последние KEEP_RELEASES версий.

Ключ сервера сверяется с known_hosts: подменённый сервер ключ не примет.
"""

import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry

log = logging.getLogger(__name__)

KEEP_RELEASES = 5


def release_name(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")


def switch_command(remote_dir: str, release: str, keep: int = KEEP_RELEASES) -> str:
    """Shell-команда: переключить current на release и удалить старые версии."""
    base, rel = shlex.quote(remote_dir), shlex.quote(f"releases/{release}")
    return (
        f"set -e; cd {base}; "
        f"test -d {rel}; "
        f"ln -sfn {rel} current.new; "
        f"mv -Tf current.new current; "
        # Имена версий — даты, поэтому сортировка по имени = по времени.
        f"ls -1d releases/*/ | sort -r | tail -n +{keep + 1} | xargs -r rm -rf --"
    )


def load_known_hosts(client: paramiko.SSHClient, path: Path) -> None:
    """Загружает known_hosts. PowerShell по умолчанию пишет файлы в UTF-16 — читаем и так."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
    keys = client.get_host_keys()
    loaded = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry = HostKeyEntry.from_line(line)
        if entry is None:
            continue
        for host in entry.hostnames:
            keys.add(host, entry.key.get_name(), entry.key)
            loaded += 1
    if not loaded:
        raise ValueError(f"В {path} нет ни одного ключа сервера")


def upload_tree(sftp, local_dir: Path, remote_dir: str) -> int:
    """Заливает папку целиком. Возвращает число файлов."""
    local_dir = Path(local_dir)
    sftp.mkdir(remote_dir)
    files = 0
    for path in sorted(local_dir.rglob("*")):
        target = f"{remote_dir}/{path.relative_to(local_dir).as_posix()}"
        if path.is_dir():
            sftp.mkdir(target)
        else:
            sftp.put(str(path), target)
            files += 1
    return files


def _run(client: paramiko.SSHClient, command: str) -> None:
    _, stdout, stderr = client.exec_command(command, timeout=60)
    status = stdout.channel.recv_exit_status()
    if status != 0:
        raise RuntimeError(f"Команда на сервере завершилась с кодом {status}: {stderr.read().decode().strip()}")


def connect(host: str, *, user: str, key_path: Path, known_hosts_path: Path, port: int = 22) -> paramiko.SSHClient:
    """SSH-соединение с сервером только по ключу и только если ключ сервера есть в known_hosts.

    Общее для выкладки сайта и для загрузки Telegram через сервер (funfinder.tg.fetch).
    """
    client = paramiko.SSHClient()
    load_known_hosts(client, known_hosts_path)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        host,
        port=port,
        username=user,
        key_filename=str(key_path),
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    return client


def deploy(
    local_dir: Path,
    *,
    host: str,
    user: str,
    key_path: Path,
    known_hosts_path: Path,
    remote_dir: str,
    port: int = 22,
    now: datetime | None = None,
) -> dict:
    local_dir = Path(local_dir)
    if not (local_dir / "index.html").exists():
        raise FileNotFoundError(f"В {local_dir} нет собранного сайта")

    client = connect(host, user=user, key_path=key_path, known_hosts_path=known_hosts_path, port=port)
    try:
        release = release_name(now)
        with client.open_sftp() as sftp:
            try:
                sftp.mkdir(f"{remote_dir}/releases")
            except OSError:
                pass  # уже есть
            files = upload_tree(sftp, local_dir, f"{remote_dir}/releases/{release}")
        _run(client, switch_command(remote_dir, release))
    finally:
        client.close()

    log.info("Выложено %d файлов в %s:%s/releases/%s", files, host, remote_dir, release)
    return {"release": release, "files": files}
