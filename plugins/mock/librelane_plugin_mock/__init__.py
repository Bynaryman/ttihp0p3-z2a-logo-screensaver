from typing import Tuple
import os

from librelane.steps.step import Step, ViewsUpdate, MetricsUpdate
from librelane.steps import OdbpyStep
from librelane.state import State, DesignFormat
from librelane.flows import Flow, SequentialFlow
from librelane.flows.classic import Classic
from librelane.steps import OpenROAD, Yosys
from librelane.logging import info


@Step.factory.register()
class OpenROADPassthrough(Step):
    """
    Mock step for validation: logs the incoming ODB path and returns it as the
    output ODB unchanged. This allows testing LibreLane plugin discovery and the
    TT --with-art substitution path without changing the flow's behavior.
    """

    id = "Mock.OpenROADPassthrough"
    name = "Mock OpenROAD Passthrough"
    long_name = "Mock Step: passthrough of ODB with a log line"

    inputs = [DesignFormat.ODB]
    # No outputs to avoid mutating the state; this keeps downstream behavior
    # bit-for-bit identical when the step is a no-op.
    outputs = []

    def run(self, state_in: State, **kwargs) -> Tuple[ViewsUpdate, MetricsUpdate]:
        odb_in = state_in[DesignFormat.ODB]
        # Log using LibreLane's logger; add the step id as extra context
        info(f"[Mock] Received ODB: {odb_in}", extra={"step": self.id})
        # No processing—do not produce any outputs to keep the state unchanged.
        return {}, {}


