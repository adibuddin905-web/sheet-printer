"""Sheet Printer - put PDFs / images on any paper size and print them.

Everything lives in this ONE file (plus version.py, which GitHub fills in).
"""
import io
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import webbrowser
from tkinter import filedialog, messagebox, ttk

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz
from PIL import Image, ImageDraw, ImageOps

from version import __version__

# ===========================================================================
# Layout maths (millimetres)
# ===========================================================================
EPS = 1e-6
MM = 72.0 / 25.4  # points per millimetre
UNITS = {"mm": 1.0, "cm": 10.0, "inch": 25.4}
DECIMALS = {"mm": 2, "cm": 3, "inch": 3}
PAPER_SIZES = {
    "A3": (297.0, 420.0), "A4": (210.0, 297.0), "A5": (148.0, 210.0),
    "A6": (105.0, 148.0), "Letter": (215.9, 279.4), "Legal": (215.9, 355.6),
    "Tabloid": (279.4, 431.8), "Custom": None,
}
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".gif", ".webp")
ARRANGE_MODES = {"Best fit (saves paper)": "best",
                 "In order: across rows": "rows",
                 "In order: down columns": "cols"}
FITZ_LOCK = threading.RLock()  # PyMuPDF is used from several threads: one at a time


def pt(mm):
    return mm * MM


def grid_capacity(usable_w, usable_h, cell_w, cell_h, gap_x, gap_y):
    if cell_w <= 0 or cell_h <= 0:
        return 0, 0
    cols = int(math.floor((usable_w + gap_x + EPS) / (cell_w + gap_x)))
    rows = int(math.floor((usable_h + gap_y + EPS) / (cell_h + gap_y)))
    return max(cols, 0), max(rows, 0)


def effective_cell(design_w, design_h, rotation):
    if rotation % 180 == 90:
        return design_h, design_w
    return design_w, design_h


def plan_sheet(paper_w, paper_h, margin, design_w, design_h, rotation,
               gap_x, gap_y, cols_rows=None, center=True):
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
                "gap_x": gap_x, "gap_y": gap_y}
    grid_w = cols * cw + (cols - 1) * gap_x
    grid_h = rows * ch + (rows - 1) * gap_y
    if center:
        x0 = (paper_w - grid_w) / 2.0
        y0 = (paper_h - grid_h) / 2.0
    else:
        x0, y0 = margin, margin
    return {"cols": cols, "rows": rows, "cell_w": cw, "cell_h": ch,
            "x0": x0, "y0": y0, "gap_x": gap_x, "gap_y": gap_y,
            "per_sheet": cols * rows}


def best_rotation(paper_w, paper_h, margin, design_w, design_h, gap_x, gap_y,
                  options=(0, 90)):
    best, best_n = options[0], -1
    for r in options:
        p = plan_sheet(paper_w, paper_h, margin, design_w, design_h, r, gap_x, gap_y)
        if p["per_sheet"] > best_n:
            best, best_n = r, p["per_sheet"]
    return best


def cell_positions(plan, count):
    out = []
    for i in range(count):
        r, c = divmod(i, plan["cols"])
        out.append((plan["x0"] + c * (plan["cell_w"] + plan["gap_x"]),
                    plan["y0"] + r * (plan["cell_h"] + plan["gap_y"])))
    return out


def parse_pages(text, total):
    """'all' or '1,3-5' -> list of 0-based page numbers."""
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


def _orient(w, h, rot):
    return (h, w) if rot in (90, 270) else (w, h)


def _shelf_pack(sizes, uw, uh, gap_x, gap_y):
    order = sorted(sizes, key=lambda t: -t[2])  # tallest first
    sheets = []
    for (idx, w, h, rot) in order:
        placed = False
        for sh in sheets:
            for shelf in sh["shelves"]:
                if h <= shelf["h"] + EPS and shelf["x"] + gap_x + w <= uw + EPS:
                    x = shelf["x"] + gap_x
                    shelf["items"].append((idx, x, w, h, rot))
                    shelf["x"] = x + w
                    placed = True
                    break
            if placed:
                break
            y = sh["used"] + gap_y
            if y + h <= uh + EPS:
                sh["shelves"].append({"y": y, "h": h, "x": w,
                                      "items": [(idx, 0.0, w, h, rot)]})
                sh["used"] = y + h
                placed = True
                break
        if not placed:
            sheets.append({"used": h, "shelves": [
                {"y": 0.0, "h": h, "x": w, "items": [(idx, 0.0, w, h, rot)]}]})
    return sheets


def _flatten_shelves(sheets):
    flat = []
    for sh in sheets:
        items = []
        for shelf in sh["shelves"]:
            for (idx, x, w, h, rot) in shelf["items"]:
                items.append((idx, x, shelf["y"], w, h, rot))
        flat.append({"cw": max(shelf["x"] for shelf in sh["shelves"]),
                     "ch": sh["used"], "items": items})
    return flat


def _ordered_pack(sizes, uw, uh, gap_x, gap_y, by_rows):
    """Keeps the order of the list. sizes: [(idx, w, h, rot)].
    by_rows=True: 1 2 3 / 4 5 6.   by_rows=False: 1 4 / 2 5 / 3 6 (down columns)."""
    if not by_rows:
        tr = [(i, h, w, r) for (i, w, h, r) in sizes]
        flat = _ordered_pack(tr, uh, uw, gap_y, gap_x, True)
        return [{"cw": sh["ch"], "ch": sh["cw"],
                 "items": [(i, y, x, h, w, r) for (i, x, y, w, h, r) in sh["items"]]}
                for sh in flat]
    sheets, cur = [], None
    for (idx, w, h, rot) in sizes:
        line = cur["lines"][-1] if cur else None
        if line is not None:
            nx = line["x"] + gap_x + w
            nth = max(line["th"], h)
            if nx <= uw + EPS and line["y"] + nth <= uh + EPS:
                line["items"].append((idx, line["x"] + gap_x, w, h, rot))
                line["x"], line["th"] = nx, nth
                continue
            y = line["y"] + line["th"] + gap_y
            if y + h <= uh + EPS:
                cur["lines"].append({"y": y, "th": h, "x": w,
                                     "items": [(idx, 0.0, w, h, rot)]})
                continue
        cur = {"lines": [{"y": 0.0, "th": h, "x": w, "items": [(idx, 0.0, w, h, rot)]}]}
        sheets.append(cur)
    flat = []
    for sh in sheets:
        items = []
        for ln in sh["lines"]:
            for (idx, x, w, h, rot) in ln["items"]:
                items.append((idx, x, ln["y"], w, h, rot))
        last = sh["lines"][-1]
        flat.append({"cw": max(ln["x"] for ln in sh["lines"]),
                     "ch": last["y"] + last["th"], "items": items})
    return flat


