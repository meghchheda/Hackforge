"""Runtime settings, read once from environment variables (a local .env file is loaded first by app.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path = ROOT / ".env"):
    """Read KEY=VALUE lines from a local .env file; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    demo_csv: Path
    host: str
    port: int
    secure_cookies: bool          # set when served over HTTPS (behind a TLS proxy)
    public_demo: bool             # landing page may show headline numbers and the assistant without signing in
    demo_tools: bool              # tamper-test button for the audit chain
    session_hours: int
    retrain_every_hours: int      # full model refit after this many new data hours
    live_seconds_per_hour: int    # wall-clock seconds per data hour; 3600 in production, smaller for a sped-up demo feed
    max_upload_mb: int
    log_level: str
    trusted_proxies: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reserve_juggler.db"

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def load() -> Settings:
    data_dir = Path(os.environ.get("RJ_DATA_DIR", "").strip() or ROOT / "data").resolve()
    return Settings(
        data_dir=data_dir,
        demo_csv=Path(os.environ.get("RJ_DEMO_CSV", "").strip() or ROOT / "data" / "treasury_4tier_dataset.csv").resolve(),
        host=os.environ.get("RJ_HOST", "127.0.0.1").strip(),
        port=_int("RJ_PORT", 8000),
        secure_cookies=_bool("RJ_SECURE_COOKIES", False),
        public_demo=_bool("RJ_PUBLIC_DEMO", True),
        demo_tools=_bool("RJ_DEMO_TOOLS", True),
        session_hours=max(1, _int("RJ_SESSION_HOURS", 12)),
        retrain_every_hours=max(1, _int("RJ_RETRAIN_EVERY_HOURS", 24)),
        live_seconds_per_hour=max(1, _int("RJ_LIVE_SECONDS_PER_HOUR", 3600)),
        max_upload_mb=max(1, _int("RJ_MAX_UPLOAD_MB", 50)),
        log_level=os.environ.get("RJ_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        trusted_proxies=[x.strip() for x in os.environ.get("RJ_TRUSTED_PROXIES", "").split(",") if x.strip()],
    )
