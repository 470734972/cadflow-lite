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
    cluster_name: str = os.getenv("CADFLOW_CLUSTER_NAME", "demo-cluster")
    db_path: Path = Path(os.getenv("CADFLOW_DB_PATH", "./data/cadflow.db"))
    admin_token: str = os.getenv("CADFLOW_ADMIN_TOKEN", "")
    config_password_hash: str = os.getenv("CADFLOW_CONFIG_PASSWORD_HASH", "")
    collect_interval_seconds: int = _int_env("CADFLOW_COLLECT_INTERVAL_SECONDS", 60)
    command_timeout_seconds: int = _int_env("CADFLOW_COMMAND_TIMEOUT_SECONDS", 20)
    stale_after_seconds: int = _int_env("CADFLOW_STALE_AFTER_SECONDS", 0)
    db_retention_days: int = _int_env("CADFLOW_DB_RETENTION_DAYS", 7)
    db_max_size_mb: int = _int_env("CADFLOW_DB_MAX_SIZE_MB", 1024)
    license_sample_interval_seconds: int = _int_env("CADFLOW_LICENSE_SAMPLE_INTERVAL_SECONDS", 3600)
    lsf_bin_dir: str = os.getenv("CADFLOW_LSF_BIN_DIR", "")
    # The executable location is site-specific and is entered in the Web
    # configuration page when LSF mode is enabled.
    lmstat_path: str = os.getenv("CADFLOW_LMSTAT_PATH", "")
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
        if self.db_retention_days < 0:
            raise ValueError("database retention days must be zero or greater")
        if self.db_max_size_mb < 0:
            raise ValueError("database maximum size must be zero or greater")
        if self.mode == "lsf" and not self.lsf_bin_dir:
            raise ValueError("CADFLOW_LSF_BIN_DIR is required in lsf mode")

    @property
    def effective_stale_after_seconds(self) -> int:
        return self.stale_after_seconds or self.collect_interval_seconds * 2 + self.command_timeout_seconds


settings = Settings()


@dataclass(frozen=True)
class LicenseSource:
    server: str
    vendor: str = ""


@dataclass(frozen=True)
class RuntimeConfig:
    mode: str
    cluster_name: str
    collect_interval_seconds: int
    command_timeout_seconds: int
    stale_after_seconds: int
    db_retention_days: int
    db_max_size_mb: int
    license_sample_interval_seconds: int
    lsf_bin_dir: str
    lmstat_path: str
    license_sources: tuple[LicenseSource, ...]
    lsf_env: dict[str, str]

    @classmethod
    def from_settings(cls, source: Settings) -> "RuntimeConfig":
        return cls(source.mode, source.cluster_name, source.collect_interval_seconds, source.command_timeout_seconds,
                   source.stale_after_seconds, source.db_retention_days, source.db_max_size_mb,
                   source.license_sample_interval_seconds, source.lsf_bin_dir, source.lmstat_path,
                   tuple(LicenseSource(server, source.license_vendor) for server in source.license_servers), {})

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeConfig":
        raw_sources = data.get("license_sources")
        if raw_sources is None:
            servers = data.get("license_servers", ())
            if isinstance(servers, str):
                servers = tuple(item.strip() for item in servers.split(",") if item.strip())
            raw_sources = [{"server": server, "vendor": data.get("license_vendor", "")} for server in servers]
        if not isinstance(raw_sources, (list, tuple)):
            raise ValueError("license_sources must be a list")
        env = data.get("lsf_env", {})
        if not isinstance(env, dict):
            raise ValueError("lsf_env must be an object")
        allowed = {key: str(value) for key, value in env.items() if key.startswith("LSF_") or key in {"PATH", "LD_LIBRARY_PATH"}}
        if len(allowed) != len(env):
            raise ValueError("lsf_env only permits LSF_*, PATH and LD_LIBRARY_PATH")
        sources: list[LicenseSource] = []
        seen_servers: set[str] = set()
        for item in raw_sources:
            if not isinstance(item, dict):
                raise ValueError("each license source must be an object")
            server = str(item.get("server", "")).strip()
            vendor_text = str(item.get("vendor", "")).strip()
            vendors = [name.strip() for name in vendor_text.split(",") if name.strip()]
            if not server or any(char.isspace() for char in server):
                raise ValueError("license server must be a non-empty host or port@host value")
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in vendors):
                raise ValueError("license vendor must contain English daemon names separated by commas")
            key = server.lower()
            if key in seen_servers:
                raise ValueError("license server must not be duplicated")
            seen_servers.add(key)
            sources.append(LicenseSource(server, ",".join(vendors)))
        config = cls(
            str(data.get("mode", "demo")).lower(), str(data.get("cluster_name", "demo-cluster")).strip(),
            int(data.get("collect_interval_seconds", 300)), int(data.get("command_timeout_seconds", 45)),
            int(data.get("stale_after_seconds", 0)), int(data.get("db_retention_days", 7)),
            int(data.get("db_max_size_mb", 1024)), int(data.get("license_sample_interval_seconds", 3600)),
            str(data.get("lsf_bin_dir", "")).strip(),
            str(data.get("lmstat_path", "")).strip(), tuple(sources), allowed,
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
        if self.db_retention_days < 0:
            raise ValueError("database retention days must be zero or greater")
        if self.db_max_size_mb < 0:
            raise ValueError("database maximum size must be zero or greater")
        if self.license_sample_interval_seconds < 300:
            raise ValueError("license sample interval must be at least 300 seconds")
        if self.mode == "lsf" and not self.lsf_bin_dir:
            raise ValueError("lsf_bin_dir is required in lsf mode")

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def license_servers(self) -> tuple[str, ...]:
        return tuple(source.server for source in self.license_sources)

    @property
    def license_vendor(self) -> str:
        return ",".join(dict.fromkeys(source.vendor for source in self.license_sources if source.vendor))

    @property
    def effective_stale_after_seconds(self) -> int:
        return self.stale_after_seconds or self.collect_interval_seconds * 2 + self.command_timeout_seconds
