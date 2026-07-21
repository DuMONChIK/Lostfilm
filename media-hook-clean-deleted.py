#!/usr/bin/env python3

"""Обновление медиатеки из каталога загрузок.

Скрипт создаёт символические ссылки на AVI/MP4/MKV, раскладывает серии
по каталогам сезонов, удаляет элементы медиатеки с битыми ссылками и
чистит пустые каталоги.

Весь вывод stdout/stderr дописывается в LOG_FILE.
"""

from __future__ import annotations

import errno
import grp
import logging
import os
import pwd
import re
import shutil
import stat
import sys
from pathlib import Path


DOWNLOAD_DIR = Path("/opt/torrents/downloads")
MEDIA_PATH = Path("/opt/torrents/media/MyMediaLib")
LOG_FILE = Path("/var/log/media-hook.log")

OWNER = "jellyfin"
GROUP = "jellyfin"

MEDIA_EXTENSIONS = {".avi", ".mp4", ".mkv"}
EPISODE_RE = re.compile(r"S([0-9]{2})E([0-9]{2})", re.IGNORECASE)
SEASON_DIR_RE = re.compile(
    r"(?:^|[/\\])(?:[0-9]{1,2}[._ -]?(?:season|sezon)|"
    r"(?:season|sezon)[._ -]?[0-9]{1,2})(?:[/\\]|$)",
    re.IGNORECASE,
)
LOSTFILM_DIR_RE = re.compile(
    r"^(.*) [0-9]+ - LostFilm\.TV.*$",
    re.IGNORECASE,
)

LOGGER = logging.getLogger("media-hook")


def redirect_stdout_stderr(log_file: Path):
    """Перенаправить stdout и stderr процесса в один файл в режиме append."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stream = log_file.open("a", encoding="utf-8", buffering=1)

    os.dup2(stream.fileno(), sys.stdout.fileno())
    os.dup2(stream.fileno(), sys.stderr.fileno())

    # После dup2 старые TextIOWrapper продолжают работать с дескрипторами 1/2.
    # Включаем построчную запись, чтобы свежие строки сразу появлялись в логе.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True, write_through=True)

    return stream


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def iter_download_files(download_dir: Path):
    """Выдать обычные файлы, как `find -type f`, исключив пути с content."""

    def on_walk_error(error: OSError) -> None:
        LOGGER.error("Не удалось прочитать каталог: %s", error)

    for dir_path, dir_names, file_names in os.walk(
        download_dir,
        topdown=True,
        onerror=on_walk_error,
        followlinks=False,
    ):
        dir_names.sort()
        file_names.sort()

        for file_name in file_names:
            file_path = Path(dir_path, file_name)

            # Аналог `grep -vi content`: проверяется весь абсолютный путь.
            if "content" in str(file_path).casefold():
                continue

            try:
                if not stat.S_ISREG(file_path.lstat().st_mode):
                    continue
            except OSError as error:
                LOGGER.error("Не удалось проверить файл [%s]: %s", file_path, error)
                continue

            yield file_path


def destination_for(source: Path, download_dir: Path, media_path: Path) -> tuple[Path, str | None]:
    relative = source.relative_to(download_dir)

    # Для файла непосредственно в DOWNLOAD_DIR исходный Bash-скрипт создавал
    # отдельный каталог с именем файла без расширения.
    if relative.parent == Path("."):
        destination = media_path / relative.stem
    else:
        destination = media_path / relative.parent

    episode_match = EPISODE_RE.search(source.name)
    season = episode_match.group(1) if episode_match else None

    # Если исходный путь уже содержит каталог сезона, второй уровень сезона
    # добавлять не нужно.
    if season is not None and SEASON_DIR_RE.search(str(destination)):
        season = None

    # Убираем суффикс вида " 1 - LostFilm.TV [качество]" из каталога.
    lostfilm_match = LOSTFILM_DIR_RE.match(str(destination))
    if lostfilm_match and lostfilm_match.group(1):
        destination = Path(lostfilm_match.group(1))

    if season is not None:
        destination /= f"{season}.season"

    return destination, season


def create_media_link(source: Path, destination: Path) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    link_path = destination / source.name

    # lexists() учитывает и битые ссылки. Битую ссылку с таким же именем
    # можно безопасно заменить сразу, не откладывая это до следующего запуска.
    if os.path.lexists(link_path):
        if link_path.is_symlink() and not link_path.exists():
            link_path.unlink()
            LOGGER.info("Удалена битая ссылка перед заменой: [%s]", link_path)
        else:
            return False

    link_path.symlink_to(source)
    LOGGER.info("Добавлена ссылка: [%s] -> [%s]", link_path, source)
    return True


def resolve_owner(owner: str, group: str) -> tuple[int, int] | None:
    try:
        uid = pwd.getpwnam(owner).pw_uid
        gid = grp.getgrnam(group).gr_gid
    except KeyError as error:
        LOGGER.error(
            "Пользователь или группа для chown не найдены (%s:%s): %s. "
            "Обновление владельца пропущено.",
            owner,
            group,
            error,
        )
        return None

    return uid, gid


def chown_one(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError as error:
        LOGGER.error("Не удалось изменить владельца [%s]: %s", path, error)


def chown_tree(root: Path, uid: int, gid: int) -> None:
    """Аналог `chown -R`, не переходящий по символическим ссылкам."""
    chown_one(root, uid, gid)

    def on_walk_error(error: OSError) -> None:
        LOGGER.error("Не удалось обойти каталог для chown: %s", error)

    for dir_path, dir_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=on_walk_error,
        followlinks=False,
    ):
        for name in dir_names:
            chown_one(Path(dir_path, name), uid, gid)
        for name in file_names:
            chown_one(Path(dir_path, name), uid, gid)


def find_broken_link_roots(media_path: Path) -> dict[Path, Path]:
    """Вернуть корневой элемент медиатеки для каждой группы битых ссылок."""
    roots: dict[Path, Path] = {}

    def on_walk_error(error: OSError) -> None:
        LOGGER.error("Не удалось проверить медиатеку: %s", error)

    for dir_path, dir_names, file_names in os.walk(
        media_path,
        topdown=True,
        onerror=on_walk_error,
        followlinks=False,
    ):
        for name in [*dir_names, *file_names]:
            candidate = Path(dir_path, name)

            try:
                broken = candidate.is_symlink() and not candidate.exists()
            except OSError as error:
                LOGGER.error("Не удалось проверить ссылку [%s]: %s", candidate, error)
                continue

            if not broken:
                continue

            relative = candidate.relative_to(media_path)
            if not relative.parts:
                continue

            top_level = media_path / relative.parts[0]
            roots.setdefault(top_level, candidate)

    return roots


def remove_path(path: Path) -> None:
    """Удалить путь рекурсивно, не переходя по символической ссылке."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def remove_entries_with_broken_links(media_path: Path) -> int:
    """Сохранить поведение Bash: удалить весь верхний элемент с битой ссылкой."""
    removed = 0

    for top_level, broken_link in sorted(
        find_broken_link_roots(media_path).items(),
        key=lambda item: str(item[0]),
    ):
        LOGGER.warning(
            "Обнаружена битая ссылка [%s]; удаляется элемент медиатеки [%s]",
            broken_link,
            top_level,
        )
        try:
            remove_path(top_level)
            removed += 1
        except OSError:
            LOGGER.exception("Не удалось удалить [%s]", top_level)

    return removed


