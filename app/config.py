from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    cluster_name: str
    collect_interval_seconds: int
    command_timeout_seconds: int
    stale_after_seconds: int
    lsf_bin_dir: str
    lmstat_path: str
    license_servers: tuple[str, ...]
    license_vendor: str
    lsf_env: dict[str, str]

    @classmethod
    def from_settings(cls, source: Settings) -> "RuntimeConfig":
        return cls(source.mode, source.cluster_name, source.collect_interval_seconds, source.command_timeout_seconds,
                   source.stale_after_seconds, source.lsf_bin_dir, source.lmstat_path, source.license_servers,
                   source.license_vendor, {})

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeConfig":
        servers = data.get("license_servers", ())
        if isinstance(servers, str):
            servers = tuple(item.strip() for item in servers.split(",") if item.strip())
        env = data.get("lsf_env", {})
        if not isinstance(env, dict):
            raise ValueError("lsf_env must be an object")
        allowed = {key: str(value) for key, value in env.items() if key.startswith("LSF_") or key in {"PATH", "LD_LIBRARY_PATH"}}
        if len(allowed) != len(env):
            raise ValueError("lsf_env only permits LSF_*, PATH and LD_LIBRARY_PATH")
        vendor_text = str(data.get("license_vendor", "")).strip()
        vendors = [item.strip() for item in vendor_text.split(",") if item.strip()]
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in vendors):
            raise ValueError("license_vendor must contain English daemon names separated by commas")
        config = cls(
            str(data.get("mode", "demo")).lower(), str(data.get("cluster_name", "eda-lab")).strip(),
            int(data.get("collect_interval_seconds", 300)), int(data.get("command_timeout_seconds", 45)),
            int(data.get("stale_after_seconds", 0)), str(data.get("lsf_bin_dir", "")).strip(),
            str(data.get("lmstat_path", "")).strip(), tuple(servers), ",".join(vendors), allowed,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.mode not in {"setup", "demo", "lsf"}:
            raise ValueError("mode must be setup, demo or lsf")
        if not self.cluster_name:
            raise ValueError("cluster_name is required")
        if self.collect_interval_seconds < 15:
            raise ValueError("collection interval must be at least 15 seconds")
        if self.command_timeout_seconds < 1:
            raise ValueError("command timeout must be at least 1 second")
        if self.mode == "lsf" and not self.lsf_bin_dir:
            raise ValueError("lsf_bin_dir is required in lsf mode")

    def to_dict(self) -> dict:
        data = asdict(self)
        data["license_servers"] = list(self.license_servers)
        return data

    @property
    def effective_stale_after_seconds(self) -> int:
        return self.stale_after_seconds or self.collect_interval_seconds * 2 + self.command_timeout_seconds
