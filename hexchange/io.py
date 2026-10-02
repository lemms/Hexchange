"""Reading and writing settings and campaigns, and finding setting files.

Settings are looked up, in order, in: explicit directories passed by the
caller, ``$HEXCHANGE_SETTINGS`` (``os.pathsep``-separated), and the generic
settings bundled with the package.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from .model import SCHEMA_VERSION, Campaign, Setting

BUNDLED = Path(__file__).parent / "settings"
SETTING_SUFFIX = ".setting.json"
CAMPAIGN_SUFFIX = ".hexchange.json"


def setting_dirs(extra: list[str | Path] | None = None) -> list[Path]:
    dirs = [Path(d).expanduser() for d in (extra or [])]
    env = os.environ.get("HEXCHANGE_SETTINGS")
    if env:
        dirs += [Path(d).expanduser() for d in env.split(os.pathsep) if d]
    dirs.append(BUNDLED)
    return dirs


def list_settings(extra: list[str | Path] | None = None) -> dict[str, Path]:
    """name (file stem without suffix) -> path; earlier directories win."""
    out: dict[str, Path] = {}
    for d in setting_dirs(extra):
        if d.is_dir():
            for p in sorted(d.glob("*" + SETTING_SUFFIX)):
                out.setdefault(p.name[: -len(SETTING_SUFFIX)], p)
    return out


def _read_json(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_setting(name_or_path: str | Path, extra_dirs: list[str | Path] | None = None) -> Setting:
    p = Path(name_or_path).expanduser()
    if not p.exists():
        found = list_settings(extra_dirs).get(str(name_or_path))
        if found is None:
            raise FileNotFoundError(f"setting {name_or_path!r} not found in {setting_dirs(extra_dirs)}")
        p = found
    return Setting.model_validate(_migrate(_read_json(p)))


def load_campaign(path: str | Path) -> Campaign:
    data = _migrate(_read_json(Path(path).expanduser()))
    return Campaign.model_validate(data)


def save_campaign(camp: Campaign, path: str | Path) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = camp.model_dump_json(indent=None, exclude_none=True)
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix == ".gz":
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)                       # atomic: never leave a half-written campaign
    return path


def _migrate(data: dict) -> dict:
    v = int(data.get("schema_version", SCHEMA_VERSION))
    if v > SCHEMA_VERSION:
        raise ValueError(f"file schema version {v} is newer than this hexchange ({SCHEMA_VERSION})")
    # future: step-wise upgrades from older versions go here
    return data


def export_schemas(out_dir: str | Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, model in (("setting", Setting), ("campaign", Campaign)):
        p = out_dir / f"{name}.schema.json"
        p.write_text(json.dumps(model.model_json_schema(), indent=2), encoding="utf-8")
        paths.append(p)
    return paths
