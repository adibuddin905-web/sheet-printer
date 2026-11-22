"""Builds the print-ready PDF from the source PDF."""
try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

import geometry as g

MM = 72.0 / 25.4  # points per millimetre


def pt(mm):
    return mm * MM


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp")


def is_image(path):
    return path.lower().endswith(IMAGE_EXT)


def open_source(path, dpi=300):
    """Opens a PDF, or turns an image into a one-page PDF at the given DPI."""
    if not is_image(path):
        return fitz.open(path)
    pix = fitz.Pixmap(path)
    w = pix.width / float(dpi) * 72.0
    h = pix.height / float(dpi) * 72.0
    doc = fitz.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, filename=path)
    return doc


def design_size_mm(doc, page_index):
    r = doc[page_index].rect  # already includes the page's own rotation
    return r.width / MM, r.height / MM


def _draw_marks(page, plan, rows_used, cols_used, s):
    """Crop marks live in the margin, lined up with every cut line."""
    if not s["crop_marks"]:
        return
    length, off = s["mark_len"], s["mark_off"]
    shape = page.new_shape()
    left = plan["x0"]
    top = plan["y0"]
    right = plan["x0"] + cols_used * plan["cell_w"] + (cols_used - 1) * plan["gap_x"]
    bottom = plan["y0"] + rows_used * plan["cell_h"] + (rows_used - 1) * plan["gap_y"]
    xs = []
    for c in range(cols_used):
        x = plan["x0"] + c * (plan["cell_w"] + plan["gap_x"])
        xs += [x, x + plan["cell_w"]]
    ys = []
    for r in range(rows_used):
        y = plan["y0"] + r * (plan["cell_h"] + plan["gap_y"])
        ys += [y, y + plan["cell_h"]]
    for x in xs:  # vertical cut lines: marks above and below the grid
        shape.draw_line((pt(x), pt(top - off)), (pt(x), pt(top - off - length)))
        shape.draw_line((pt(x), pt(bottom + off)), (pt(x), pt(bottom + off + length)))
    for y in ys:  # horizontal cut lines: marks left and right of the grid
        shape.draw_line((pt(left - off), pt(y)), (pt(left - off - length), pt(y)))
        shape.draw_line((pt(right + off), pt(y)), (pt(right + off + length), pt(y)))
    shape.finish(color=(0, 0, 0), width=0.25)
    shape.commit()


def compose(src, s):
    """src: open fitz document. s: settings dict (mm). Returns (doc, info)."""
    pages = g.parse_pages(s["pages"], len(src))
    out = fitz.open()
    total_sheets = 0
    per_sheet_info = []
    for pi in pages:
        dw, dh = design_size_mm(src, pi)
        rot = s["rotation"]
        if rot == "auto":
            rot = g.best_rotation(s["paper_w"], s["paper_h"], s["margin"],
                                  dw, dh, s["gap_x"], s["gap_y"])
        plan = g.plan_sheet(s["paper_w"], s["paper_h"], s["margin"], dw, dh,
                            rot, s["gap_x"], s["gap_y"],
                            cols_rows=s.get("grid"), center=s["center"])
        if plan["per_sheet"] == 0:
            raise ValueError(
                "Design (%.1f x %.1f mm) does not fit on the paper with these "
                "margins." % (dw, dh))
        copies = plan["per_sheet"] if s["mode"] == "auto" else max(int(s["copies"]), 1)
        left = copies
        while left > 0:
            n = min(left, plan["per_sheet"])
            left -= n
            sheet = out.new_page(width=pt(s["paper_w"]), height=pt(s["paper_h"]))
            pos = g.cell_positions(plan, n)
            border = sheet.new_shape() if s.get("border") else None
            for (x, y) in pos:
                rect = fitz.Rect(pt(x), pt(y), pt(x + plan["cell_w"]), pt(y + plan["cell_h"]))
                sheet.show_pdf_page(rect, src, pi, rotate=int(rot))
                if border is not None:
                    border.draw_rect(rect)
            if border is not None:
                border.finish(color=(0, 0, 0), width=max(pt(s.get("border_w", 0.3)), 0.1))
                border.commit()
            if n >= plan["cols"]:
                cols_used, rows_used = plan["cols"], -(-n // plan["cols"])
            else:
                cols_used, rows_used = n, 1
            _draw_marks(sheet, plan, rows_used, cols_used, s)
            total_sheets += 1
        per_sheet_info.append((pi + 1, plan["per_sheet"], copies, rot))
    return out, {"sheets": total_sheets, "details": per_sheet_info}


def render_page(doc, index, dpi):
    """Return a PIL image of one sheet."""
    from PIL import Image
    pix = doc[index].get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
