#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import yaml


def load_info_yaml(path: Path):
    with path.open() as f:
        return yaml.safe_load(f)


def ensure_tt_assets(project_root: Path, pdk: str, tiles: str) -> Path:
    """Copy minimal TT assets (tile_sizes + required DEF) into project-local folder.

    Returns the path to the copied DEF inside the project.
    """
    src_tools = project_root / ".tt-tools"
    if not src_tools.exists():
        return Path()
    src_tech = src_tools / "tech" / pdk
    if not src_tech.exists():
        return Path()
    dst = project_root / "tt_assets"
    dst.mkdir(parents=True, exist_ok=True)
    # Copy tile_sizes.yaml
    (dst / "tile_sizes.yaml").write_text((src_tech / "tile_sizes.yaml").read_text())
    # Copy only the needed DEF template
    def_suffix = "pg" if pdk == "sky130A" else "pgvdd"
    def_name = f"tt_block_{tiles}_{def_suffix}.def"
    def_src = src_tech / "def" / def_name
    def_dst = dst / def_name
    def_dst.write_text(def_src.read_text())
    return def_dst


def main():
    project_root = Path(os.environ.get("PROJECT_DIR", ".")).resolve()
    info = load_info_yaml(project_root / "info.yaml")
    proj = info["project"]
    top = proj["top_module"]
    sources = proj["source_files"]
    tiles = proj["tiles"]
    pdk = "sky130A"  # default TT PDK

    # Ensure TT assets available for DEF template and tile sizes
    copied_def = ensure_tt_assets(project_root, pdk, tiles)

    # Load tile sizes
    tile_sizes_path = project_root / "tt_assets" / "tile_sizes.yaml"
    if not tile_sizes_path.exists():
        print("Error: Missing TT assets. Could not find", tile_sizes_path, file=sys.stderr)
        sys.exit(1)
    with tile_sizes_path.open() as f:
        tile_sizes = yaml.safe_load(f)
    die_area = tile_sizes[tiles]

    def_suffix = "pg" if pdk == "sky130A" else "pgvdd"
    if copied_def:
        def_template = f"dir::{copied_def.relative_to(project_root)}"
    else:
        def_template = f"dir::tt_assets/tt_block_{tiles}_{def_suffix}.def"

    user_cfg = {
        "DESIGN_NAME": top,
        "VERILOG_FILES": [f"dir::{s}" for s in sources],
        "DIE_AREA": die_area,
        "FP_DEF_TEMPLATE": def_template,
        "VDD_PIN": "VPWR",
        "GND_PIN": "VGND",
        "RT_MAX_LAYER": "met4",
    }

    src_dir = project_root / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "user_config.json").write_text(json.dumps(user_cfg, indent=2) + "\n")

    # Merge with existing src/config.json if present
    merged = {}
    cfg_path_json = src_dir / "config.json"
    if cfg_path_json.exists():
        merged.update(json.loads(cfg_path_json.read_text()))
    merged.update(user_cfg)
    (src_dir / "config_merged.json").write_text(json.dumps(merged, indent=2) + "\n")
    print("Created src/user_config.json and src/config_merged.json")


if __name__ == "__main__":
    main()