@Flow.factory.register()
class ClassicWithArt(SequentialFlow):
    """
    A Classic flow variant that inserts the mock art step right before the first
    global placement (OpenROAD.GlobalPlacementSkipIO).

    This preserves all original Classic steps and only inserts our step at the
    desired point.
    """

    name = "ClassicWithArt"

    # Start with Classic steps but drop EQY to keep iteration fast/stable
    # for the art flow. This leaves all other checks intact.
    _steps = [s for s in Classic.Steps if s is not Yosys.EQY]
    try:
        insert_idx = _steps.index(OpenROAD.GlobalPlacementSkipIO)
    except ValueError:
        # Fallback: if for any reason the step is renamed/missing, insert before
        # OpenROAD.GlobalPlacement (the next occurrence in the flow)
        try:
            insert_idx = _steps.index(OpenROAD.GlobalPlacement)
        except ValueError:
            # As a last resort, append at end (should not happen in supported versions)
            insert_idx = len(_steps)

    # Insert our Python ODB art step with CLI flags
    class ApplyArt(OdbpyStep):
        id = "Odb.ApplyArt"
        name = "Apply Art"
        long_name = "Apply Art In-Place (Python)"
        inputs = [DesignFormat.ODB]
        outputs = []

        def get_script_path(self):
            return "/opt/mock/apply_art.py"

        def get_command(self):
            cmd = super().get_command()
            # Prefer env vars if they are present inside the container
            img = os.getenv("TT_ART_IMAGE")
            grid = os.getenv("TT_ART_GRID")
            thr = os.getenv("TT_ART_THRESHOLD")
            inv = os.getenv("TT_ART_INVERT")
            pct = os.getenv("TT_ART_AREA_PCT")
            mode = os.getenv("TT_ART_MODE")
            route_layer = os.getenv("TT_ART_ROUTE_LAYER")
            route_layers = []
            route_layers_env = os.getenv("TT_ART_ROUTE_LAYERS")
            if route_layers_env:
                route_layers.extend([p.strip() for p in route_layers_env.replace(";", ",").split(",") if p.strip()])

            # If not present, try a config file created by tt_tool at project root
            if not any([img, grid, thr, inv, pct]):
                try:
                    import json
                    # Resolve project root as parent of 'runs' if CWD in a step dir
                    cwd = os.getcwd()
                    parts = cwd.split(os.sep)
                    if "runs" in parts:
                        root = os.sep.join(parts[: parts.index("runs")]) or "/"
                    else:
                        root = cwd
                    cfg_path = os.path.join(root, "art_config.json")
                    with open(cfg_path) as f:
                        cfg = json.load(f)
                    img = cfg.get("image") or img
                    grid = str(cfg.get("grid")) if cfg.get("grid") is not None else grid
                    thr = str(cfg.get("threshold")) if cfg.get("threshold") is not None else thr
                    inv = "1" if cfg.get("invert") else inv
                    pct = str(cfg.get("area_pct")) if cfg.get("area_pct") is not None else pct
                    mode = cfg.get("mode") or mode
                    route_layer = cfg.get("route_layer") or route_layer
                    cfg_layers = cfg.get("route_layers")
                    if cfg_layers:
                        if isinstance(cfg_layers, str):
                            route_layers.extend([p.strip() for p in cfg_layers.replace(";", ",").split(",") if p.strip()])
                        elif isinstance(cfg_layers, (list, tuple)):
                            route_layers.extend([str(p).strip() for p in cfg_layers if str(p).strip()])
                except Exception:
                    pass

            if route_layers:
                route_layers = list(dict.fromkeys(route_layers))
            if img:
                cmd += ["--image", img]
            if grid:
                cmd += ["--grid", grid]
            if thr:
                cmd += ["--threshold", thr]
            if str(inv).lower() in ("1", "true", "yes"):
                cmd += ["--invert"]
            if pct:
                cmd += ["--area-pct", pct]
            if mode:
                cmd += ["--mode", mode]
            if route_layer:
                cmd += ["--route-layer", route_layer]
            for part in route_layers:
                cmd += ["--route-layers", part]
            return cmd

    _steps.insert(insert_idx, ApplyArt)

    # Post-art viewer bundle preparation step
    @Step.factory.register()
    class ViewerPrepareBundle(Step):
        id = "Viewer.PrepareBundle"
        name = "Prepare Viewer Bundle"
        long_name = "Prepare a viewer bundle (DEF + LEFs + scripts)"

        inputs = [DesignFormat.ODB]
        outputs = []

        def run(self, state_in: State, **kwargs) -> Tuple[ViewsUpdate, MetricsUpdate]:
            import os, json, glob, subprocess, shlex, shutil
            odb_path = state_in[DesignFormat.ODB]

            # Derive run root (…/runs/<tag>) and step dir from ODB path
            odb_abs = os.path.abspath(odb_path)
            parts = odb_abs.split(os.sep)
            try:
                runs_idx = parts.index("runs")
                run_root = os.sep.join(parts[: runs_idx + 2])  # …/runs/<tag>
            except ValueError:
                run_root = os.path.join(os.getcwd(), "runs", "wokwi")

            step_dir = os.path.dirname(odb_abs)
            view_dir = os.path.join(step_dir, "view")
            if os.path.isdir(view_dir):
                shutil.rmtree(view_dir, ignore_errors=True)
            os.makedirs(view_dir, exist_ok=True)

            # Export DEF from ODB via OpenROAD and stash a copy of the ODB for GUI
            def_path = os.path.join(view_dir, "design.def")
            tcl = f"read_db {shlex.quote(odb_abs)}; write_def {shlex.quote(def_path)}\n"
            try:
                subprocess.run(
                    ["openroad", "-no_init", "-exit"],
                    input=tcl.encode(),
                    check=True,
                )
            except Exception as e:
                # Best effort: leave a note if export fails
                with open(os.path.join(view_dir, "EXPORT_FAILED.txt"), "w") as f:
                    f.write(f"Failed to export DEF from {odb_abs}: {e}\n")
            # Copy ODB alongside for direct GUI load (tech comes embedded)
            try:
                import shutil
                odb_copy = os.path.join(view_dir, os.path.basename(odb_abs))
                if not os.path.exists(odb_copy):
                    shutil.copyfile(odb_abs, odb_copy)
            except Exception:
                pass

            # Collect LEFs: separate tech LEFs (*.tlef) from cell/macro LEFs (*.lef)
            tlefs: list[str] = []
            cell_lefs: list[str] = []
            final_lef_dir = os.path.join(run_root, "final", "lef")
            if os.path.isdir(final_lef_dir):
                cell_lefs += sorted(glob.glob(os.path.join(final_lef_dir, "*.lef")))

            # Read resolved.json for PDK info
            pdk_root = None
            pdk = None
            resolved_path = os.path.join(run_root, "resolved.json")
            try:
                with open(resolved_path) as f:
                    res = json.load(f)
                    pdk_root = res.get("PDK_ROOT")
                    pdk = res.get("PDK")
            except Exception:
                pass

            def add_glob_tlef(pattern):
                for p in sorted(glob.glob(pattern, recursive=True)):
                    if p not in tlefs:
                        tlefs.append(p)

            def add_glob_lef(pattern):
                for p in sorted(glob.glob(pattern, recursive=True)):
                    if p not in cell_lefs:
                        cell_lefs.append(p)

            # PDK LEFs: search recursively to support CIEL-style paths (…/ciel/…/versions/<hash>/sky130A/...)
            if pdk_root and pdk == "sky130A":
                # Tech LEFs
                add_glob_tlef(os.path.join(pdk_root, "**", "sky130A", "libs.tech", "lef", "**", "*.tlef"))
                # All std/macro LEFs (hd/ef/others)
                add_glob_lef(os.path.join(pdk_root, "**", "sky130A", "libs.ref", "**", "lef", "*.lef"))
            elif pdk_root and pdk == "ihp-sg13g2":
                # Some IHP distributions use .lef for tech; include as regular LEF if no .tlef exists
                add_glob_tlef(os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.tech", "lef", "*.tlef"))
                add_glob_lef(os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.tech", "lef", "*.lef"))
                add_glob_lef(os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.ref", "**", "lef", "*.lef"))
            elif pdk_root and pdk == "gf180mcuD":
                add_glob_tlef(os.path.join(pdk_root, "**", "gf180mcuD", "libs.tech", "lef", "*.tlef"))
                add_glob_lef(os.path.join(pdk_root, "**", "gf180mcuD", "libs.tech", "lef", "*.lef"))
                add_glob_lef(os.path.join(pdk_root, "**", "gf180mcuD", "libs.ref", "**", "lef", "*.lef"))

            # Write lefs.txt list (tech first)
            with open(os.path.join(view_dir, "lefs.txt"), "w") as f:
                for lef in tlefs:
                    f.write(lef + "\n")
                for lef in cell_lefs:
                    f.write(lef + "\n")

            # Collect liberty files (best effort) for GUI timing to stop errors
            lib_files: list[str] = []
            def add_lib_glob(pattern):
                for p in sorted(glob.glob(pattern, recursive=True)):
                    if p not in lib_files:
                        lib_files.append(p)

            if pdk_root and pdk == "sky130A":
                add_lib_glob(os.path.join(pdk_root, "**", "sky130A", "libs.ref", "sky130_fd_sc_hd", "lib", "*.lib"))
            elif pdk_root and pdk == "ihp-sg13g2":
                add_lib_glob(os.path.join(pdk_root, "**", "ihp-sg13g2", "libs.ref", "**", "lib", "*.lib"))
            elif pdk_root and pdk == "gf180mcuD":
                add_lib_glob(os.path.join(pdk_root, "**", "gf180mcuD", "libs.ref", "**", "lib", "*.lib"))

            with open(os.path.join(view_dir, "libs.txt"), "w") as f:
                for lib in lib_files:
                    f.write(lib + "\n")

            # OpenROAD helper script
            with open(os.path.join(view_dir, "openroad_open_view.tcl"), "w") as f:
                f.write("# Auto-generated viewer script\n")
                f.write(f"set view_dir {{{view_dir}}}\n")
                f.write(f"set def_file {{{def_path}}}\n")
                f.write(f"set odb_file {{{os.path.join(view_dir, os.path.basename(odb_abs))}}}\n")
                f.write(f"set libs_file {{{os.path.join(view_dir, 'libs.txt')}}}\n")
                f.write("# Try to read liberty files to quiet STA-related GUI warnings\n")
                f.write("if {[file exists $libs_file]} {\n")
                f.write("  set fh [open $libs_file r]\n")
                f.write("  set libs [split [read $fh] \n]\n")
                f.write("  close $fh\n")
                f.write("  foreach lib $libs { if {$lib ne \"\"} { catch { read_liberty $lib } } }\n")
                f.write("} else { catch { sta::suppress_message STA-2141 } }\n")
                f.write("set using_odb 0\n")
                f.write("if {[file exists $odb_file]} {\n")
                f.write("  puts \"Reading ODB: $odb_file\"\n")
                f.write("  read_db $odb_file\n")
                f.write("  set using_odb 1\n")
                f.write("}\n")
                f.write("# Always read LEFs as well to provide GUI geometry for stdcells/macros\n")
                for lef in tlefs:
                    # Heuristic: tech LEFs usually end with .tlef
                    f.write(f"read_lef -tech {{{lef}}}\n")
                for lef in cell_lefs:
                    f.write(f"read_lef {{{lef}}}\n")
                f.write("# If ODB not available, fall back to DEF\n")
                f.write("if {!$using_odb} { read_def $def_file }\n")
                f.write("# gui_show\n")

            # README with instructions
            with open(os.path.join(view_dir, "README.md"), "w") as f:
                f.write("""
Viewer bundle
=============

Files:
- design.def: exported from the current ODB
- lefs.txt: list of LEF files (tech/stdcell/macros)
- openroad_open_view.tcl: helper to open in OpenROAD GUI

Open in OpenROAD GUI:
  openroad -gui -no_init -files openroad_open_view.tcl

Open in KLayout (LEF/DEF importer):
  - File → Import → LEF/DEF, load LEFs from lefs.txt, then design.def

Notes:
- DEF-level BLOCKAGES and macro OBS should be visible.
- Routes and stdcell geometry depend on LEF completeness; add more LEFs if needed.
""".strip() + "\n")

            # Mirror to runs/<tag>/view for convenience (best effort)
            view_top = os.path.join(run_root, "view")
            try:
                if os.path.isdir(view_top):
                    shutil.rmtree(view_top, ignore_errors=True)
                shutil.copytree(view_dir, view_top, dirs_exist_ok=True)
            except Exception as mirror_err:
                print(f"[Viewer] WARNING: could not mirror view bundle to {view_top}: {mirror_err}")

            # KLayout helper script (self-contained: uses files in this folder)
            with open(os.path.join(view_dir, "klayout_open_view.py"), "w") as f:
                f.write(
                    ("""
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
    if hasattr(pya, "LefdefConfig"):
        cfg = pya.LefdefConfig()
        cfg.lef_files = tlefs + cell_lefs
        opt.lefdef_config = cfg
        lv.load_layout(def_path, "", opt)
    else:
        lv.load_layout(def_path, "")
        print("[KLayout] NOTE: LefdefConfig API not available. Use File → Import → LEF/DEF and select lefs.txt + design.def if needed.")
except Exception as e:
    print(f"[KLayout] ERROR: failed to load DEF: {e}")
""").strip()
                    + "\n"
                )

            return {}, {}

    # Insert the viewer step immediately after ApplyArt
    try:
        art_idx = _steps.index(ApplyArt)
        _steps.insert(art_idx + 1, ViewerPrepareBundle)
    except Exception:
        _steps.append(ViewerPrepareBundle)

    Steps = _steps
