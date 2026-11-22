"""Pure layout maths (no PDF library needed). All sizes are in millimetres."""
import math

EPS = 1e-6

PAPER_SIZES = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "Letter": (215.9, 279.4),
    "Legal": (215.9, 355.6),
    "Tabloid": (279.4, 431.8),
    "Custom": None,
}


def grid_capacity(usable_w, usable_h, cell_w, cell_h, gap_x, gap_y):
    """How many columns / rows of cells fit in the usable area."""
    if cell_w <= 0 or cell_h <= 0:
        return 0, 0
    cols = int(math.floor((usable_w + gap_x + EPS) / (cell_w + gap_x)))
    rows = int(math.floor((usable_h + gap_y + EPS) / (cell_h + gap_y)))
    return max(cols, 0), max(rows, 0)


def effective_cell(design_w, design_h, rotation):
    """Cell size on the sheet after rotating the design."""
    if rotation % 180 == 90:
        return design_h, design_w
    return design_w, design_h


def plan_sheet(paper_w, paper_h, margin, design_w, design_h, rotation,
               gap_x, gap_y, cols_rows=None, center=True):
    """Return a dict describing one sheet's grid.

    cols_rows: None = fill the sheet, or (cols, rows) to force a grid
    (it is trimmed to what physically fits).
    """
    cw, ch = effective_cell(design_w, design_h, rotation)
    uw = paper_w - 2 * margin
    uh = paper_h - 2 * margin
    max_c, max_r = grid_capacity(uw, uh, cw, ch, gap_x, gap_y)
    cols, rows = max_c, max_r
    if cols_rows:
        cols = min(max(cols_rows[0], 1), max_c)
        rows = min(max(cols_rows[1], 1), max_r)
    if cols <= 0 or rows <= 0:
        return {"cols": 0, "rows": 0, "cell_w": cw, "cell_h": ch,
                "x0": margin, "y0": margin, "per_sheet": 0,
                "max_cols": max_c, "max_rows": max_r}
    grid_w = cols * cw + (cols - 1) * gap_x
    grid_h = rows * ch + (rows - 1) * gap_y
    if center:
        x0 = (paper_w - grid_w) / 2.0
        y0 = (paper_h - grid_h) / 2.0
    else:
        x0, y0 = margin, margin
    return {"cols": cols, "rows": rows, "cell_w": cw, "cell_h": ch,
            "x0": x0, "y0": y0, "gap_x": gap_x, "gap_y": gap_y,
            "per_sheet": cols * rows, "max_cols": max_c, "max_rows": max_r,
            "grid_w": grid_w, "grid_h": grid_h}


def best_rotation(paper_w, paper_h, margin, design_w, design_h, gap_x, gap_y,
                  options=(0, 90)):
    """Pick the rotation that fits the most copies per sheet."""
    best, best_n = options[0], -1
    for r in options:
        p = plan_sheet(paper_w, paper_h, margin, design_w, design_h, r,
                       gap_x, gap_y)
        if p["per_sheet"] > best_n:
            best, best_n = r, p["per_sheet"]
    return best


def cell_positions(plan, count):
    """Top-left (x, y) of the first `count` cells, row by row."""
    out = []
    for i in range(count):
        r, c = divmod(i, plan["cols"])
        x = plan["x0"] + c * (plan["cell_w"] + plan["gap_x"])
        y = plan["y0"] + r * (plan["cell_h"] + plan["gap_y"])
        out.append((x, y))
    return out


def parse_pages(text, total):
    """'all' or '1,3-5' -> list of 0-based page indexes."""
    text = (text or "").strip().lower()
    if text in ("", "all"):
        return list(range(total))
    pages = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            rng = range(a, b + 1)
        else:
            rng = [int(part)]
        for n in rng:
            if n < 1 or n > total:
                raise ValueError("Page %d is outside 1-%d" % (n, total))
            pages.append(n - 1)
    if not pages:
        raise ValueError("No pages selected")
    return pages
