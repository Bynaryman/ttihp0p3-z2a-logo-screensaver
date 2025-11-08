#!/usr/bin/env python3
"""
Prepare a standalone OpenROAD viewer bundle (DEF + LEFs + TCL) from an ODB.

Usage examples:
  python3 tools/prepare_view_bundle.py \
    --odb runs/wokwi/26-odb-applydeftemplate/tt_um_*.odb

Optional overrides:
  --out-dir runs/wokwi/view
  --pdk-root /path/to/pdk --pdk sky130A|ihp-sg13g2|gf180mcuD

Then open with:
  openroad -gui -no_init -files runs/wokwi/view/openroad_open_view.tcl
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import subprocess
from pathlib import Path


def derive_run_root(odb: Path) -> Path:
    parts = odb.resolve().parts
    if "runs" in parts:
        i = parts.index("runs")
        # runs/<tag>
        if i + 1 < len(parts):
            return Path(*parts[: i + 2])
    # Fallback to project/runs/wokwi
    return Path.cwd() / "runs" / "wokwi"


def load_resolved(run_root: Path) -> tuple[str | None, str | None]:
    try:
        with (run_root / "resolved.json").open() as f:
            data = json.load(f)
            return data.get("PDK_ROOT"), data.get("PDK")
    except Exception:
        return None, None


def add_glob(acc: list[str], pattern: str, recursive: bool = True) -> None:
    for p in sorted(glob.glob(pattern, recursive=recursive)):
        if p not in acc:
            acc.append(p)


def collect_lefs(pdk_root: str | None, pdk: str | None, final_lef_dir: Path | None) -> tuple[list[str], list[str]]:
    tlefs: list[str] = []
    lefs: list[str] = []

    if final_lef_dir and final_lef_dir.is_dir():
        add_glob(lefs, str(final_lef_dir / "*.lef"), recursive=False)

    if pdk_root and pdk == "sky130A":
        # Tech LEFs (recursive to support varied layouts)
        add_glob(tlefs, os.path.join(pdk_root, "**", "sky130A", "libs.tech", "lef", "**", "*.tlef"))
        # All std/macro LEFs (include ef/others, not only hd)
        add_glob(lefs, os.path.join(pdk_root, "**", "sky130A", "libs.ref", "**", "lef", "*.lef"))
    elif pdk_root and pdk == "ihp-sg13g2":
        add_glob(tlefs, os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.tech", "lef", "*.tlef"))
        add_glob(lefs, os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.tech", "lef", "*.lef"))
        add_glob(lefs, os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.ref", "**", "lef", "*.lef"))
    elif pdk_root and pdk == "gf180mcuD":
        add_glob(tlefs, os.path.join(pdk_root, "**", "gf180mcuD", "libs.tech", "lef", "*.tlef"))
        add_glob(lefs, os.path.join(pdk_root, "**", "gf180mcuD", "libs.tech", "lef", "*.lef"))
        add_glob(lefs, os.path.join(pdk_root, "**", "gf180mcuD", "libs.ref", "**", "lef", "*.lef"))

    return tlefs, lefs


def export_def(odb: Path, out_def: Path) -> None:
    tcl = f"read_db {shlex.quote(str(odb))}; write_def {shlex.quote(str(out_def))}\n"
    subprocess.run(["openroad", "-no_init", "-exit"], input=tcl.encode(), check=True)


def write_files(view_dir: Path, out_def: Path, tlefs: list[str], lefs: list[str], odb_src: Path | None) -> None:
    view_dir.mkdir(parents=True, exist_ok=True)

    with (view_dir / "lefs.txt").open("w") as f:
        for lef in tlefs:
            f.write(lef + "\n")
        for lef in lefs:
            f.write(lef + "\n")

    with (view_dir / "openroad_open_view.tcl").open("w") as f:
        f.write("# Auto-generated viewer script\n")
        f.write(f"set view_dir {{{view_dir}}}\n")
        f.write(f"set def_file {{{out_def}}}\n")
        if odb_src is not None:
            f.write(f"set odb_file {{{view_dir / odb_src.name}}}\n")
        else:
            f.write("set odb_file {}\n")
        libs_file = view_dir / "libs.txt"
        f.write(f"set libs_file {{{libs_file}}}\n")
        f.write("if {[file exists $libs_file]} {\n")
        f.write("  set fh [open $libs_file r]\n")
        f.write("  set libs [split [read $fh] \n]\n")
        f.write("  close $fh\n")
        f.write("  foreach lib $libs { if {$lib ne \"\"} { catch { read_liberty $lib } } }\n")
        f.write("} else { catch { sta::suppress_message STA-2141 } }\n")
        f.write("if {[file exists $odb_file]} {\n")
        f.write("  puts \"Reading ODB: $odb_file\"\n")
        f.write("  read_db $odb_file\n")
        f.write("} else {\n")
        for lef in tlefs:
            f.write(f"  read_lef -tech {{{lef}}}\n")
        for lef in lefs:
            f.write(f"  read_lef {{{lef}}}\n")
        f.write(f"  read_def {{{out_def}}}\n")
        f.write("}\n")
        f.write("# gui_show\n")

    # KLayout helper script
    with (view_dir / "klayout_open_view.py").open("w") as f:
        f.write(
            (
                """
