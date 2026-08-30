#!/usr/bin/env python3
"""Send only the newest WiGLE upload's recent Wi-Fi observations to WDG.

This script:
  1. Requests exactly one WiGLE transaction: the newest upload.
  2. Downloads only that transaction's original CSV.
  3. Keeps only Wi-Fi rows whose FirstSeen timestamp is in the rolling
     last-N-hours window (24 hours by default and never more than 24).
  4. Skips a transaction already accepted by WDG from this installation.
  5. Uploads a valid WiGLE 1.6 CSV.gz file to WDG's asynchronous v2 API.

It uses only the Python standard library and is intended for Windows 10/11.
Keep the .env file private; it contains credentials.
"""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import http.client
import io
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "1.0.0-beta.1"
USER_AGENT = f"wigle-to-wdg/{VERSION} (Windows; Python {platform.python_version()})"

WIGLE_API_BASE = "https://api.wigle.net/api/v2"
WDG_SITE_BASE = "https://wdgwars.pl"

DEFAULT_HOURS = 24.0
MAX_HOURS = 24.0
STATE_RETENTION_DAYS = 8
MAX_WDG_UPLOAD_BYTES = 30 * 1024 * 1024
WDG_SAFE_UPLOAD_BYTES = 29 * 1024 * 1024
WDG_POLL_INTERVAL_SECONDS = 3.0
WDG_POLL_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = SCRIPT_DIR / ".env"
DEFAULT_STATE_PATH = SCRIPT_DIR / ".wigle_to_wdg_state.json"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "out"
DEFAULT_LOG_PATH = SCRIPT_DIR / "wigle_to_wdg.log"

WIGLE_16_COLUMNS = [
    "MAC",
    "SSID",
    "AuthMode",
    "FirstSeen",
    "Channel",
    "Frequency",
    "RSSI",
    "CurrentLatitude",
    "CurrentLongitude",
    "AltitudeMeters",
    "AccuracyMeters",
    "RCOIs",
    "MfgrId",
    "Type",
]

NOT_READY_STATUSES = {
    "W", "I", "T", "S", "A", "C", "G",
    "QUEUED", "PARSING", "TRILATERATING", "STATS", "ARCHIVE",
    "CATALOG", "GEOINDEX", "PROCESSING", "RUNNING",
}
FAILED_STATUSES = {"E", "F", "ERROR", "FAILED", "FAIL"}
WIFI_TYPES = {"", "WIFI", "WI-FI", "WLAN", "AP", "ACCESS_POINT"}


class TransferError(RuntimeError):
    """A user-facing transfer failure."""


class HttpRequestError(TransferError):
    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Credentials:
    wigle_authorization: str
    wdg_api_key: str


@dataclass
class FilterStats:
    source_rows: int = 0
    kept_rows: int = 0
    duplicate_rows_in_run: int = 0
    already_sent_rows: int = 0
    too_old_rows: int = 0
    future_rows: int = 0
    bad_timestamp_rows: int = 0
    non_wifi_rows: int = 0
    no_gps_rows: int = 0
    malformed_rows: int = 0

    def merge(self, other: "FilterStats") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class PreparedRow:
    values: dict[str, str]
    fingerprint: str


@dataclass
class PreparedBatch:
    csv_path: Path
    gzip_path: Path
    rows: list[PreparedRow]
    cutoff: datetime
    end: datetime
    stats: FilterStats
    source_transactions: list[str] = field(default_factory=list)


@dataclass
class WdgResult:
    ok: bool
    job_id: int | None = None
    status: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class Logger:
    def __init__(self, path: Path, verbose: bool = False) -> None:
        self.path = path
        self.verbose = verbose
        path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        line = f"[{now}] {level}: {message}"
        print(line)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def debug(self, message: str) -> None:
        if self.verbose:
            self._write("DEBUG", message)


def load_env_file(path: Path) -> None:
    """Load a minimal .env file without third-party dependencies."""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TransferError(f"Could not read {path}: {exc}") from exc

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise TransferError(f"Invalid .env line {line_number}: expected NAME=value")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise TransferError(f"Invalid .env line {line_number}: empty variable name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or any(
        token in lowered
        for token in ("replace_me", "your_", "paste_", "example", "changeme", "<", ">")
    )


