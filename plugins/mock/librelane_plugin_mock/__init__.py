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
            route_layers_csv = os.getenv("TT_ART_ROUTE_LAYERS")

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
                except Exception:
                    pass

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
            if route_layers_csv:
                for part in route_layers_csv.split(","):
                    part = part.strip()
                    if part:
                        cmd += ["--route-layers", part]
            return cmd

    _steps.insert(insert_idx, ApplyArt)

    Steps = _steps