import os
import pya

here = os.path.dirname(__file__)
def_path = os.path.join(here, "design.def")
lefs_path = os.path.join(here, "lefs.txt")

app = pya.Application.instance()
mw = app.main_window()
lv = mw.current_view()
if lv is None:
    mw.create_view()
    lv = mw.current_view()

# Collect LEFs
tlefs = []
cell_lefs = []
if os.path.isfile(lefs_path):
    with open(lefs_path) as lf:
        for line in lf:
            p = line.strip()
            if not p:
                continue
            (tlefs if p.endswith(".tlef") else cell_lefs).append(p)
    try:
        opt = pya.LoadLayoutOptions()
        # Newer KLayout builds provide LefdefConfig API
        if hasattr(pya, "LefdefConfig"):
            cfg = pya.LefdefConfig()
            cfg.lef_files = tlefs + cell_lefs
            opt.lefdef_config = cfg
            lv.load_layout(def_path, "", opt)
        else:
            # Fallback: try plain DEF load and instruct manual import
            lv.load_layout(def_path, "")
            print("[KLayout] NOTE: LefdefConfig API not available. If macros are missing, use File → Import → LEF/DEF and select lefs.txt + design.def.")
    except Exception as e:
        print(f"[KLayout] ERROR: failed to load DEF: {e}")
"""
            ).strip()
            + "\n"
        )

    with (view_dir / "README.md").open("w") as f:
        f.write(
            """
Viewer bundle
=============

Files:
- design.def: exported from the provided ODB
- lefs.txt: list of LEF files (tech/stdcell/macros)
- openroad_open_view.tcl: helper to open in OpenROAD GUI

Open in OpenROAD GUI:
  openroad -gui -no_init -files openroad_open_view.tcl

Open in KLayout (LEF/DEF importer):
  - File → Import → LEF/DEF, load LEFs from lefs.txt, then design.def
""".strip()
            + "\n"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Prepare a standalone viewer bundle from an ODB")
    ap.add_argument("--odb", required=True, help="Path to input .odb")
    ap.add_argument("--out-dir", default=None, help="Output directory for the view bundle")
    ap.add_argument("--pdk-root", default=None, help="PDK root (if not detectable from run)")
    ap.add_argument("--pdk", default=None, help="PDK name (sky130A|ihp-sg13g2|gf180mcuD)")
    ap.add_argument("--final-lef-dir", default=None, help="Directory with final LEFs to include")
    args = ap.parse_args()

    odb = Path(args.odb)
    run_root = derive_run_root(odb)
    out_dir = Path(args.out_dir) if args.out_dir else (run_root / "view")
    final_lef_dir = Path(args.final_lef_dir) if args.final_lef_dir else (run_root / "final" / "lef")

    pdk_root, pdk = args.pdk_root, args.pdk
    if not pdk_root or not pdk:
        rr_pdk_root, rr_pdk = load_resolved(run_root)
        pdk_root = pdk_root or rr_pdk_root
        pdk = pdk or rr_pdk

    out_def = out_dir / "design.def"
    print(f"[view] Exporting DEF from ODB: {odb}")
    out_dir.mkdir(parents=True, exist_ok=True)
    export_def(odb, out_def)
    # Copy ODB alongside for direct GUI load
    odb_copy = out_dir / odb.name
    try:
        if not odb_copy.exists():
            import shutil
            shutil.copyfile(odb, odb_copy)
    except Exception:
        odb_copy = None

    print(f"[view] Discovering LEFs (PDK={pdk}, PDK_ROOT={pdk_root})")
    tlefs, lefs = collect_lefs(pdk_root, pdk, final_lef_dir)

    # Also write a libs.txt with a best-effort list of liberty files
    lib_files: list[str] = []
    def add_lib_glob(pattern: str):
        for p in sorted(glob.glob(pattern, recursive=True)):
            if p not in lib_files:
                lib_files.append(p)
    if pdk_root and pdk == "sky130A":
        add_lib_glob(os.path.join(pdk_root, "**", "sky130A", "libs.ref", "sky130_fd_sc_hd", "lib", "*.lib"))
    elif pdk_root and pdk == "ihp-sg13g2":
        add_lib_glob(os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.ref", "**", "lib", "*.lib"))
    elif pdk_root and pdk == "gf180mcuD":
        add_lib_glob(os.path.join(pdk_root, "**", "gf180mcuD", "libs.ref", "**", "lib", "*.lib"))

    try:
        with (out_dir / "libs.txt").open("w") as f:
            for lib in lib_files:
                f.write(lib + "\n")
    except Exception:
        pass

    write_files(out_dir, out_def, tlefs, lefs, odb_copy if isinstance(odb_copy, Path) and odb_copy.exists() else None)

    print("[view] Wrote:")
    print(f"  - {out_def}")
    print(f"  - {out_dir/'lefs.txt'}")
    print(f"  - {out_dir/'openroad_open_view.tcl'}")


if __name__ == "__main__":
    main()