def _combined_wigle_authorization(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("basic "):
        value = value[6:].strip()

    if ":" in value:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"

    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise TransferError(
            "The combined WiGLE credential must be either API_NAME:API_TOKEN "
            "or the Base64 value WiGLE supplies for Basic authentication."
        ) from exc
    if ":" not in decoded:
        raise TransferError(
            "The combined WiGLE credential decoded successfully but did not contain API_NAME:API_TOKEN."
        )
    return f"Basic {value}"


def read_credentials(*, require_wdg: bool = True) -> Credentials:
    combined = _first_env(
        "WIGLE_BASIC_TOKEN",
        "WIGLE_AUTH_TOKEN",
        "WIGLE_API_KEY",
    )
    api_name = _first_env("WIGLE_API_NAME", "WIGLE_API_USERNAME")
    api_token = _first_env("WIGLE_API_TOKEN")
    wdg_key = _first_env("WDG_API_KEY", "WDGWARS_API_KEY")

    # A common WiGLE copy/paste is one Base64 string. Accept it even when a
    # user placed it in WIGLE_API_TOKEN without an API name.
    if not combined and not api_name and api_token:
        try:
            combined = api_token
            wigle_authorization = _combined_wigle_authorization(combined)
        except TransferError:
            wigle_authorization = ""
    elif combined:
        wigle_authorization = _combined_wigle_authorization(combined)
    else:
        wigle_authorization = ""

    if not wigle_authorization:
        if _looks_like_placeholder(api_name) or _looks_like_placeholder(api_token):
            raise TransferError(
                "Missing WiGLE credentials. Set WIGLE_BASIC_TOKEN, or set both "
                "WIGLE_API_NAME and WIGLE_API_TOKEN in .env."
            )
        encoded = base64.b64encode(f"{api_name}:{api_token}".encode("utf-8")).decode("ascii")
        wigle_authorization = f"Basic {encoded}"

    if _looks_like_placeholder(wdg_key):
        if require_wdg:
            raise TransferError("Missing WDG_API_KEY in .env.")
        wdg_key = ""

    return Credentials(wigle_authorization=wigle_authorization, wdg_api_key=wdg_key)


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _error_message_from_body(body: str, fallback: str) -> str:
    try:
        payload = json.loads(body)
    except Exception:
        return fallback
    if isinstance(payload, Mapping):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if value:
                return str(value)
    return fallback


def request_bytes(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    attempts: int = 3,
    logger: Logger | None = None,
) -> tuple[int, Mapping[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = int(getattr(response, "status", 200))
                response_headers = dict(response.headers.items())
                body = response.read()
                return status, response_headers, body
        except urllib.error.HTTPError as exc:
            body = _read_http_error_body(exc)
            message = _error_message_from_body(body, str(exc.reason))
            if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt >= attempts:
                raise HttpRequestError(
                    f"HTTP {exc.code} from {redact_url(url)}: {message}",
                    status=exc.code,
                    body=body,
                ) from exc
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            try:
                delay = max(float(retry_after), float(2 ** (attempt - 1)))
            except ValueError:
                delay = float(2 ** (attempt - 1))
            if logger:
                logger.warning(f"Temporary HTTP {exc.code}; retrying request (attempt {attempt + 1}).")
            time.sleep(min(delay, 30.0))
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                reason = getattr(exc, "reason", exc)
                raise HttpRequestError(
                    f"Request to {redact_url(url)} failed: {reason}"
                ) from exc
            if logger:
                logger.warning(f"Network request failed; retrying (attempt {attempt + 1}).")
            time.sleep(float(2 ** (attempt - 1)))
            last_error = exc

    raise HttpRequestError(f"Request to {redact_url(url)} failed: {last_error}")


def request_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    attempts: int = 3,
    logger: Logger | None = None,
) -> dict[str, Any]:
    _, _, body = request_bytes(
        url,
        headers=headers,
        timeout=timeout,
        attempts=attempts,
        logger=logger,
    )
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        excerpt = body[:200].decode("utf-8", errors="replace")
        raise TransferError(f"Expected JSON from {redact_url(url)}, received: {excerpt!r}") from exc
    if not isinstance(payload, dict):
        raise TransferError(f"Expected a JSON object from {redact_url(url)}.")
    return payload


def parse_timestamp(value: Any, *, naive_timezone: timezone = timezone.utc) -> datetime | None:
    """Parse WiGLE/API timestamps and return an aware UTC datetime."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Unix seconds or milliseconds occasionally appear in API wrappers.
    try:
        numeric = float(raw)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        if numeric > 1_000_000_000:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except ValueError:
        pass

    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(timezone.utc)


def fetch_latest_wigle_transaction(
    authorization: str,
    *,
    logger: Logger,
) -> dict[str, Any] | None:
    """Query exactly one transaction and return WiGLE's newest result."""
    headers = {"Authorization": authorization, "Accept": "application/json"}
    query = urllib.parse.urlencode({"pagestart": 0, "pageend": 1})
    payload = request_json(
        f"{WIGLE_API_BASE}/file/transactions?{query}",
        headers=headers,
        logger=logger,
    )
    if payload.get("success") is False:
        message = payload.get("message") or payload.get("error") or "WiGLE returned success=false"
        raise TransferError(f"WiGLE transaction request failed: {message}")

    results = payload.get("results") or []
    if not isinstance(results, list):
        raise TransferError("WiGLE transaction response had an invalid results field.")
    if not results:
        return None
    item = results[0]
    if not isinstance(item, dict) or not str(item.get("transid") or "").strip():
        raise TransferError("WiGLE's newest transaction record had no transaction ID.")
    return item


def transaction_is_ready(item: Mapping[str, Any], *, logger: Logger) -> bool:
    status = str(item.get("status") or item.get("wwwdStatus") or "").strip().upper()
    if status in FAILED_STATUSES:
        logger.warning(f"Newest WiGLE transaction {item.get('transid')} failed processing ({status}).")
        return False
    if status in NOT_READY_STATUSES:
        logger.info(f"Newest WiGLE transaction {item.get('transid')} is still processing ({status}).")
        return False
    return True


def download_wigle_csv(
    transid: str,
    authorization: str,
    *,
    logger: Logger,
) -> str:
    safe_transid = urllib.parse.quote(transid, safe="")
    _, headers, body = request_bytes(
        f"{WIGLE_API_BASE}/file/csv/{safe_transid}",
        headers={"Authorization": authorization, "Accept": "text/csv,*/*"},
        timeout=120.0,
        logger=logger,
    )

    content_encoding = str(headers.get("Content-Encoding", "")).lower()
    if body.startswith(b"\x1f\x8b") or "gzip" in content_encoding:
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise TransferError(f"WiGLE returned invalid gzip data for transaction {transid}.") from exc

    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("latin-1")

    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("message") or payload.get("error") or "unexpected JSON response"
        raise TransferError(f"Could not download WiGLE transaction {transid}: {message}")
    return text


def _canonical_header_name(name: str) -> str:
    return name.replace("\ufeff", "").strip().lower().replace("_", "").replace(" ", "")


COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "MAC": ("mac", "bssid", "address"),
    "SSID": ("ssid", "name"),
    "AuthMode": ("authmode", "capabilities", "encryption"),
    "FirstSeen": ("firstseen", "firsttime", "time", "timestamp"),
    "Channel": ("channel",),
    "Frequency": ("frequency", "freq"),
    "RSSI": ("rssi", "level", "signal"),
    "CurrentLatitude": ("currentlatitude", "latitude", "lat"),
    "CurrentLongitude": ("currentlongitude", "longitude", "lon", "lng"),
    "AltitudeMeters": ("altitudemeters", "altitude", "alt"),
    "AccuracyMeters": ("accuracymeters", "accuracy"),
    "RCOIs": ("rcois",),
    "MfgrId": ("mfgrid", "manufacturerid"),
    "Type": ("type", "networktype"),
}


def _find_csv_header(lines: list[str]) -> tuple[int, list[str]]:
    for index, line in enumerate(lines[:25]):
        try:
            fields = next(csv.reader([line]))
        except csv.Error:
            continue
        canonical = {_canonical_header_name(field) for field in fields}
        required_groups = [
            {"mac", "bssid", "address"},
            {"firstseen", "firsttime", "time", "timestamp"},
            {"currentlatitude", "latitude", "lat"},
            {"currentlongitude", "longitude", "lon", "lng"},
        ]
        if all(canonical & group for group in required_groups):
            return index, fields
    raise TransferError("Downloaded WiGLE file did not contain a recognizable CSV header.")


def _row_lookup(row: Mapping[str, Any], output_column: str) -> str:
    canonical_row = {_canonical_header_name(str(key)): value for key, value in row.items() if key is not None}
    for alias in COLUMN_ALIASES[output_column]:
        if alias in canonical_row:
            value = canonical_row[alias]
            return "" if value is None else str(value).strip()
    return ""


def _valid_gps(lat_raw: str, lon_raw: str) -> bool:
    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    if abs(lat) < 1e-12 and abs(lon) < 1e-12:
        return False
    return True


def _normalize_mac(value: str) -> str:
    compact = "".join(ch for ch in value if ch.isalnum())
    if len(compact) == 12 and all(ch in "0123456789abcdefABCDEF" for ch in compact):
        return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).upper()
    return value.strip().upper()