def pack_mixed(items, paper_w, paper_h, margin, gap_x, gap_y, rotation="auto",
               center=True, arrange="best"):
    uw = paper_w - 2 * margin
    uh = paper_h - 2 * margin
    n = len(items)
    best = None
    if arrange in ("rows", "cols"):
        sizes = []
        for i, (w, h) in enumerate(items):
            if rotation == "auto":
                rot = 0 if (w <= uw + EPS and h <= uh + EPS) else 90
            else:
                rot = int(rotation)
            ow, oh = _orient(w, h, rot)
            if ow > uw + EPS or oh > uh + EPS:
                raise ValueError("A design does not fit on the paper with these margins.")
            sizes.append((i, ow, oh, rot))
        best = _ordered_pack(sizes, uw, uh, gap_x, gap_y, arrange == "rows")
    else:
        if rotation == "auto":
            variants = [[0] * n, [90] * n,
                        [0 if w >= h else 90 for (w, h) in items],
                        [0 if h >= w else 90 for (w, h) in items],
                        [0 if (w <= uw + EPS and h <= uh + EPS) else 90 for (w, h) in items]]
        else:
            variants = [[int(rotation)] * n]
        best_score = None
        for rv in variants:
            sizes, ok = [], True
            for i, (w, h) in enumerate(items):
                ow, oh = _orient(w, h, rv[i])
                if ow > uw + EPS or oh > uh + EPS:
                    ok = False
                    break
                sizes.append((i, ow, oh, rv[i]))
            if not ok:
                continue
            flat = _flatten_shelves(_shelf_pack(sizes, uw, uh, gap_x, gap_y))
            score = (len(flat), sum(sh["ch"] for sh in flat))
            if best_score is None or score < best_score:
                best_score, best = score, flat
        if best is None:
            raise ValueError("A design does not fit on the paper with these margins.")
    placements = []
    for si, sh in enumerate(best):
        ox = margin + ((uw - sh["cw"]) / 2.0 if center else 0.0)
        oy = margin + ((uh - sh["ch"]) / 2.0 if center else 0.0)
        for (idx, x, y, w, h, rot) in sh["items"]:
            placements.append({"idx": idx, "sheet": si, "x": ox + x, "y": oy + y,
                               "w": w, "h": h, "rot": rot})
    return placements, len(best)


# ===========================================================================
# Building the print PDF
# ===========================================================================
_DOC_CACHE = {}


def is_image(path):
    return path.lower().endswith(IMAGE_EXT)


def _image_pixels(path):
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        pix = fitz.Pixmap(path)
        return pix.width, pix.height


def get_doc(path, dpi):
    """Opens a PDF or turns an image into a one-page PDF (cached)."""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0
    key = (path, int(dpi) if is_image(path) else 0, stamp)
    doc = _DOC_CACHE.get(key)
    if doc is not None:
        return doc
    if is_image(path):
        pw, ph = _image_pixels(path)
        doc = fitz.open()
        page = doc.new_page(width=pw / float(dpi) * 72.0, height=ph / float(dpi) * 72.0)
        page.insert_image(page.rect, filename=path)
    else:
        doc = fitz.open(path)
    if len(_DOC_CACHE) > 8:
        _DOC_CACHE.clear()
    _DOC_CACHE[key] = doc
    return doc


_INV_CACHE = {}


