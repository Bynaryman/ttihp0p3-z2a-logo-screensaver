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
@click_odb
def main(input_db, reader, image, grid, threshold, invert, area_pct, **_):
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

    # If an image is provided, decompose it into grid blockages
    if image:
        try:
            from PIL import Image, ImageOps, ImageStat, ImageDraw  # type: ignore
        except Exception as e:  # pragma: no cover
            print(f"[ApplyArt] ERROR: Pillow not available in container: {e}")
            reader.design.writeDb(input_db)
            return

        img = Image.open(image).convert("L")
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
                x1 = int((c + 1) * ih / rows) if False else int((c + 1) * iw / cols)
                region = img.crop((x0, y0, x1, y1))
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
                if odb is not None:
                    blk = odb.dbBlockage_create(block, llx, lly, urx, ury)  # type: ignore
                    if hasattr(blk, "setSoft"):
                        blk.setSoft()  # type: ignore
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