def _valid_mac(value: str) -> bool:
    compact = value.replace(":", "").replace("-", "").strip()
    if len(compact) != 12 or not all(ch in "0123456789abcdefABCDEF" for ch in compact):
        return False
    return compact.upper() not in {"000000000000", "FFFFFFFFFFFF"}


def _row_fingerprint(values: Mapping[str, str]) -> str:
    canonical = [values.get(column, "").strip() for column in WIGLE_16_COLUMNS]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def filter_wigle_csv(
    text: str,
    *,
    cutoff: datetime,
    end: datetime,
    already_sent: set[str],
    run_seen: set[str],
) -> tuple[list[PreparedRow], FilterStats]:
    stats = FilterStats()
    physical_lines = text.splitlines()
    header_index, headers = _find_csv_header(physical_lines)
    csv_body = "\n".join(physical_lines[header_index + 1:])
    reader = csv.DictReader(io.StringIO(csv_body, newline=""), fieldnames=headers)
    prepared: list[PreparedRow] = []

    for raw_row in reader:
        if not raw_row or all(not str(value or "").strip() for value in raw_row.values()):
            continue
        stats.source_rows += 1
        try:
            values = {column: _row_lookup(raw_row, column) for column in WIGLE_16_COLUMNS}
        except Exception:
            stats.malformed_rows += 1
            continue

        network_type = values["Type"].strip().upper().replace(" ", "_")
        if network_type not in WIFI_TYPES:
            stats.non_wifi_rows += 1
            continue

        observed = parse_timestamp(values["FirstSeen"])
        if observed is None:
            stats.bad_timestamp_rows += 1
            continue
        if observed < cutoff:
            stats.too_old_rows += 1
            continue
        if observed > end:
            stats.future_rows += 1
            continue
        if not _valid_gps(values["CurrentLatitude"], values["CurrentLongitude"]):
            stats.no_gps_rows += 1
            continue

        values["MAC"] = _normalize_mac(values["MAC"])
        if not _valid_mac(values["MAC"]):
            stats.malformed_rows += 1
            continue
        values["FirstSeen"] = observed.strftime("%Y-%m-%d %H:%M:%S")
        values["Type"] = "WIFI"
        fingerprint = _row_fingerprint(values)

        if fingerprint in run_seen:
            stats.duplicate_rows_in_run += 1
            continue
        run_seen.add(fingerprint)
        if fingerprint in already_sent:
            stats.already_sent_rows += 1
            continue

        prepared.append(PreparedRow(values=values, fingerprint=fingerprint))
        stats.kept_rows += 1

    return prepared, stats