def remove_empty_directories(media_path: Path) -> int:
    removed = 0

    for dir_path, _, _ in os.walk(media_path, topdown=False, followlinks=False):
        directory = Path(dir_path)

        # В отличие от `find ... -empty -delete`, сохраняем сам корень
        # медиатеки, даже если он временно пуст.
        if directory == media_path:
            continue

        try:
            directory.rmdir()
            LOGGER.info("Удалён пустой каталог: [%s]", directory)
            removed += 1
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                LOGGER.error("Не удалось удалить пустой каталог [%s]: %s", directory, error)

    return removed


def update_media_library() -> int:
    if not DOWNLOAD_DIR.is_dir():
        LOGGER.error("Каталог загрузок не найден: [%s]", DOWNLOAD_DIR)
        return 1

    MEDIA_PATH.mkdir(parents=True, exist_ok=True)
    owner_ids = resolve_owner(OWNER, GROUP)

    found_media = 0
    links_added = 0

    for source in iter_download_files(DOWNLOAD_DIR):
        if source.suffix.casefold() not in MEDIA_EXTENSIONS:
            continue

        found_media += 1

        try:
            destination, _ = destination_for(source, DOWNLOAD_DIR, MEDIA_PATH)
            if create_media_link(source, destination):
                links_added += 1
#^chown
#            if owner_ids is not None:
#                chown_tree(destination, *owner_ids)
        except OSError:
            LOGGER.exception("Ошибка при обработке файла [%s]", source)

    entries_removed = remove_entries_with_broken_links(MEDIA_PATH)
    empty_dirs_removed = remove_empty_directories(MEDIA_PATH)

    LOGGER.info(
        "Завершено: найдено медиафайлов=%d, добавлено ссылок=%d, "
        "удалено элементов=%d, удалено пустых каталогов=%d",
        found_media,
        links_added,
        entries_removed,
        empty_dirs_removed,
    )
    return 0


def main() -> int:
    try:
        log_stream = redirect_stdout_stderr(LOG_FILE)
    except OSError as error:
        print(f"Не удалось открыть лог-файл [{LOG_FILE}]: {error}", file=sys.stderr)
        return 1

    # Ссылка нужна до завершения main(), чтобы исходный дескриптор не закрылся.
    _ = log_stream
    configure_logging()
    LOGGER.info("Запуск обновления медиатеки")

    try:
        return update_media_library()
    except Exception:
        LOGGER.exception("Необработанная ошибка")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
