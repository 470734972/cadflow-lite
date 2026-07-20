from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("CADFLOW_MODE", "demo").lower()
    cluster_name: str = os.getenv("CADFLOW_CLUSTER_NAME", "eda-lab")
    db_path: Path = Path(os.getenv("CADFLOW_DB_PATH", "./data/cadflow.db"))
    admin_token: str = os.getenv("CADFLOW_ADMIN_TOKEN", "")
    collect_interval_seconds: int = _int_env("CADFLOW_COLLECT_INTERVAL_SECONDS", 60)
    command_timeout_seconds: int = _int_env("CADFLOW_COMMAND_TIMEOUT_SECONDS", 20)
    stale_after_seconds: int = _int_env("CADFLOW_STALE_AFTER_SECONDS", 0)
    lsf_bin_dir: str = os.getenv("CADFLOW_LSF_BIN_DIR", "")
    lmstat_path: str = os.getenv("CADFLOW_LMSTAT_PATH", "/opt/flexnet/bin/lmstat")
    license_vendor: str = os.getenv("CADFLOW_LICENSE_VENDOR", "")
    license_servers: tuple[str, ...] = tuple(
        value.strip()
        for value in os.getenv("CADFLOW_LICENSE_SERVERS", "").split(",")
        if value.strip()
    )

    def validate(self) -> None:
        if self.mode not in {"demo", "lsf"}:
            raise ValueError("CADFLOW_MODE must be demo or lsf")
        if self.collect_interval_seconds < 15:
            raise ValueError("collection interval must be at least 15 seconds")
        if self.command_timeout_seconds < 1:
            raise ValueError("command timeout must be at least 1 second")
        if self.mode == "lsf" and not self.lsf_bin_dir:
            raise ValueError("CADFLOW_LSF_BIN_DIR is required in lsf mode")

    @property
    def effective_stale_after_seconds(self) -> int:
        return self.stale_after_seconds or self.collect_interval_seconds * 2 + self.command_timeout_seconds


settings = Settings()