def load_state(path: Path, *, now: datetime, logger: Logger) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "sent": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.replace(backup)
            logger.warning(f"State file was invalid and was moved to {backup.name}.")
        except OSError:
            logger.warning("State file was invalid; starting with an empty state.")
        return {"version": 1, "sent": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("sent", {}), dict):
        return {"version": 1, "sent": {}}

    sent: dict[str, str] = {}
    retention_cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    for fingerprint, timestamp in payload.get("sent", {}).items():
        parsed = parse_timestamp(timestamp)
        if parsed is None or parsed >= retention_cutoff:
            sent[str(fingerprint)] = str(timestamp)
    payload["version"] = 1
    payload["sent"] = sent
    return payload


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _metadata_line() -> list[str]:
    return [
        "WigleWifi-1.6",
        f"appRelease=wigle-to-wdg-{VERSION}",
        f"model={platform.machine() or 'Windows-PC'}",
        f"release={platform.release()}",
        f"device={socket.gethostname()}",
        f"display=Python-{platform.python_version()}",
        "board=PC",
        "brand=generic",
        "star=Sol",
        "body=3",
        "subBody=0",
    ]


def write_batch_files(
    rows: Sequence[PreparedRow],
    *,
    output_dir: Path,
    end: datetime,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = end.strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"wigle-last-24h-{stamp}.csv"
    gzip_path = csv_path.with_suffix(csv_path.suffix + ".gz")

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(_metadata_line())
        writer.writerow(WIGLE_16_COLUMNS)
        for row in rows:
            writer.writerow([row.values.get(column, "") for column in WIGLE_16_COLUMNS])

    with csv_path.open("rb") as source, gzip.open(gzip_path, "wb", compresslevel=6) as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)

    return csv_path, gzip_path


