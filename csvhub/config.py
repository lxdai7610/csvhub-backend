from dataclasses import dataclass
from pathlib import Path
from ipaddress import ip_address
import os
import re
import secrets


PROJECT_DIR = Path(__file__).resolve().parents[1]
ENV_KEYS = frozenset(("CSVHUB_DATA_DIR", "CSVHUB_BATCH_ROWS", "CSVHUB_BATCH_BYTES",
                      "CSVHUB_CACHE_BYTES", "CSVHUB_API_TOKEN", "CSVHUB_HOST", "CSVHUB_PORT",
                      "CSVHUB_SQLITE_TEMP_DIR"))


def project_environment(project=PROJECT_DIR):
    """Read literal project settings; never evaluate shell commands or expand secrets."""
    path = project / ".env"
    values = {}
    if not path.exists():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env setting on line {number}")
        if key not in ENV_KEYS:
            if key.startswith("CSVHUB_"):
                raise ValueError(f"Unknown CSVHub setting on .env line {number}")
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            tail = value[end + 1:].strip() if end >= 0 else ""
            if end < 0 or (tail and not tail.startswith("#")):
                raise ValueError(f"Invalid quoted .env setting on line {number}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if not value:
            raise ValueError(f"Empty CSVHub setting on .env line {number}; remove it to use the default")
        values[key] = value
    return values


def project_path(value):
    path = Path(value)
    return (path if path.is_absolute() else PROJECT_DIR / path).resolve()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    batch_rows: int = 5000
    batch_bytes: int = 8 * 1024 * 1024
    sqlite_cache_kib: int = 32768
    result_cache_bytes: int = 128 * 1024 * 1024
    cache_ttl: float = 120
    max_upload_bytes: int = 20 * 1024**3
    max_chunk_bytes: int = 8 * 1024 * 1024
    max_record_bytes: int = 4 * 1024 * 1024
    query_timeout: float = 3
    api_token: str | None = None
    min_free_bytes: int = 256 * 1024 * 1024
    read_concurrency: int = 4
    max_response_bytes: int = 8 * 1024 * 1024
    max_xls_bytes: int = 128 * 1024 * 1024
    max_xlsx_metadata_bytes: int = 64 * 1024 * 1024
    max_excel_uncompressed_bytes: int = 40 * 1024**3
    host: str = "0.0.0.0"
    port: int = 8000
    sqlite_temp_dir: Path | None = None

    def __post_init__(self):
        positive = (self.batch_rows, self.batch_bytes, self.sqlite_cache_kib, self.result_cache_bytes,
                    self.cache_ttl, self.max_upload_bytes, self.max_chunk_bytes, self.max_record_bytes,
                    self.query_timeout, self.read_concurrency, self.max_response_bytes,
                    self.max_xls_bytes, self.max_xlsx_metadata_bytes, self.max_excel_uncompressed_bytes)
        if any(value <= 0 for value in positive) or self.min_free_bytes < 0:
            raise ValueError("Capacity and timeout settings must be positive")
        if self.api_token is not None and len(self.api_token) < 24:
            raise ValueError("API token must have at least 24 characters")
        try:
            ip_address(self.host)
        except ValueError:
            raise ValueError("CSVHUB_HOST must be an IPv4 or IPv6 address") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("CSVHUB_PORT must be between 1 and 65535")

    @classmethod
    def from_env(cls):
        values = project_environment()
        values.update({key: value for key, value in os.environ.items() if key in ENV_KEYS})
        return cls(
            data_dir=project_path(values.get("CSVHUB_DATA_DIR", "data")),
            batch_rows=int(values.get("CSVHUB_BATCH_ROWS", "5000")),
            batch_bytes=int(values.get("CSVHUB_BATCH_BYTES", str(8 * 1024**2))),
            result_cache_bytes=int(values.get("CSVHUB_CACHE_BYTES", str(128 * 1024**2))),
            api_token=values.get("CSVHUB_API_TOKEN"),
            host=values.get("CSVHUB_HOST", "0.0.0.0").strip(),
            port=int(values.get("CSVHUB_PORT", "8000")),
            sqlite_temp_dir=project_path(values["CSVHUB_SQLITE_TEMP_DIR"])
            if values.get("CSVHUB_SQLITE_TEMP_DIR") else None,
        )

    def prepare(self):
        for name in ("uploads", "versions", "locks", "deletions"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    def token(self) -> str:
        if self.api_token:
            return self.api_token
        self.prepare()
        path = self.data_dir / "api-token.txt"
        try:
            with path.open("x", encoding="ascii") as file:
                file.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 24:
            raise RuntimeError("API token must have at least 24 characters")
        return value

