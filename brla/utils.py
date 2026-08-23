"""Shared utilities: filesystem checkpoints are the pipeline's orchestrator."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s[:60] or "unnamed"


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path):
    if not Path(path).exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def checkpoint_exists(path: Path, force: bool = False) -> bool:
    """True if this module's output already exists and we are not forcing."""
    return (not force) and Path(path).exists()


def tech_dir(cfg: dict, tech_id: str) -> Path:
    d = cfg["paths"]["cache_dir"] / tech_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_bin_signal(record: dict) -> dict:
    """Coerce an evidence record's `bin_signal` into a {dimension: bin} dict.

    M5's output is LLM-generated and occasionally malformed. Observed in the
    wild (t002.b, 2 records in 914): `null`, and a bare `"Mid"` string where a
    dict was required. Both crashed M6 and M7 with AttributeError, because
    `.get("bin_signal", {})` only substitutes the default when the key is
    ABSENT — a present-but-null value sails straight through.

    A bare string is repaired rather than discarded: it is applied to every
    dimension the record is tagged with, which is what the model meant.
    Anything else degrades to {} (no direct signal), never an exception.
    """
    raw = record.get("bin_signal")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {d: raw.strip() for d in (record.get("dimensions") or [])}
    return {}


def domain_blocked(url: str, blocked_domains: list[str]) -> bool:
    """True if the URL's host is, or is a subdomain of, a blocked domain.

    Video/social hosts never yield extractable text (see handoff: YouTube,
    TikTok, Facebook all came back `no_content`), so they are dropped before
    they consume a fetch slot.
    """
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in blocked_domains)


def extract_urls(text: str) -> list[str]:
    """Pull http(s) URLs out of free text (used by M4b on DR reports)."""
    pattern = r"https?://[^\s\)\]\>\"\'\,]+"
    urls = re.findall(pattern, text)
    cleaned = []
    for u in urls:
        u = u.rstrip(".;:!?")
        if len(u) > 12 and u not in cleaned:
            cleaned.append(u)
    return cleaned