def validate_wdg_key(api_key: str, *, logger: Logger) -> dict[str, Any]:
    payload = request_json(
        f"{WDG_SITE_BASE}/api/me",
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        logger=logger,
    )
    if not payload.get("ok"):
        message = payload.get("error") or payload.get("message") or "WDG rejected the API key"
        raise TransferError(str(message))
    return payload


def _stream_multipart_upload(
    url: str,
    *,
    api_key: str,
    file_path: Path,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise TransferError(f"Invalid upload URL: {url}")

    boundary = "----wigleToWdg" + uuid.uuid4().hex
    filename = file_path.name
    content_type = "application/gzip" if filename.lower().endswith(".gz") else "text/csv"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    content_length = len(head) + file_path.stat().st_size + len(tail)

    if content_length > MAX_WDG_UPLOAD_BYTES:
        raise TransferError(
            f"Prepared WDG upload is {content_length / (1024 * 1024):.1f} MB, "
            "which exceeds WDG's 30 MB upload cap. Reduce the run interval so each batch is smaller."
        )

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection_kwargs: dict[str, Any] = {"timeout": timeout}
    if parsed.scheme == "https":
        connection_kwargs["context"] = ssl.create_default_context()
    connection = connection_class(parsed.hostname, parsed.port, **connection_kwargs)

    try:
        connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", parsed.netloc)
        connection.putheader("User-Agent", USER_AGENT)
        connection.putheader("Accept", "application/json")
        connection.putheader("X-API-Key", api_key)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.endheaders()
        connection.send(head)
        with file_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                connection.send(chunk)
        connection.send(tail)
        response = connection.getresponse()
        body = response.read()
        headers = {key: value for key, value in response.getheaders()}
        return int(response.status), headers, body
    except (OSError, http.client.HTTPException) as exc:
        raise HttpRequestError(f"WDG upload connection failed: {exc}") from exc
    finally:
        connection.close()


def upload_wdg_v2(
    api_key: str,
    file_path: Path,
    *,
    logger: Logger,
) -> WdgResult:
    logger.info(f"Uploading {file_path.name} ({file_path.stat().st_size / 1024:.1f} KiB) to WDG.")
    last_error: Exception | None = None
    payload: dict[str, Any] | None = None

    for attempt in range(1, 4):
        try:
            status, _, body = _stream_multipart_upload(
                f"{WDG_SITE_BASE}/api/v2/upload-csv",
                api_key=api_key,
                file_path=file_path,
                timeout=180.0,
            )
            decoded = body.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(decoded)
            except json.JSONDecodeError as exc:
                raise HttpRequestError(
                    f"WDG returned HTTP {status} with non-JSON content: {decoded[:250]!r}",
                    status=status,
                    body=decoded,
                ) from exc
            if not isinstance(parsed, dict):
                raise TransferError("WDG returned an invalid response object.")
            payload = parsed
            if status >= 400:
                message = parsed.get("error") or parsed.get("message") or f"HTTP {status}"
                raise HttpRequestError(str(message), status=status, body=decoded)
            break
        except HttpRequestError as exc:
            last_error = exc
            if exc.status in {400, 401, 403, 413, 415} or attempt >= 3:
                raise
            logger.warning(f"WDG upload attempt {attempt} failed; retrying.")
            time.sleep(float((2, 8)[attempt - 1]))

    if payload is None:
        raise TransferError(f"WDG upload failed: {last_error}")
    if not payload.get("ok"):
        message = payload.get("error") or payload.get("message") or "WDG rejected the upload"
        return WdgResult(ok=False, error=str(message))

    job_id_raw = payload.get("job_id")
    if job_id_raw is None:
        return WdgResult(ok=False, error="WDG accepted the request but returned no job_id.")
    try:
        job_id = int(job_id_raw)
    except (TypeError, ValueError):
        return WdgResult(ok=False, error=f"WDG returned an invalid job_id: {job_id_raw!r}")

    poll_url = str(payload.get("poll_url") or f"/api/v2/upload-job/{job_id}")
    if not poll_url.startswith("http://") and not poll_url.startswith("https://"):
        poll_url = urllib.parse.urljoin(WDG_SITE_BASE + "/", poll_url.lstrip("/"))
    expected_host = urllib.parse.urlsplit(WDG_SITE_BASE).hostname
    poll_host = urllib.parse.urlsplit(poll_url).hostname
    if poll_host != expected_host:
        return WdgResult(
            ok=False,
            job_id=job_id,
            error="WDG returned a poll URL on an unexpected host; refusing to send the API key there.",
        )

    deadline = time.monotonic() + WDG_POLL_TIMEOUT_SECONDS
    previous_status = ""
    transient_errors = 0
    while time.monotonic() < deadline:
        try:
            job = request_json(
                poll_url,
                headers={"X-API-Key": api_key, "Accept": "application/json"},
                timeout=30.0,
                attempts=1,
                logger=logger,
            )
            transient_errors = 0
        except HttpRequestError as exc:
            transient_errors += 1
            if transient_errors >= 5:
                raise TransferError(f"Could not poll WDG upload job {job_id}: {exc}") from exc
            time.sleep(WDG_POLL_INTERVAL_SECONDS)
            continue

        if job.get("ok") is False:
            message = job.get("error") or job.get("message") or "WDG job polling returned ok=false"
            return WdgResult(ok=False, job_id=job_id, error=str(message))
        status = str(job.get("status") or "").lower()
        if status != previous_status:
            logger.info(f"WDG job {job_id}: {status or 'unknown'}.")
            previous_status = status
        if status == "done":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            return WdgResult(ok=True, job_id=job_id, status=status, result=result)
        if status == "failed":
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            message = job.get("error") or result.get("error") or "WDG import job failed"
            return WdgResult(ok=False, job_id=job_id, status=status, result=result, error=str(message))
        time.sleep(WDG_POLL_INTERVAL_SECONDS)

    return WdgResult(
        ok=False,
        job_id=job_id,
        status=previous_status,
        error=f"WDG job did not finish within {int(WDG_POLL_TIMEOUT_SECONDS)} seconds.",
    )


def _print_filter_summary(stats: FilterStats, logger: Logger) -> None:
    logger.info(
        "Row filter: "
        f"source={stats.source_rows}, ready={stats.kept_rows}, "
        f"old={stats.too_old_rows}, already_sent={stats.already_sent_rows}, "
        f"run_duplicates={stats.duplicate_rows_in_run}, non_wifi={stats.non_wifi_rows}, "
        f"no_gps={stats.no_gps_rows}, bad_time={stats.bad_timestamp_rows}, "
        f"future={stats.future_rows}, malformed={stats.malformed_rows}."
    )


def build_batch(
    credentials: Credentials,
    *,
    cutoff: datetime,
    end: datetime,
    output_dir: Path,
    state_path: Path,
    logger: Logger,
) -> tuple[PreparedBatch | None, dict[str, Any]]:
    state = load_state(state_path, now=end, logger=logger)
    sent_map = state.setdefault("sent", {})
    already_sent = set(str(key) for key in sent_map)

    logger.info("Querying only the single newest WiGLE upload transaction.")
    newest = fetch_latest_wigle_transaction(
        credentials.wigle_authorization,
        logger=logger,
    )
    if newest is None:
        logger.info("WiGLE returned no upload transactions.")
        return None, state
    transid = str(newest["transid"]).strip()
    if state.get("last_successful_transaction_id") == transid:
        logger.info("No new WiGLE upload; the newest transaction was already accepted by WDG.")
        return None, state
    if not transaction_is_ready(newest, logger=logger):
        return None, state

    try:
        text = download_wigle_csv(
            transid,
            credentials.wigle_authorization,
            logger=logger,
        )
    except HttpRequestError as exc:
        if exc.status in {404, 409, 425}:
            logger.info(f"Newest WiGLE transaction {transid} is not downloadable yet.")
            return None, state
        raise

    rows, stats = filter_wigle_csv(
        text,
        cutoff=cutoff,
        end=end,
        already_sent=already_sent,
        run_seen=set(),
    )
    _print_filter_summary(stats, logger)
    if not rows:
        logger.info("The newest WiGLE upload has no unsent Wi-Fi/GPS rows from the selected time window.")
        return None, state

    rows.sort(
        key=lambda row: (
            row.values.get("FirstSeen", ""),
            row.values.get("MAC", ""),
            row.values.get("CurrentLatitude", ""),
            row.values.get("CurrentLongitude", ""),
        )
    )
    csv_path, gzip_path = write_batch_files(rows, output_dir=output_dir, end=end)
    if gzip_path.stat().st_size > WDG_SAFE_UPLOAD_BYTES:
        raise TransferError(
            f"The filtered 24-hour batch compressed to {gzip_path.stat().st_size / (1024 * 1024):.1f} MB. "
            "WDG's cap is 30 MB; the newest transaction is too large for one upload."
        )

    return PreparedBatch(
        csv_path=csv_path,
        gzip_path=gzip_path,
        rows=rows,
        cutoff=cutoff,
        end=end,
        stats=stats,
        source_transactions=[transid],
    ), state


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send recent rows from only your single newest WiGLE upload to WDG."
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=DEFAULT_HOURS,
        help="Rolling lookback window in hours (0 < hours <= 24; default: 24).",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Credential file (default: {DEFAULT_ENV_PATH.name}).",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Local duplicate-prevention state (default: {DEFAULT_STATE_PATH.name}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for filtered audit CSV files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and filter data, but do not upload it or update state.",
    )
    parser.add_argument("--verbose", action="store_true", help="Write extra diagnostic lines.")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = Logger(DEFAULT_LOG_PATH, verbose=args.verbose)

    try:
        if not (0.0 < args.hours <= MAX_HOURS):
            raise TransferError("--hours must be greater than 0 and no more than 24.")

        load_env_file(args.env.resolve())
        credentials = read_credentials(require_wdg=True)
        end = datetime.now(timezone.utc)
        cutoff = end - timedelta(hours=float(args.hours))
        logger.info(
            f"Starting WiGLE -> WDG transfer for {args.hours:g} hours: "
            f"{cutoff.strftime('%Y-%m-%d %H:%M:%SZ')} through {end.strftime('%Y-%m-%d %H:%M:%SZ')}."
        )

        profile = validate_wdg_key(credentials.wdg_api_key, logger=logger)
        username = str(profile.get("username") or "authenticated user")
        logger.info(f"WDG API key accepted for {username}.")

        batch, state = build_batch(
            credentials,
            cutoff=cutoff,
            end=end,
            output_dir=args.output_dir.resolve(),
            state_path=args.state.resolve(),
            logger=logger,
        )
        if batch is None:
            logger.info("Nothing new from the last 24 hours needs to be sent to WDG.")
            return 0

        logger.info(
            f"Prepared {len(batch.rows)} Wi-Fi observations in {batch.csv_path.name}."
        )
        if args.dry_run:
            logger.info("Dry run complete; no WDG upload was made and state was not changed.")
            return 0

        result = upload_wdg_v2(credentials.wdg_api_key, batch.gzip_path, logger=logger)
        if not result.ok:
            raise TransferError(
                f"WDG upload job {result.job_id or 'unknown'} failed: {result.error or result.status}"
            )

        sent_map = state.setdefault("sent", {})
        sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for row in batch.rows:
            sent_map[row.fingerprint] = sent_at
        state["last_success"] = {
            "at": sent_at,
            "job_id": result.job_id,
            "rows": len(batch.rows),
            "cutoff": batch.cutoff.isoformat().replace("+00:00", "Z"),
            "end": batch.end.isoformat().replace("+00:00", "Z"),
            "csv": batch.csv_path.name,
            "source_transactions": batch.source_transactions,
            "wdg_result": result.result,
        }
        state["last_successful_transaction_id"] = batch.source_transactions[0]
        save_state(args.state.resolve(), state)

        detail = result.result
        detail_bits = []
        for key in ("imported", "captured", "updated", "duplicates", "no_gps", "bad_rows"):
            value = detail.get(key)
            if value not in (None, 0, "0"):
                detail_bits.append(f"{key}={value}")
        suffix = ", ".join(detail_bits) if detail_bits else "completed"
        logger.info(f"WDG job {result.job_id} succeeded: {suffix}.")
        logger.info(f"Local duplicate state updated at {args.state.resolve()}.")
        return 0

    except KeyboardInterrupt:
        logger.warning("Cancelled by user.")
        return 130
    except (TransferError, HttpRequestError) as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:
        logger.error(f"Unexpected {type(exc).__name__}: {exc}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