def inverted_doc(doc, pno, full, regions, dpi):
    """One-page PDF holding the page as a picture with colours inverted.
    full: invert everything.  regions: list of polygons (points 0..1 of the page);
    inside a polygon the colours are flipped again (so full + area = area stays normal)."""
    regions = tuple(tuple((float(x), float(y)) for (x, y) in r) for r in (regions or ()))
    key = (id(doc), pno, bool(full), regions, int(round(dpi)))
    hit = _INV_CACHE.get(key)
    if hit is not None:
        return hit[1]
    page = doc[pno]
    rect = page.rect
    zoom = max(dpi, 72.0) / 72.0
    zoom = min(zoom, 8000.0 / max(rect.width, rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    inv = ImageOps.invert(img)
    if regions:
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        for poly in regions:
            if len(poly) >= 3:
                draw.polygon([(x * (img.width - 1), y * (img.height - 1)) for (x, y) in poly],
                             fill=255)
        base, other = (inv, img) if full else (img, inv)
        result = Image.composite(other, base, mask)
    else:
        result = inv if full else img
    buf = io.BytesIO()
    result.save(buf, "PNG", compress_level=1)
    new = fitz.open()
    p = new.new_page(width=rect.width, height=rect.height)
    p.insert_image(p.rect, stream=buf.getvalue())
    if len(_INV_CACHE) > 12:
        _INV_CACHE.clear()
    _INV_CACHE[key] = (doc, new)  # keeps the source alive so id(doc) stays valid
    return new


def design_size_mm(doc, page_index):
    r = doc[page_index].rect
    return r.width / MM, r.height / MM


_REUSE_OK = [True]


def _show(page, rect, doc, pno, rot, cache):
    """Place a page. Newer PyMuPDF versions store the same source page once by
    themselves; older ones accept reuse_xref (used here when available)."""
    key = (id(doc), pno)
    xr = 0
    if _REUSE_OK[0]:
        try:
            xr = page.show_pdf_page(rect, doc, pno, rotate=int(rot),
                                    reuse_xref=cache.get(key, 0))
        except TypeError:  # this PyMuPDF has no reuse_xref option
            _REUSE_OK[0] = False
            xr = page.show_pdf_page(rect, doc, pno, rotate=int(rot))
    else:
        xr = page.show_pdf_page(rect, doc, pno, rotate=int(rot))
    if isinstance(xr, int) and xr > 0:
        cache[key] = xr


def _draw_marks(page, plan, rows_used, cols_used, s):
    if not s["crop_marks"]:
        return
    length, off = s["mark_len"], s["mark_off"]
    shape = page.new_shape()
    left, top = plan["x0"], plan["y0"]
    right = left + cols_used * plan["cell_w"] + (cols_used - 1) * plan["gap_x"]
    bottom = top + rows_used * plan["cell_h"] + (rows_used - 1) * plan["gap_y"]
    xs, ys = [], []
    for c in range(cols_used):
        x = left + c * (plan["cell_w"] + plan["gap_x"])
        xs += [x, x + plan["cell_w"]]
    for r in range(rows_used):
        y = top + r * (plan["cell_h"] + plan["gap_y"])
        ys += [y, y + plan["cell_h"]]
    for x in xs:
        shape.draw_line((pt(x), pt(top - off)), (pt(x), pt(top - off - length)))
        shape.draw_line((pt(x), pt(bottom + off)), (pt(x), pt(bottom + off + length)))
    for y in ys:
        shape.draw_line((pt(left - off), pt(y)), (pt(left - off - length), pt(y)))
        shape.draw_line((pt(right + off), pt(y)), (pt(right + off + length), pt(y)))
    shape.finish(color=(0, 0, 0), width=0.25)
    shape.commit()


def _fit_scale(s, dw, dh, rot):
    cw, ch = effective_cell(dw, dh, rot)
    uw = s["paper_w"] - 2 * s["margin"]
    uh = s["paper_h"] - 2 * s["margin"]
    if uw <= 0 or uh <= 0 or cw <= 0 or ch <= 0:
        return 0.0
    return min(uw / cw, uh / ch)


def compose(src, s):
    """One design (every selected page) repeated on the sheets."""
    pages = parse_pages(s["pages"], len(src))
    out = fitz.open()
    cache = {}
    keep = []
    total_sheets = 0
    details = []
    for pi in pages:
        dw, dh = design_size_mm(src, pi)
        k = s.get("scale", 1.0)
        dw, dh = dw * k, dh * k
        src_pg, src_no = src, pi
        inv = s.get("invert") or {}
        if inv.get("full") or inv.get("regions"):
            d = max(s.get("invert_dpi", 300) * k,
                    s.get("image_dpi", 300) if s.get("src_is_image") else 0, 72)
            src_pg = inverted_doc(src, pi, inv.get("full"), inv.get("regions") or (), d)
            keep.append(src_pg)
            src_no = 0
        rot = s["rotation"]
        if rot == "auto":
            rot = best_rotation(s["paper_w"], s["paper_h"], s["margin"], dw, dh,
                                s["gap_x"], s["gap_y"])
        plan = plan_sheet(s["paper_w"], s["paper_h"], s["margin"], dw, dh, rot,
                          s["gap_x"], s["gap_y"], cols_rows=s.get("grid"), center=s["center"])
        if plan["per_sheet"] == 0:  # bigger than the paper: shrink to fit
            if s["rotation"] == "auto":
                rot = max((0, 90), key=lambda r: _fit_scale(s, dw, dh, r))
            sc = min(_fit_scale(s, dw, dh, rot), 1.0) * 0.9995
            if sc > 0.001:
                dw, dh = dw * sc, dh * sc
                plan = plan_sheet(s["paper_w"], s["paper_h"], s["margin"], dw, dh, rot,
                                  s["gap_x"], s["gap_y"], cols_rows=s.get("grid"),
                                  center=s["center"])
        if plan["per_sheet"] == 0:
            raise ValueError("Design (%.1f x %.1f mm) does not fit on the paper with "
                             "these margins." % (dw, dh))
        copies = plan["per_sheet"] if s["mode"] == "auto" else max(int(s["copies"]), 1)
        left = copies
        while left > 0:
            n = min(left, plan["per_sheet"])
            left -= n
            sheet = out.new_page(width=pt(s["paper_w"]), height=pt(s["paper_h"]))
            border = sheet.new_shape() if s.get("border") else None
            for (x, y) in cell_positions(plan, n):
                rect = fitz.Rect(pt(x), pt(y), pt(x + plan["cell_w"]), pt(y + plan["cell_h"]))
                _show(sheet, rect, src_pg, src_no, rot, cache)
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
        details.append((pi + 1, plan["per_sheet"], copies, rot, dw, dh))
    return out, {"sheets": total_sheets, "details": details}


def compose_mix(items, s):
    """Many different designs on the same sheet(s)."""
    uw = s["paper_w"] - 2 * s["margin"]
    uh = s["paper_h"] - 2 * s["margin"]
    if uw <= 0 or uh <= 0:
        raise ValueError("Margins are bigger than the paper")
    rotation = s["rotation"]
    sizes, owner = [], []
    shrunk = False
    for i, it in enumerate(items):
        dw, dh = design_size_mm(it["doc"], it["page"])
        dw, dh = dw * it["scale"], dh * it["scale"]
        if rotation == "auto":
            fit = max(min(uw / dw, uh / dh), min(uw / dh, uh / dw))
        else:
            ow, oh = _orient(dw, dh, int(rotation))
            fit = min(uw / ow, uh / oh)
        if fit < 1.0:
            dw, dh = dw * fit * 0.9995, dh * fit * 0.9995
            shrunk = True
        for _ in range(max(int(it["copies"]), 0)):
            sizes.append((dw, dh))
            owner.append(i)
    if not sizes:
        raise ValueError("Set at least 1 copy for a design")
    placements, n_sheets = pack_mixed(sizes, s["paper_w"], s["paper_h"], s["margin"],
                                      s["gap_x"], s["gap_y"], rotation, s["center"],
                                      s.get("arrange", "best"))
    out = fitz.open()
    for _ in range(n_sheets):
        out.new_page(width=pt(s["paper_w"]), height=pt(s["paper_h"]))
    cache = {}
    keep = []
    for si in range(n_sheets):  # PyMuPDF: fetch each page fresh after all are created
        page = out[si]
        border = page.new_shape() if s.get("border") else None
        for pl in placements:
            if pl["sheet"] != si:
                continue
            it = items[owner[pl["idx"]]]
            rect = fitz.Rect(pt(pl["x"]), pt(pl["y"]), pt(pl["x"] + pl["w"]), pt(pl["y"] + pl["h"]))
            d_doc, d_no = it["doc"], it["page"]
            if it.get("inv_full") or it.get("regions"):
                d = max(s.get("invert_dpi", 300) * it["scale"],
                        s.get("image_dpi", 300) if is_image(it["path"]) else 0, 72)
                d_doc = inverted_doc(it["doc"], it["page"], it.get("inv_full"),
                                     it.get("regions") or (), d)
                keep.append(d_doc)
                d_no = 0
            _show(page, rect, d_doc, d_no, pl["rot"], cache)
            if border is not None:
                border.draw_rect(rect)
        if border is not None:
            border.finish(color=(0, 0, 0), width=max(pt(s.get("border_w", 0.3)), 0.1))
            border.commit()
    return out, {"sheets": n_sheets, "placed": len(placements), "shrunk": shrunk}


def render_preview(doc, index, max_side=1600):
    """Sheet picture for the screen (one render, then just resized)."""
    page = doc[index]
    zoom = max_side / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


# ===========================================================================
# Windows printing
# ===========================================================================
def list_printers():
    import win32print
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [p[2] for p in win32print.EnumPrinters(flags)]
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = names[0] if names else ""
    return names, default


def print_document(doc, printer_name, copies=1, title="Sheet Printer", max_dpi=600):
    import win32con
    import win32ui
    from PIL import ImageWin

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    dpi = min(max(hdc.GetDeviceCaps(win32con.LOGPIXELSX),
                  hdc.GetDeviceCaps(win32con.LOGPIXELSY)), max_dpi)
    phys_w = hdc.GetDeviceCaps(win32con.PHYSICALWIDTH)
    phys_h = hdc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
    off_x = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
    off_y = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
    hdc.StartDoc(title)
    try:
        for _ in range(max(int(copies), 1)):
            for i in range(len(doc)):
                with FITZ_LOCK:
                    pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
                    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                hdc.StartPage()
                ImageWin.Dib(img).draw(hdc.GetHandleOutput(),
                                       (-off_x, -off_y, -off_x + phys_w, -off_y + phys_h))
                hdc.EndPage()
        hdc.EndDoc()
    except Exception:
        hdc.AbortDoc()
        raise
    finally:
        hdc.DeleteDC()


def open_printer_preferences(printer_name):
    subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", printer_name])


# ===========================================================================
# Updater (GitHub releases)
# ===========================================================================
GITHUB_USER = "adibuddin905-web"
GITHUB_REPO = "sheet-printer"
ASSET_NAME = "SheetPrinter.exe"
API = "https://api.github.com/repos/%s/%s/releases/latest" % (GITHUB_USER, GITHUB_REPO)
RELEASES_PAGE = "https://github.com/%s/%s/releases" % (GITHUB_USER, GITHUB_REPO)


def _ver(text):
    nums = []
    for p in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def check_latest():
    req = urllib.request.Request(API, headers={"User-Agent": "SheetPrinter"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    url = None
    for a in data.get("assets", []):
        if a.get("name") == ASSET_NAME:
            url = a.get("browser_download_url")
    return tag, url, _ver(tag) > _ver(__version__)


def download_and_replace(url):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-update only works in the installed .exe version.")
    exe = sys.executable
    tmp_dir = tempfile.mkdtemp(prefix="sheetprinter_")
    new_exe = os.path.join(tmp_dir, "SheetPrinter_new.exe")
    req = urllib.request.Request(url, headers={"User-Agent": "SheetPrinter"})
    with urllib.request.urlopen(req, timeout=120) as r, open(new_exe, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
    bat = os.path.join(tmp_dir, "update.bat")
    with open(bat, "w") as f:
        f.write("@echo off\r\n"
                "ping 127.0.0.1 -n 3 >nul\r\n"
                ":retry\r\n"
                'move /Y "%s" "%s" >nul 2>&1\r\n'
                "if errorlevel 1 (ping 127.0.0.1 -n 2 >nul & goto retry)\r\n"
                'start "" "%s"\r\n' % (new_exe, exe, exe))
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)


# ===========================================================================
# The app window
# ===========================================================================
def clean_number(text):
    try:
        v = float(str(text).replace(",", "."))
    except ValueError:
        return text
    return ("%.4f" % v).rstrip("0").rstrip(".")


class AreaSelector(tk.Toplevel):
    """Window to draw (freehand or rectangle) the areas whose colours get inverted."""

    def __init__(self, master, img, regions, base_inverted, on_done):
        super().__init__(master)
        self.title("Select area to invert")
        self.transient(master)
        self.on_done = on_done
        scale = min(900.0 / img.width, 640.0 / img.height)
        self.w, self.h = max(int(img.width * scale), 50), max(int(img.height * scale), 50)
        shown = img.resize((self.w, self.h), Image.LANCZOS)
        flipped = ImageOps.invert(shown)
        self.base, self.other = (flipped, shown) if base_inverted else (shown, flipped)
        self.regions = [[(x * self.w, y * self.h) for (x, y) in r] for r in regions]
        self.cur = None
        self.start = None
        self.tool = tk.StringVar(value="free")
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill="x")
        ttk.Radiobutton(bar, text="Freehand (lasso)", variable=self.tool, value="free").pack(side="left")
        ttk.Radiobutton(bar, text="Rectangle", variable=self.tool, value="rect").pack(side="left", padx=8)
        ttk.Button(bar, text="Undo last", command=self.undo).pack(side="left", padx=4)
        ttk.Button(bar, text="Clear all", command=self.clear).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Done", command=self.finish).pack(side="right", padx=6)
        ttk.Label(self, text="Hold the left mouse button and draw. Selected areas are shown inverted.",
                  foreground="#555").pack(anchor="w", padx=8)
        self.canvas = tk.Canvas(self, width=self.w, height=self.h, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=8, pady=8)
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.redraw()
        self.grab_set()

    def redraw(self):
        from PIL import ImageTk
        mask = Image.new("L", (self.w, self.h), 0)
        d = ImageDraw.Draw(mask)
        for poly in self.regions:
            if len(poly) >= 3:
                d.polygon(poly, fill=255)
        self._photo = ImageTk.PhotoImage(Image.composite(self.other, self.base, mask))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        for poly in self.regions:
            if len(poly) >= 3:
                self.canvas.create_polygon(poly, outline="#e00000", fill="", width=2)

    def _clip(self, e):
        return (min(max(e.x, 0), self.w), min(max(e.y, 0), self.h))

    def press(self, e):
        self.start = self._clip(e)
        self.cur = [self.start]

    def drag(self, e):
        if self.cur is None:
            return
        p = self._clip(e)
        self.canvas.delete("tmp")
        if self.tool.get() == "rect":
            self.canvas.create_rectangle(self.start[0], self.start[1], p[0], p[1],
                                         outline="#e00000", width=2, tags="tmp")
        else:
            self.cur.append(p)
            if len(self.cur) > 1:
                self.canvas.create_line(self.cur, fill="#e00000", width=2, tags="tmp")

    def release(self, e):
        if self.cur is None:
            return
        p = self._clip(e)
        if self.tool.get() == "rect":
            x0, y0 = self.start
            poly = [(x0, y0), (p[0], y0), (p[0], p[1]), (x0, p[1])]
            ok = abs(p[0] - x0) > 2 and abs(p[1] - y0) > 2
        else:
            poly = self.cur + [p]
            ok = len(poly) >= 3
        self.cur = None
        if ok:
            self.regions.append(poly)
        self.redraw()

    def undo(self):
        if self.regions:
            self.regions.pop()
            self.redraw()

    def clear(self):
        self.regions = []
        self.redraw()

    def finish(self):
        out = [[(x / self.w, y / self.h) for (x, y) in r] for r in self.regions if len(r) >= 3]
        self.destroy()
        self.on_done(out)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sheet Printer  v%s" % __version__)
        self.geometry("1180x760")
        self.minsize(980, 640)
        try:
            self.state("zoomed")
        except tk.TclError:
            pass
        self.q = queue.Queue()      # results coming back from background threads
        self.busy = False           # a sheet is being built right now
        self.pending = False        # settings changed while busy -> build again
        self.src_path = ""
        self.out = None             # finished print PDF
        self.preview_index = 0
        self.preview_pil = None
        self._photo = None
        self._photo_src = None
        self._photo_size = None
        self._after = None
        self._draw_after = None
        self._muting = False
        self.unit = "mm"
        self.items = []
        self.single_regions = []
        self._next_id = 1
        self.mm_spins = []

        self.v = {}
        for name, default in [
            ("pages", "all"), ("preset", "A4"), ("orient", "Portrait"),
            ("paper_w", "210"), ("paper_h", "297"), ("margin", "10"),
            ("gap_x", "5"), ("gap_y", "5"), ("rotation", "0"),
            ("mode", "auto"), ("copies", "10"), ("columns", "0"),
            ("center", "1"), ("crop", "1"), ("border", "0"), ("border_w", "0.3"),
            ("image_dpi", "300"), ("scale", "100"), ("mark_len", "4"), ("mark_off", "1"),
            ("unit", "mm"), ("printer", ""), ("sheet_copies", "1"),
            ("layout", "single"), ("item_copies", "1"), ("item_scale", "100"),
            ("arrange", "Best fit (saves paper)"), ("inv_single", "0"), ("invert_dpi", "300"),
        ]:
            self.v[name] = tk.StringVar(value=default)
        for name, var in self.v.items():
            if name not in ("unit", "printer", "sheet_copies", "item_copies", "item_scale"):
                var.trace_add("write", lambda *a: self.schedule())

        self._build_ui()
        self._load_printers()
        self.after(40, self._poll)
        self.after(1500, lambda: self.check_update(silent=True))

    # ---------- small helpers ----------
    def _poll(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                self.status.config(text="Problem: %s" % e, foreground="#b00020")
        self.after(40, self._poll)

    def spin(self, parent, key, step, kind="int", lo=0, hi=100000, width=9):
        """Number box with up/down arrows (also the keyboard Up / Down keys)."""
        fmt = "%.3f" if kind == "mm" else "%.0f"
        sp = ttk.Spinbox(parent, textvariable=self.v[key], from_=lo, to=hi,
                         increment=step, width=width, format=fmt,
                         command=lambda k=key: self.v[k].set(clean_number(self.v[k].get())))
        if kind == "mm":
            self.mm_spins.append((sp, step))
        return sp

    # ---------- UI ----------
    def _build_ui(self):
        left = ttk.Frame(self, padding=10)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self, padding=(0, 10, 10, 10))
        right.pack(side="left", fill="both", expand=True)
        r = 0

        def label(text, row, col=0):
            ttk.Label(left, text=text).grid(row=row, column=col, sticky="w", pady=2)

        def num(row, text, key, step, kind="int", lo=0, hi=100000):
            label(text, row)
            self.spin(left, key, step, kind, lo, hi).grid(row=row, column=1, sticky="w")

        ttk.Button(left, text="Open PDF or image...", command=self.open_pdf).grid(
            row=r, column=0, columnspan=2, sticky="we")
        self.file_lbl = ttk.Label(left, text="No file opened", wraplength=250, foreground="#555")
        self.file_lbl.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        r += 2
        ttk.Radiobutton(left, text="One design, repeated", variable=self.v["layout"],
                        value="single", command=self.on_layout_change).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Radiobutton(left, text="Many designs on one sheet (list)", variable=self.v["layout"],
                        value="mix", command=self.on_layout_change).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        label("Pages (all / 1,3-5)", r)
        ttk.Entry(left, textvariable=self.v["pages"], width=14).grid(
            row=r, column=1, columnspan=2, sticky="w"); r += 1

        ttk.Separator(left).grid(row=r, column=0, columnspan=3, sticky="we", pady=6); r += 1
        label("Units", r)
        cb = ttk.Combobox(left, textvariable=self.v["unit"], values=list(UNITS), width=8, state="readonly")
        cb.grid(row=r, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self.change_unit()); r += 1
        label("Paper", r)
        pc = ttk.Combobox(left, textvariable=self.v["preset"], values=list(PAPER_SIZES),
                          width=10, state="readonly")
        pc.grid(row=r, column=1, sticky="w")
        pc.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        oc = ttk.Combobox(left, textvariable=self.v["orient"], values=["Portrait", "Landscape"],
                          width=10, state="readonly")
        oc.grid(row=r, column=2, sticky="w", padx=4)
        oc.bind("<<ComboboxSelected>>", lambda e: self.apply_orientation()); r += 1
        num(r, "Paper width", "paper_w", 1, "mm", 1)
        self.unit_lbl1 = ttk.Label(left, text="mm")
        self.unit_lbl1.grid(row=r, column=2, sticky="w"); r += 1
        num(r, "Paper height", "paper_h", 1, "mm", 1); r += 1
        num(r, "Page margin", "margin", 1, "mm"); r += 1
        num(r, "Gap sideways", "gap_x", 1, "mm"); r += 1
        num(r, "Gap up/down", "gap_y", 1, "mm"); r += 1
        label("Rotate design", r)
        ttk.Combobox(left, textvariable=self.v["rotation"],
                     values=["0", "90", "180", "270", "auto"], width=8,
                     state="readonly").grid(row=r, column=1, sticky="w")
        ttk.Label(left, text="(auto = best fit)", foreground="#555").grid(row=r, column=2, sticky="w")
        r += 1

        ttk.Separator(left).grid(row=r, column=0, columnspan=3, sticky="we", pady=6); r += 1
        ttk.Radiobutton(left, text="Fill the page automatically", variable=self.v["mode"],
                        value="auto").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Radiobutton(left, text="Custom number of copies", variable=self.v["mode"],
                        value="custom").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        num(r, "   Copies (per page)", "copies", 1, "int", 1); r += 1
        num(r, "   Columns (0 = auto)", "columns", 1); r += 1
        ttk.Checkbutton(left, text="Centre on paper", variable=self.v["center"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(left, text="Border around each design", variable=self.v["border"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        num(r, "   Border thickness", "border_w", 0.1, "mm"); r += 1
        num(r, "Image DPI (images only)", "image_dpi", 10, "int", 1, 2400); r += 1
        num(r, "Design size % (100 = original)", "scale", 5, "int", 1, 5000); r += 1
        label("Order (list mode)", r)
        ttk.Combobox(left, textvariable=self.v["arrange"], values=list(ARRANGE_MODES),
                     width=22, state="readonly").grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(left, text="Invert colours (one design)", variable=self.v["inv_single"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        af = ttk.Frame(left)
        af.grid(row=r, column=0, columnspan=3, sticky="w")
        ttk.Button(af, text="Select area to invert...", command=self.pick_single_area).pack(side="left")
        ttk.Button(af, text="Clear", width=6, command=self.clear_single_area).pack(side="left", padx=4)
        self.area_lbl = ttk.Label(af, text="Areas: 0", foreground="#555")
        self.area_lbl.pack(side="left", padx=4); r += 1
        num(r, "   Invert quality (DPI)", "invert_dpi", 50, "int", 72, 1200); r += 1
        ttk.Checkbutton(left, text="Crop marks (drawn in the margin)", variable=self.v["crop"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        num(r, "   Mark length", "mark_len", 1, "mm"); r += 1
        num(r, "   Mark distance", "mark_off", 0.5, "mm"); r += 1

        ttk.Separator(left).grid(row=r, column=0, columnspan=3, sticky="we", pady=6); r += 1
        label("Printer", r)
        self.printer_cb = ttk.Combobox(left, textvariable=self.v["printer"], width=26, state="readonly")
        self.printer_cb.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        label("Sheets to print", r)
        self.spin(left, "sheet_copies", 1, "int", 1, 1000, width=6).grid(row=r, column=1, sticky="w")
        ttk.Button(left, text="Printer settings", command=self.printer_settings).grid(
            row=r, column=2, sticky="w", padx=4); r += 1
        bf = ttk.Frame(left)
        bf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Button(bf, text="PRINT", command=self.do_print).pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="Save PDF...", command=self.save_pdf).pack(side="left", padx=4)
        ttk.Button(bf, text="Update", command=lambda: self.check_update(False)).pack(side="left")

        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Button(top, text="<", width=3, command=lambda: self.nav(-1)).pack(side="left")
        self.page_lbl = ttk.Label(top, text="Sheet - / -")
        self.page_lbl.pack(side="left", padx=8)
        ttk.Button(top, text=">", width=3, command=lambda: self.nav(1)).pack(side="left")
        self.status = ttk.Label(top, text="Open a PDF or image to start", foreground="#555")
        self.status.pack(side="left", padx=16)
        self._build_mix_panel(right)
        self.canvas = tk.Canvas(right, bg="#d9d9d9", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas.bind("<Configure>", lambda e: self.draw_soon())

    def _build_mix_panel(self, parent):
        self.mix_frame = ttk.LabelFrame(parent, text="Designs on the sheet", padding=6)
        self.tree = ttk.Treeview(self.mix_frame, columns=("name", "size", "copies", "scale", "inv"),
                                 show="headings", height=5, selectmode="extended")
        for col, text, w in (("name", "Design", 300), ("size", "Size", 130),
                             ("copies", "Copies", 70), ("scale", "Size %", 70),
                             ("inv", "Invert", 80)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(self.mix_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, columnspan=8, sticky="we")
        sb.grid(row=0, column=8, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_item_select())
        ttk.Button(self.mix_frame, text="Add files...", command=self.add_files).grid(row=1, column=0, pady=4, sticky="w")
        ttk.Button(self.mix_frame, text="Remove selected", command=self.remove_items).grid(row=1, column=1, padx=4, sticky="w")
        ttk.Button(self.mix_frame, text="Clear all", command=self.clear_items).grid(row=1, column=2, sticky="w")
        ttk.Label(self.mix_frame, text="Copies").grid(row=1, column=3, padx=(14, 2))
        self.spin(self.mix_frame, "item_copies", 1, "int", 0, 100000, width=6).grid(row=1, column=4)
        ttk.Label(self.mix_frame, text="Size %").grid(row=1, column=5, padx=(8, 2))
        self.spin(self.mix_frame, "item_scale", 5, "int", 1, 5000, width=6).grid(row=1, column=6)
        ttk.Button(self.mix_frame, text="Apply to selected", command=lambda: self.apply_items(False)).grid(row=2, column=3, columnspan=3, pady=2, sticky="we")
        ttk.Button(self.mix_frame, text="Apply to all", command=lambda: self.apply_items(True)).grid(row=2, column=6, columnspan=2, pady=2, sticky="we")
        inv = ttk.Frame(self.mix_frame)
        inv.grid(row=3, column=0, columnspan=8, sticky="w", pady=(4, 0))
        ttk.Label(inv, text="Invert colours:").pack(side="left")
        ttk.Button(inv, text="Selected", command=lambda: self.toggle_invert(False)).pack(side="left", padx=4)
        ttk.Button(inv, text="All", command=lambda: self.toggle_invert(True)).pack(side="left")
        ttk.Button(inv, text="Select area for selected design...", command=self.pick_item_area).pack(side="left", padx=4)
        ttk.Button(inv, text="Clear invert", command=self.clear_invert).pack(side="left")

    # ---------- units / paper ----------
    def f(self, name):
        return float(self.v[name].get().replace(",", "."))

    def mm(self, name):
        return self.f(name) * UNITS[self.unit]

    def fmt(self, mm_value):
        s = ("%." + str(DECIMALS[self.unit]) + "f") % (mm_value / UNITS[self.unit])
        return s.rstrip("0").rstrip(".") if "." in s else s

    def change_unit(self):
        new, old = self.v["unit"].get(), self.unit
        if new == old:
            return
        self._muting = True
        for name in ("paper_w", "paper_h", "margin", "gap_x", "gap_y", "mark_len", "mark_off", "border_w"):
            try:
                val_mm = float(self.v[name].get().replace(",", ".")) * UNITS[old]
            except ValueError:
                continue
            self.v[name].set(clean_number(val_mm / UNITS[new]))
        self.unit = new
        self.unit_lbl1.config(text=new)
        for sp, step_mm in self.mm_spins:
            sp.configure(increment=step_mm / UNITS[new])
        self._muting = False
        self.refresh_table()
        self.schedule()

    def apply_preset(self):
        size = PAPER_SIZES.get(self.v["preset"].get())
        if not size:
            return
        w, h = size
        if self.v["orient"].get() == "Landscape":
            w, h = h, w
        self._muting = True
        self.v["paper_w"].set(self.fmt(w))
        self.v["paper_h"].set(self.fmt(h))
        self._muting = False
        self.schedule()

    def apply_orientation(self):
        try:
            w, h = self.f("paper_w"), self.f("paper_h")
        except ValueError:
            return
        lo, hi = min(w, h), max(w, h)
        self._muting = True
        if self.v["orient"].get() == "Landscape":
            self.v["paper_w"].set(clean_number(hi))
            self.v["paper_h"].set(clean_number(lo))
        else:
            self.v["paper_w"].set(clean_number(lo))
            self.v["paper_h"].set(clean_number(hi))
        self._muting = False
        self.schedule()

    def image_dpi(self):
        try:
            return max(float(self.v["image_dpi"].get().replace(",", ".")), 1.0)
        except ValueError:
            return 300.0

    def _invert_dpi(self):
        try:
            return min(max(float(self.v["invert_dpi"].get().replace(",", ".")), 72.0), 1200.0)
        except ValueError:
            return 300.0

    def settings(self):
        s = {
            "pages": self.v["pages"].get(),
            "paper_w": self.mm("paper_w"), "paper_h": self.mm("paper_h"),
            "margin": self.mm("margin"),
            "gap_x": self.mm("gap_x"), "gap_y": self.mm("gap_y"),
            "rotation": "auto" if self.v["rotation"].get() == "auto" else int(self.v["rotation"].get()),
            "mode": self.v["mode"].get(),
            "copies": int(float(self.v["copies"].get() or 1)),
            "center": self.v["center"].get() == "1",
            "crop_marks": self.v["crop"].get() == "1",
            "mark_len": self.mm("mark_len"), "mark_off": self.mm("mark_off"),
            "border": self.v["border"].get() == "1", "border_w": self.mm("border_w"),
            "scale": float(self.v["scale"].get().replace(",", ".") or 100) / 100.0,
            "grid": None,
            "arrange": ARRANGE_MODES.get(self.v["arrange"].get(), "best"),
            "invert_dpi": self._invert_dpi(),
            "invert": {"full": self.v["inv_single"].get() == "1",
                       "regions": [tuple(tuple(p) for p in r) for r in self.single_regions]},
            "image_dpi": self.image_dpi(), "src_is_image": False,
        }
        cols = int(float(self.v["columns"].get() or 0))
        if s["mode"] == "custom" and cols > 0:
            s["grid"] = (cols, 9999)
        if s["scale"] <= 0:
            raise ValueError("Design size % must be above zero")
        if s["paper_w"] <= 0 or s["paper_h"] <= 0:
            raise ValueError("Paper size must be above zero")
        if min(s["margin"], s["gap_x"], s["gap_y"]) < 0:
            raise ValueError("Margin and gaps cannot be negative")
        return s

    # ---------- building the sheets (in the background) ----------
    def schedule(self):
        if self._muting:
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(300, self.rebuild)

    def rebuild(self):
        if self._after:
            self.after_cancel(self._after)
        self._after = None
        if self.busy:
            self.pending = True
            return
        mix = self.v["layout"].get() == "mix"
        try:
            s = self.settings()
        except Exception as e:
            self.status.config(text="Problem: %s" % e, foreground="#b00020")
            return
        if mix:
            if not self.items:
                self.out = None
                self.preview_pil = None
                self.status.config(text="Click 'Add files...' to put designs on the sheet", foreground="#555")
                self.file_lbl.config(text="No designs in the list")
                self.draw_preview()
                return
            items = [dict(it) for it in self.items]
            path = ""
        else:
            if not self.src_path:
                return
            items, path = None, self.src_path
        self.busy = True
        self.status.config(text="Working...", foreground="#555")
        args = (mix, s, items, path, self.image_dpi(), self.preview_index)
        threading.Thread(target=self._job, args=args, daemon=True).start()

    def _job(self, mix, s, items, path, dpi, idx):
        try:
            with FITZ_LOCK:
                meta = None
                if mix:
                    for it in items:
                        it["doc"] = get_doc(it["path"], dpi)
                        it["w"], it["h"] = design_size_mm(it["doc"], it["page"])
                    out, info = compose_mix(items, s)
                else:
                    src = get_doc(path, dpi)
                    s["src_is_image"] = is_image(path)
                    w, h = design_size_mm(src, 0)
                    meta = (len(src), w, h)
                    out, info = compose(src, s)
                idx = max(0, min(idx, len(out) - 1))
                img = render_preview(out, idx)
            self.q.put(lambda: self._done(out, info, meta, idx, img, items if mix else None, None))
        except Exception as e:
            err = str(e)
            self.q.put(lambda: self._done(None, None, None, idx, None, None, err))

    def _done(self, out, info, meta, idx, img, items, err):
        self.busy = False
        if err:
            self.out = None
            self.preview_pil = None
            self.status.config(text="Problem: %s" % err, foreground="#b00020")
        else:
            self.out, self.preview_index, self.preview_pil = out, idx, img
            if items is not None:  # keep sizes / documents found in the background
                by_id = {it["id"]: it for it in items}
                for it in self.items:
                    got = by_id.get(it["id"])
                    if got:
                        it["doc"], it["w"], it["h"] = got["doc"], got["w"], got["h"]
                self.refresh_table()
                self.file_lbl.config(text="%d design(s) in the list" % len(self.items))
                txt = "%d sheet(s)  |  %d designs placed" % (info["sheets"], info["placed"])
                if info["shrunk"]:
                    txt += "  |  some designs were too big and were shrunk to fit"
            else:
                n_pages, w, h = meta
                self.file_lbl.config(text="%s\n%d page(s), page 1 = %s x %s %s" % (
                    os.path.basename(self.src_path), n_pages, self.fmt(w), self.fmt(h), self.unit))
                d = info["details"][0]
                txt = "%d sheet(s) total  |  %d copies fit per sheet (rotation %s)  |  design %s x %s %s" % (
                    info["sheets"], d[1], d[3], self.fmt(d[4]), self.fmt(d[5]), self.unit)
            self.status.config(text=txt, foreground="#555")
        self.draw_preview()
        if self.pending:
            self.pending = False
            self.rebuild()

    # ---------- preview ----------
    def nav(self, step):
        if self.out is None or self.busy:
            return
        self.preview_index = (self.preview_index + step) % len(self.out)
        out, idx = self.out, self.preview_index

        def work():
            with FITZ_LOCK:
                img = render_preview(out, idx)
            self.q.put(lambda: self._preview_done(out, idx, img))
        threading.Thread(target=work, daemon=True).start()

    def _preview_done(self, out, idx, img):
        if out is self.out and idx == self.preview_index:
            self.preview_pil = img
            self.draw_preview()

    def draw_soon(self):
        if self._draw_after:
            self.after_cancel(self._draw_after)
        self._draw_after = self.after(80, self.draw_preview)

    def draw_preview(self):
        self._draw_after = None
        c = self.canvas
        c.delete("all")
        if self.out is None or self.preview_pil is None:
            self.page_lbl.config(text="Sheet - / -")
            return
        from PIL import ImageTk
        cw, ch = max(c.winfo_width(), 60), max(c.winfo_height(), 60)
        img = self.preview_pil
        scale = min((cw - 30) / img.width, (ch - 30) / img.height)
        nw, nh = max(int(img.width * scale), 10), max(int(img.height * scale), 10)
        if self._photo_src is not img or self._photo_size != (nw, nh):
            self._photo = ImageTk.PhotoImage(img.resize((nw, nh), Image.LANCZOS))
            self._photo_src, self._photo_size = img, (nw, nh)
        x, y = (cw - nw) // 2, (ch - nh) // 2
        c.create_rectangle(x + 4, y + 4, x + nw + 4, y + nh + 4, fill="#b5b5b5", outline="")
        c.create_image(x, y, anchor="nw", image=self._photo)
        c.create_rectangle(x, y, x + nw, y + nh, outline="#888")
        self.page_lbl.config(text="Sheet %d / %d" % (self.preview_index + 1, len(self.out)))

    # ---------- opening files ----------
    def open_pdf(self):
        if self.v["layout"].get() == "mix":
            self.add_files()
            return
        path = filedialog.askopenfilename(
            title="Open PDF or image",
            filetypes=[("PDF or image", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp"),
                       ("PDF files", "*.pdf"), ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp")])
        if not path:
            return
        self.src_path = path
        self.single_regions = []
        self.area_lbl.config(text="Areas: 0")
        self.preview_index = 0
        self.file_lbl.config(text="Loading...")
        self.rebuild()

    # ---------- many designs on one sheet ----------
    def on_layout_change(self):
        if self.v["layout"].get() == "mix":
            self.mix_frame.pack(side="bottom", fill="x", before=self.canvas)
            self.refresh_table()
        else:
            self.mix_frame.pack_forget()
        self.rebuild()

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Add PDFs or images",
            filetypes=[("PDF or image", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp"),
                       ("PDF files", "*.pdf"), ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif *.webp")])
        if not paths:
            return
        self.status.config(text="Loading files...", foreground="#555")
        pages_text, dpi = self.v["pages"].get(), self.image_dpi()

        def work():
            new, errors = [], []
            with FITZ_LOCK:
                for path in paths:
                    try:
                        doc = get_doc(path, dpi)
                        base = os.path.basename(path)
                        pages = [0] if is_image(path) else parse_pages(pages_text, len(doc))
                        for pi in pages:
                            name = base if len(doc) == 1 else "%s  (page %d)" % (base, pi + 1)
                            w, h = design_size_mm(doc, pi)
                            new.append({"name": name, "path": path, "page": pi, "doc": doc,
                                        "copies": 1, "scale": 1.0, "w": w, "h": h,
                                        "inv_full": False, "regions": []})
                    except Exception as e:
                        errors.append("%s\n%s" % (path, e))
            self.q.put(lambda: self._added(new, errors))
        threading.Thread(target=work, daemon=True).start()

    def _added(self, new, errors):
        for it in new:
            it["id"] = self._next_id
            self._next_id += 1
            self.items.append(it)
        if errors:
            messagebox.showerror("Cannot open file", "\n\n".join(errors))
        self.refresh_table()
        self.rebuild()

    def refresh_table(self):
        if not hasattr(self, "tree"):
            return
        keep = list(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            size = "%s x %s %s" % (self.fmt(it["w"] * it["scale"]), self.fmt(it["h"] * it["scale"]), self.unit)
            self.tree.insert("", "end", iid=str(i), values=(
                it["name"], size, it["copies"], "%g" % (it["scale"] * 100), self._inv_text(it)))
        keep = [i for i in keep if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

    def on_item_select(self):
        sel = self.tree.selection()
        if len(sel) == 1:
            it = self.items[int(sel[0])]
            self.v["item_copies"].set(str(it["copies"]))
            self.v["item_scale"].set("%g" % (it["scale"] * 100))

    def apply_items(self, everything):
        try:
            copies = max(int(float(self.v["item_copies"].get() or 1)), 0)
            scale = float(self.v["item_scale"].get().replace(",", ".") or 100) / 100.0
            if scale <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Check numbers", "Copies and Size % must be numbers (Size % above 0).")
            return
        ids = range(len(self.items)) if everything else [int(i) for i in self.tree.selection()]
        for i in ids:
            self.items[i]["copies"] = copies
            self.items[i]["scale"] = scale
        self.refresh_table()
        self.rebuild()

    def remove_items(self):
        for i in sorted((int(x) for x in self.tree.selection()), reverse=True):
            del self.items[i]
        self.refresh_table()
        self.rebuild()

    def clear_items(self):
        self.items = []
        self.refresh_table()
        self.rebuild()

    # ---------- colour inversion ----------
    def _inv_text(self, it):
        full, area = it.get("inv_full"), bool(it.get("regions"))
        return "Full+Area" if (full and area) else "Full" if full else "Area" if area else "-"

    def toggle_invert(self, everything):
        ids = list(range(len(self.items))) if everything else [int(i) for i in self.tree.selection()]
        if not ids:
            messagebox.showinfo("Invert", "Select a design in the list first (or use 'All').")
            return
        turn_on = not all(self.items[i].get("inv_full") for i in ids)
        for i in ids:
            self.items[i]["inv_full"] = turn_on
        self.refresh_table()
        self.rebuild()

    def clear_invert(self):
        sel = [int(i) for i in self.tree.selection()]
        for i in (sel or range(len(self.items))):
            self.items[i]["inv_full"] = False
            self.items[i]["regions"] = []
        self.refresh_table()
        self.rebuild()

    def _open_selector(self, path, page_no, regions, base_inverted, on_done):
        """Renders the design in the background, then shows the selection window."""
        dpi = self.image_dpi()
        self.status.config(text="Preparing selection window...", foreground="#555")

        def work():
            try:
                with FITZ_LOCK:
                    doc = get_doc(path, dpi)
                    pno = page_no if page_no is not None else parse_pages(self.v["pages"].get(), len(doc))[0]
                    img = render_preview(doc, pno, 1100)
                self.q.put(lambda: (self.status.config(text="Select the area, then press Done", foreground="#555"),
                                    AreaSelector(self, img, regions, base_inverted, on_done)))
            except Exception as e:
                err = str(e)
                self.q.put(lambda: self.status.config(text="Problem: %s" % err, foreground="#b00020"))
        threading.Thread(target=work, daemon=True).start()

    def pick_single_area(self):
        if not self.src_path:
            messagebox.showinfo("Select area", "Open a PDF or image first.")
            return

        def done(regions):
            self.single_regions = regions
            self.area_lbl.config(text="Areas: %d" % len(regions))
            self.rebuild()
        self._open_selector(self.src_path, None, self.single_regions,
                            self.v["inv_single"].get() == "1", done)

    def clear_single_area(self):
        self.single_regions = []
        self.area_lbl.config(text="Areas: 0")
        self.rebuild()

    def pick_item_area(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            messagebox.showinfo("Select area", "Select exactly ONE design in the list first.")
            return
        it = self.items[int(sel[0])]

        def done(regions, it=it):
            it["regions"] = regions
            self.refresh_table()
            self.rebuild()
        self._open_selector(it["path"], it["page"], it["regions"], bool(it.get("inv_full")), done)

    # ---------- save / print ----------
    def save_pdf(self):
        if self.busy:
            messagebox.showinfo("Please wait", "The sheets are still being prepared. Try again in a moment.")
            return
        if self.out is None:
            messagebox.showinfo("Nothing to save", "Open a PDF or image and fix any problems first.")
            return
        base = (os.path.splitext(os.path.basename(self.src_path))[0] or "mixed") + "_sheet.pdf"
        if self.v["layout"].get() == "mix":
            base = "mixed_sheet.pdf"
        path = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=base,
                                            filetypes=[("PDF", "*.pdf")])
        if path:
            with FITZ_LOCK:
                self.out.save(path)
            messagebox.showinfo("Saved", "Saved:\n" + path)

    def _load_printers(self):
        try:
            names, default = list_printers()
        except Exception:
            names, default = [], ""
        self.printer_cb["values"] = names
        if default:
            self.v["printer"].set(default)

    def printer_settings(self):
        if self.v["printer"].get():
            open_printer_preferences(self.v["printer"].get())

    def do_print(self):
        if self.busy:
            messagebox.showinfo("Please wait", "The sheets are still being prepared. Try again in a moment.")
            return
        if self.out is None:
            messagebox.showinfo("Nothing to print", "Open a PDF or image and fix any problems first.")
            return
        printer = self.v["printer"].get()
        if not printer:
            messagebox.showerror("No printer", "No printer was found on this PC.")
            return
        try:
            n = int(float(self.v["sheet_copies"].get() or 1))
        except ValueError:
            n = 1
        if not messagebox.askyesno(
                "Print",
                "Print %d sheet(s) x %d on\n%s\n\nIn the printer settings, set the paper size to match "
                "and turn OFF 'fit to page' / scaling." % (len(self.out), n, printer)):
            return
        doc = self.out
        title = os.path.basename(self.src_path) or "Sheet Printer"
        self.status.config(text="Printing...", foreground="#555")

        def work():
            try:
                print_document(doc, printer, copies=n, title=title)
                self.q.put(lambda: self.status.config(text="Sent to printer", foreground="#2e7d32"))
            except Exception as e:
                err = str(e)
                self.q.put(lambda: messagebox.showerror("Print failed", err))
        threading.Thread(target=work, daemon=True).start()

    # ---------- update ----------
    def check_update(self, silent):
        def work():
            try:
                tag, url, newer = check_latest()
            except Exception as e:
                err = str(e)
                if not silent:
                    self.q.put(lambda: messagebox.showerror(
                        "Update", "Could not check for updates.\n(The GitHub repository must be public.)\n\n%s" % err))
                return
            if not newer:
                if not silent:
                    self.q.put(lambda: messagebox.showinfo("Update", "You have the latest version (%s)." % __version__))
                return
            self.q.put(lambda: self.offer_update(tag, url))
        threading.Thread(target=work, daemon=True).start()

    def offer_update(self, tag, url):
        if not url:
            if messagebox.askyesno("Update", "Version %s is out, but no SheetPrinter.exe is attached yet.\nOpen the releases page?" % tag):
                webbrowser.open(RELEASES_PAGE)
            return
        if not messagebox.askyesno("Update available", "New version %s is available (you have %s).\nUpdate now? The app will restart." % (tag, __version__)):
            return
        self.status.config(text="Downloading update...", foreground="#555")

        def work():
            try:
                download_and_replace(url)
                self.q.put(lambda: (self.destroy(), sys.exit(0)))
            except Exception as e:
                err = str(e)
                self.q.put(lambda: messagebox.showerror("Update failed", err))
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
