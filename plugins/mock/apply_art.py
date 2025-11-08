#!/usr/bin/env python3
# Minimal ODB Python script that opens the current ODB, performs no changes,
# and writes it back to the same path. Serves as a pure-Python placeholder to
# evolve into real in-place art insertion logic.

from reader import click, click_odb
from decimal import Decimal

try:
    import odb  # type: ignore
except Exception:  # pragma: no cover
    odb = None  # available at runtime inside the LibreLane container


@click.command()
@click.option("--image", type=str, required=False, help="Path to input image (PNG/JPG)")
@click.option("--grid", type=int, default=40, show_default=True, help="Grid columns (rows derived by aspect ratio)")
@click.option("--threshold", type=int, default=128, show_default=True, help="Luminance threshold [0..255]")
@click.option("--invert", is_flag=True, default=False, help="Invert selection (treat dark as blocked)")
@click.option("--area-pct", type=float, default=20.0, show_default=True, help="Artwork scale as percentage of core (0..100)")
@click.option(
    "--mode",
    type=click.Choice(["soft", "hard", "route"], case_sensitive=False),
    default="soft",
    show_default=True,
    help="Blockage type: placement soft, placement hard, or routing obstruction",
)
@click.option(
    "--route-layer",
    type=str,
    default=None,
    help="Deprecated: single routing layer (use --route-layers)",
)
@click.option(
    "--route-layers",
    multiple=True,
    help="Routing layer names for obstructions (repeat or comma-separated)",
)
@click_odb
def main(input_db, reader, image, grid, threshold, invert, area_pct, mode, route_layer, route_layers, **_):
    """
    In-place ODB edit:
    - Print core and die sizes/areas
    - If --image provided, rasterize into a grid and add soft placement
      blockages for selected cells; also write an 'art_preview.png' in the
      step directory showing the chosen cells
    - Otherwise, add a small routing obstruction square at the core center
    - Write back to the same ODB path
    """
    block = reader.block
    tech = reader.tech
    dbu = tech.getDbUnitsPerMicron()

    # Fetch die/core areas
    die = block.getDieArea()
    core = None
    if hasattr(block, "getCoreArea"):
        core = block.getCoreArea()

    def bbox_info(b):
        w = Decimal(b.xMax() - b.xMin()) / Decimal(dbu)
        h = Decimal(b.yMax() - b.yMin()) / Decimal(dbu)
        return (w, h, w * h)

    die_w, die_h, die_area = bbox_info(die)
    print(f"[ApplyArt] Die size: {die_w} x {die_h} um  area={die_area} um^2")

    if core is not None:
        core_w, core_h, core_area = bbox_info(core)
        print(f"[ApplyArt] Core size: {core_w} x {core_h} um  area={core_area} um^2")
    else:
        core = die  # fallback
        core_w, core_h, core_area = die_w, die_h, die_area
        print("[ApplyArt] Core bbox not available; using die bbox as fallback")

    # Center point
    cx = (core.xMin() + core.xMax()) // 2
    cy = (core.yMin() + core.yMax()) // 2

    def create_placement_blockage(llx, lly, urx, ury):
        if odb is None:
            return
        blk = odb.dbBlockage_create(block, llx, lly, urx, ury)  # type: ignore
        if mode.lower() == "soft" and hasattr(blk, "setSoft"):
            blk.setSoft()  # type: ignore
        elif mode.lower() == "hard":
            if hasattr(blk, "setSoft"):
                try:
                    blk.setSoft(False)  # type: ignore[arg-type]
                except TypeError:
                    pass
            if hasattr(blk, "setMaxDensity"):
                try:
                    blk.setMaxDensity(0.0)  # type: ignore[attr-defined]
                except Exception:
                    pass

    def find_layer_by_name(tech_obj, name: str):
        try:
            lyr = tech_obj.findLayer(name)
            if lyr:
                return lyr
        except Exception:
            pass
        for lyr in tech_obj.getLayers():
            try:
                if lyr.getName() == name:
                    return lyr
            except Exception:
                continue
        return None

    # Normalize route layers: accept --route-layer or --route-layers and CSV
    route_layer_list = []
    if route_layer:
        route_layer_list.append(route_layer)
    for rl in route_layers or ():
        route_layer_list.extend([p.strip() for p in rl.split(",") if p.strip()])

    def create_route_obstruction(llx, lly, urx, ury):
        if odb is None:
            return
        if not route_layer_list:
            return
        for rl in route_layer_list:
            lyr = find_layer_by_name(tech, rl)
            if lyr is None:
                raise click.ClickException(f"ApplyArt: route layer '{rl}' not found in tech")
            try:
                odb.dbObstruction_create(block, lyr, llx, lly, urx, ury)  # type: ignore
            except Exception as e:
                raise click.ClickException(f"ApplyArt: failed to create route obstruction on {rl}: {e}")

    # Decide what to create per cell
    want_place = mode.lower() in ("soft", "hard")
    want_route = (mode.lower() == "route") or bool(route_layer_list)
    print(
        f"[ApplyArt] Mode={mode} placement={'yes' if want_place else 'no'} routing_layers={route_layer_list}"
    )

    # If an image is provided, decompose it into grid blockages
    if image:
        try:
            from PIL import Image, ImageOps, ImageStat, ImageDraw  # type: ignore
        except Exception as e:  # pragma: no cover
            print(f"[ApplyArt] ERROR: Pillow not available in container: {e}")
            reader.design.writeDb(input_db)
            return

        try:
            img = Image.open(image).convert("L")
        except FileNotFoundError:
            from click import ClickException as _CE  # type: ignore
            raise _CE(f"ApplyArt: image not found: {image} (use absolute path or ensure it is mounted)")
        except Exception as e:  # UnidentifiedImageError or other PIL errors
            from click import ClickException as _CE  # type: ignore
            raise _CE(f"ApplyArt: cannot open image '{image}': {e}. Re-save as PNG/JPG (8-bit) and retry.")
        img = ImageOps.flip(img)
        iw, ih = img.size
        cols = max(1, grid)
        rows = max(1, int(round(ih * cols / iw)))
        print(f"[ApplyArt] Rasterizing {image} to grid {cols}x{rows}, threshold={threshold}, invert={invert}, area={area_pct}%")

        # Scale image into core with area_pct
        scale = max(0.0, min(1.0, float(area_pct) / 100.0))
        core_w_dbu = core.xMax() - core.xMin()
        core_h_dbu = core.yMax() - core.yMin()
        pad_x = int(core_w_dbu * (1.0 - scale) / 2)
        pad_y = int(core_h_dbu * (1.0 - scale) / 2)
        offset_x = core.xMin() + pad_x
        offset_y = core.yMin() + pad_y
        cell_w = max(1, int(core_w_dbu * scale / cols))
        cell_h = max(1, int(core_h_dbu * scale / rows))

        # Prepare a simple preview image (binary grid visualization)
        preview_scale = 8  # pixels per cell for preview only
        preview = Image.new("L", (cols * preview_scale, rows * preview_scale), 0)
        draw = ImageDraw.Draw(preview)

        placed = 0
        for r in range(rows):
            y0 = int(r * ih / rows)
            y1 = int((r + 1) * ih / rows)
            for c in range(cols):
                x0 = int(c * iw / cols)
                x1 = int((c + 1) * iw / cols)
                if x1 <= x0 or y1 <= y0:
                    continue
                region = img.crop((x0, y0, x1, y1))
                if region.width == 0 or region.height == 0:
                    continue
                lum = ImageStat.Stat(region).mean[0]
                on = lum >= threshold
                if invert:
                    on = not on
                if not on:
                    continue
                llx = offset_x + c * cell_w
                lly = offset_y + r * cell_h
                urx = llx + cell_w
                ury = lly + cell_h
                if want_place:
                    create_placement_blockage(llx, lly, urx, ury)
                if want_route:
                    create_route_obstruction(llx, lly, urx, ury)
                placed += 1
                # mark cell in preview
                px0 = c * preview_scale
                py0 = r * preview_scale
                px1 = px0 + preview_scale
                py1 = py0 + preview_scale
                draw.rectangle((px0, py0, px1 - 1, py1 - 1), fill=255)
        print(f"[ApplyArt] Placed {placed} placement blockages from image")

        # Save preview alongside step logs (current working directory)
        try:
            preview_path = "art_preview.png"
            preview.save(preview_path)
            print(f"[ApplyArt] Preview saved to {preview_path}")
        except Exception as e:  # pragma: no cover
            print(f"[ApplyArt] WARNING: Failed to save preview: {e}")
    else:
        # No image provided: treat as configuration error to avoid silent fallback.
        # This ensures --with-art runs fail fast unless TT_ART_IMAGE is set.
        raise click.ClickException(
            "ApplyArt: no image provided (set TT_ART_IMAGE or pass --image)"
        )

    # Write back in place
    reader.design.writeDb(input_db)


if __name__ == "__main__":
    # The click wrapper is returned by the decorator; call it.
    # mypy/click type confusion aside, this entrypoint is required when
    # running via "openroad -python apply_art.py ...".
    main()
