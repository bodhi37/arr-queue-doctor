#!/usr/bin/env python3
"""Diagnose stuck Sonarr/Radarr queue items and re-search healthy replacements.

Every run:

1. Lists qBittorrent jobs and the Sonarr/Radarr queue.
2. Finds jobs stuck fetching metadata, stalled with zero progress, or
   completed but containing no playable media (or containing executables).
3. Removes the stuck queue item (blocklisted, without deleting the library),
   then either grabs the healthiest already-indexed release or triggers a
   fresh search.
4. Appends one JSON line per recovery to the event log.

Replacement picking uses the exact reported seeder count after your
quality/profile score, instead of the Arrs' coarse seeder buckets.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} must be an integer")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


QBIT_URL = _env("QBIT_URL", "http://127.0.0.1:8080/api/v2")
SONARR_URL = _env("SONARR_URL", "http://127.0.0.1:8989/api/v3")
RADARR_URL = _env("RADARR_URL", "http://127.0.0.1:7878/api/v3")
SONARR_CONFIG = _env("SONARR_CONFIG", "/var/lib/sonarr/config.xml")
RADARR_CONFIG = _env("RADARR_CONFIG", "/var/lib/radarr/config.xml")
META_TIMEOUT = _env_int("ARR_QUEUE_DOCTOR_META_TIMEOUT", 30 * 60)
STALL_TIMEOUT = _env_int("ARR_QUEUE_DOCTOR_STALL_TIMEOUT", 90 * 60)
MAX_RECOVERIES = _env_int("ARR_QUEUE_DOCTOR_MAX_RECOVERIES", 2)
HTTP_TIMEOUT = _env_int("ARR_QUEUE_DOCTOR_HTTP_TIMEOUT", 90)
DRY_RUN = _env("ARR_QUEUE_DOCTOR_DRY_RUN", "0") == "1"
LOG = pathlib.Path(
    _env(
        "ARR_QUEUE_DOCTOR_LOG",
        "/var/lib/arr-queue-doctor/recovery-events.jsonl",
    )
)
SKIP_TAGS = frozenset(
    tag.strip()
    for tag in _env(
        "ARR_QUEUE_DOCTOR_SKIP_TAGS",
        "route_error,route_import_failed,route_overcommit,route_waiting_space",
    ).split(",")
    if tag.strip()
)

MEDIA_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv"}
)
DANGEROUS_EXTENSIONS = frozenset(
    {".bat", ".cmd", ".com", ".exe", ".jar", ".msi", ".ps1", ".scr", ".sh"}
)


def api_key(app: str, config_path: str, key_env: str) -> str:
    """Return the Arr API key from env, falling back to config.xml."""
    direct = os.environ.get(key_env, "").strip()
    if direct:
        return direct
    try:
        value = ET.parse(config_path).getroot().findtext("ApiKey")
    except (OSError, ET.ParseError) as error:
        raise RuntimeError(
            f"cannot read API key for {app} from {config_path}: {error}. "
            f"Set {key_env} or fix {config_path}."
        ) from error
    if not value:
        raise RuntimeError(
            f"missing API key for {app}: set {key_env} or check {config_path}"
        )
    return value


def request(method: str, url: str, key: str | None = None, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"X-Api-Key": key} if key else {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(
            f"{method} {url} failed HTTP {error.code}: {body[:1200]}"
        ) from error
    return json.loads(body) if body else None


class Arr:
    def __init__(self, name: str, base: str, key: str):
        self.name = name
        self.base = base.rstrip("/")
        self.key = key

    def get(self, path: str):
        return request("GET", f"{self.base}/{path.lstrip('/')}", self.key)

    def post(self, path: str, payload):
        if DRY_RUN:
            return {"dry_run": True}
        return request("POST", f"{self.base}/{path.lstrip('/')}", self.key, payload)

    def delete(self, path: str):
        if DRY_RUN:
            return None
        return request("DELETE", f"{self.base}/{path.lstrip('/')}", self.key)


def write_event(event: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    os.chmod(LOG, 0o600)


def acceptable(release: dict) -> bool:
    return (
        release.get("protocol") == "torrent"
        and _safe_int(release.get("seeders"), 0) > 0
        and not release.get("rejections")
    )


def unsafe_payload(torrent_hash: str) -> str | None:
    """Return a recovery reason for completed torrents that cannot be media."""
    files = request(
        "GET",
        f"{QBIT_URL}/torrents/files?hash={urllib.parse.quote(torrent_hash)}",
    )
    suffixes = {
        pathlib.PurePosixPath(item.get("name") or "").suffix.lower()
        for item in files
        if item.get("name")
    }
    dangerous = sorted(suffixes & DANGEROUS_EXTENSIONS)
    if dangerous:
        return f"dangerous_payload:{','.join(dangerous)}"
    if not suffixes & MEDIA_EXTENSIONS:
        return "no_media_payload"
    return None


def best_sonarr_release(arr: Arr, episode_id: int):
    releases = arr.get(f"release?episodeId={episode_id}")
    candidates = [release for release in releases if acceptable(release)]
    if not candidates:
        return None
    # Your profile has already rejected junk and scored release shape and
    # resolution. Use the exact reported seed count after that policy score
    # instead of the Arr's native order-of-magnitude seeder buckets.
    return max(
        candidates,
        key=lambda release: (
            int(release.get("customFormatScore") or 0),
            int(release.get("seeders") or 0),
            int(release.get("leechers") or 0),
        ),
    )


def best_radarr_release(arr: Arr, movie_id: int):
    releases = arr.get(f"release?movieId={movie_id}")
    candidates = [release for release in releases if acceptable(release)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda release: (
            int(
                release.get("quality", {})
                .get("quality", {})
                .get("resolution", 0)
                == 1080
            ),
            int(release.get("customFormatScore") or 0),
            int(release.get("seeders") or 0),
            int(release.get("leechers") or 0),
        ),
    )


def recover(arr: Arr, records: list[dict], torrent: dict) -> dict:
    result = {
        "app": arr.name,
        "hash": torrent["hash"],
        "old_title": torrent.get("name"),
        "old_state": torrent.get("state"),
        "age_seconds": int(time.time()) - int(torrent.get("added_on") or 0),
    }
    if DRY_RUN:
        result["dry_run"] = True
    if arr.name == "sonarr":
        episode_ids = sorted(
            {int(item["episodeId"]) for item in records if item.get("episodeId")}
        )
        if not episode_ids:
            result["action"] = "skipped_missing_ids"
            return result
        seasons = {
            (item.get("seriesId"), (item.get("episode") or {}).get("seasonNumber"))
            for item in records
        }
    else:
        movie_ids = sorted(
            {int(item["movieId"]) for item in records if item.get("movieId")}
        )
        if not movie_ids:
            result["action"] = "skipped_missing_ids"
            return result

    first = records[0]
    params = urllib.parse.urlencode(
        {
            "removeFromClient": "true",
            "blocklist": "true",
            "skipRedownload": "true",
            "changeCategory": "false",
        }
    )
    arr.delete(f"queue/{first['id']}?{params}")
    if not DRY_RUN:
        time.sleep(2)

    if arr.name == "sonarr":
        if len(episode_ids) <= 2:
            release = best_sonarr_release(arr, episode_ids[0])
            if release:
                arr.post("release", release)
                result.update(
                    {
                        "action": "exact_seeded_release",
                        "new_title": release.get("title"),
                        "reported_seeders": release.get("seeders"),
                        "custom_format_score": release.get("customFormatScore"),
                    }
                )
                return result
            arr.post("command", {"name": "EpisodeSearch", "episodeIds": episode_ids})
            result["action"] = "episode_search"
            return result
        if len(seasons) == 1:
            series_id, season_number = seasons.pop()
            arr.post(
                "command",
                {
                    "name": "SeasonSearch",
                    "seriesId": series_id,
                    "seasonNumber": season_number,
                },
            )
            result["action"] = "season_search"
            return result
        arr.post("command", {"name": "EpisodeSearch", "episodeIds": episode_ids})
        result["action"] = "episode_search"
        return result

    release = best_radarr_release(arr, movie_ids[0])
    if release:
        arr.post("release", release)
        result.update(
            {
                "action": "exact_seeded_release",
                "new_title": release.get("title"),
                "reported_seeders": release.get("seeders"),
                "custom_format_score": release.get("customFormatScore"),
            }
        )
        return result
    arr.post("command", {"name": "MoviesSearch", "movieIds": movie_ids})
    result["action"] = "movie_search"
    return result


def main() -> int:
    sonarr = Arr("sonarr", SONARR_URL, api_key("sonarr", SONARR_CONFIG, "SONARR_API_KEY"))
    radarr = Arr("radarr", RADARR_URL, api_key("radarr", RADARR_CONFIG, "RADARR_API_KEY"))
    now = int(time.time())
    torrents = request("GET", f"{QBIT_URL}/torrents/info")
    by_hash = {item["hash"].lower(): item for item in torrents}
    candidates = []

    for arr in (sonarr, radarr):
        queue = arr.get(
            "queue?page=1&pageSize=1000&includeSeries=true&includeEpisode=true"
        )
        grouped: dict[str, list[dict]] = {}
        for item in queue.get("records", []):
            download_id = (item.get("downloadId") or "").lower()
            if download_id:
                grouped.setdefault(download_id, []).append(item)
        for download_id, records in grouped.items():
            torrent = by_hash.get(download_id)
            if not torrent:
                continue
            torrent_tags = {
                tag.strip()
                for tag in (torrent.get("tags") or "").split(",")
                if tag.strip()
            }
            if torrent_tags & SKIP_TAGS:
                continue
            age = now - int(torrent.get("added_on") or now)
            stale_metadata = torrent.get("state") == "metaDL" and age >= META_TIMEOUT
            stale_payload = (
                torrent.get("state") == "stalledDL"
                and float(torrent.get("progress") or 0) <= 0.001
                and age >= STALL_TIMEOUT
            )
            payload_reason = None
            if float(torrent.get("progress") or 0) >= 1:
                payload_reason = unsafe_payload(download_id)
            if stale_metadata or stale_payload or payload_reason:
                reason = payload_reason or (
                    "stale_metadata" if stale_metadata else "stale_payload"
                )
                candidates.append((age, arr, records, torrent, reason))

    recovered = 0
    for _, arr, records, torrent, reason in sorted(
        candidates, key=lambda item: item[0], reverse=True
    )[:MAX_RECOVERIES]:
        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "reason": reason,
        }
        try:
            event.update(recover(arr, records, torrent))
            recovered += 1
        except Exception as error:  # keep the other recovery independent
            event.update(
                {
                    "app": arr.name,
                    "hash": torrent.get("hash"),
                    "old_title": torrent.get("name"),
                    "action": "error",
                    "error": str(error)[:1200],
                }
            )
        write_event(event)

    print(f"candidates={len(candidates)} recovered={recovered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
