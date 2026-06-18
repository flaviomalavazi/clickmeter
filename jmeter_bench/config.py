"""Load config/benchmark.properties into a plain dict, with CLI overrides."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CFG = ROOT / "config" / "benchmark.properties"
DEFAULT_ENV = ROOT / ".env"


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _parse_kv_file(path: Path) -> dict:
    """Parse a `key=value` file (.properties or .env): skip blanks/comments,
    split on the first '=', strip surrounding quotes from the value."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = _strip_quotes(val.strip())
    return out


def load_config(path=None, overrides=None, env_path=None) -> dict:
    """Build the benchmark config. Precedence (low to high):
    properties file < .env (credentials) < `overrides` (CLI key=val args)."""
    path = Path(path) if path else DEFAULT_CFG
    cfg: dict[str, str] = _parse_kv_file(path)

    env_path = Path(env_path) if env_path else DEFAULT_ENV
    if env_path.exists():
        cfg.update(_parse_kv_file(env_path))

    for key, val in (overrides or {}).items():
        cfg[str(key)] = str(val)
    return cfg


def parse_overrides(pairs) -> dict:
    """Turn ['threads=50', 'duration=30'] into {'threads': '50', ...}."""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"override must be key=value, got: {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out
