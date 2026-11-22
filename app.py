"""Sheet Printer - tile a PDF design on any paper size and print it."""
import os
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import composer
import geometry as g
import updater
from version import __version__

UNITS = {"mm": 1.0, "cm": 10.0, "inch": 25.4}
DECIMALS = {"mm": 2, "cm": 3, "inch": 3}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Sheet Printer  v%s" % __version__)
        self.geometry("1180x760")
        self.minsize(980, 640)
        try:
            self.state("zoomed")  # open maximised so every button is visible
        except tk.TclError:
            pass
        self.src = None
        self.src_path = ""
        self.out = None
        self.info = None
        self.preview_index = 0
        self._img = None
        self._after = None
        self._muting = False
        self.unit = "mm"
        self.items = []  # designs for "many designs on one sheet" mode

        self.v = {}
        for name, default in [
            ("pages", "all"), ("preset", "A4"), ("orient", "Portrait"),
            ("paper_w", "210"), ("paper_h", "297"), ("margin", "10"),
            ("gap_x", "5"), ("gap_y", "5"), ("rotation", "0"),
            ("mode", "auto"), ("copies", "10"), ("columns", "0"),
            ("center", "1"), ("crop", "1"), ("border", "0"), ("border_w", "0.3"), ("image_dpi", "300"), ("scale", "100"), ("mark_len", "4"), ("mark_off", "1"),
            ("unit", "mm"), ("printer", ""), ("sheet_copies", "1"),
            ("layout", "single"), ("item_copies", "1"), ("item_scale", "100"),
        ]:
            self.v[name] = tk.StringVar(value=default)
        for name, var in self.v.items():
            if name not in ("unit", "printer", "sheet_copies", "item_copies", "item_scale"):
                var.trace_add("write", lambda *a: self.schedule())

        self._build_ui()
        self._load_printers()
        self.after(1500, lambda: self.check_update(silent=True))

    # ---------- UI ----------
    def _build_ui(self):
        left = ttk.Frame(self, padding=10)
        left.pack(side="left", fill="y")
        right = ttk.Frame(self, padding=(0, 10, 10, 10))
        right.pack(side="left", fill="both", expand=True)

        r = 0

        def label(text, row, col=0, **kw):
            ttk.Label(left, text=text).grid(row=row, column=col, sticky="w", pady=2, **kw)

        ttk.Button(left, text="Open PDF or image...", command=self.open_pdf).grid(
            row=r, column=0, columnspan=2, sticky="we")
        self.file_lbl = ttk.Label(left, text="No file opened", wraplength=250, foreground="#555")
        self.file_lbl.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(2, 8))
        r += 2
        ttk.Radiobutton(left, text="One design, repeated", variable=self.v["layout"],
                        value="single", command=self.on_layout_change).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Radiobutton(left, text="Many designs on one sheet (list)", variable=self.v["layout"],
                        value="mix", command=self.on_layout_change).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1

        label("Pages (all / 1,3-5)", r)
        ttk.Entry(left, textvariable=self.v["pages"], width=14).grid(row=r, column=1, columnspan=2, sticky="w")
        r += 1

        ttk.Separator(left).grid(row=r, column=0, columnspan=3, sticky="we", pady=6); r += 1
        label("Units", r)
        cb = ttk.Combobox(left, textvariable=self.v["unit"], values=list(UNITS), width=8, state="readonly")
        cb.grid(row=r, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self.change_unit())
        r += 1

        label("Paper", r)
        pc = ttk.Combobox(left, textvariable=self.v["preset"], values=list(g.PAPER_SIZES),
                          width=10, state="readonly")
        pc.grid(row=r, column=1, sticky="w")
        pc.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        oc = ttk.Combobox(left, textvariable=self.v["orient"], values=["Portrait", "Landscape"],
                          width=10, state="readonly")
        oc.grid(row=r, column=2, sticky="w", padx=4)
        oc.bind("<<ComboboxSelected>>", lambda e: self.apply_orientation())
        r += 1
        label("Paper width", r)
        ttk.Entry(left, textvariable=self.v["paper_w"], width=10).grid(row=r, column=1, sticky="w")
        self.unit_lbl1 = ttk.Label(left, text="mm"); self.unit_lbl1.grid(row=r, column=2, sticky="w")
        r += 1
        label("Paper height", r)
        ttk.Entry(left, textvariable=self.v["paper_h"], width=10).grid(row=r, column=1, sticky="w")
        r += 1
        label("Page margin", r)
        ttk.Entry(left, textvariable=self.v["margin"], width=10).grid(row=r, column=1, sticky="w")
        r += 1
        label("Gap sideways", r)
        ttk.Entry(left, textvariable=self.v["gap_x"], width=10).grid(row=r, column=1, sticky="w")
        r += 1
        label("Gap up/down", r)
        ttk.Entry(left, textvariable=self.v["gap_y"], width=10).grid(row=r, column=1, sticky="w")
        r += 1
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
        label("   Copies (per page)", r)
        ttk.Entry(left, textvariable=self.v["copies"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        label("   Columns (0 = auto)", r)
        ttk.Entry(left, textvariable=self.v["columns"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        ttk.Checkbutton(left, text="Centre on paper", variable=self.v["center"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        ttk.Checkbutton(left, text="Border around each design", variable=self.v["border"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        label("   Border thickness", r)
        ttk.Entry(left, textvariable=self.v["border_w"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        label("Image DPI (images only)", r)
        ttk.Entry(left, textvariable=self.v["image_dpi"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        label("Design size % (100 = original)", r)
        ttk.Entry(left, textvariable=self.v["scale"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        ttk.Checkbutton(left, text="Crop marks (drawn in the margin)", variable=self.v["crop"],
                        onvalue="1", offvalue="0").grid(row=r, column=0, columnspan=3, sticky="w"); r += 1
        label("   Mark length", r)
        ttk.Entry(left, textvariable=self.v["mark_len"], width=10).grid(row=r, column=1, sticky="w"); r += 1
        label("   Mark distance", r)
        ttk.Entry(left, textvariable=self.v["mark_off"], width=10).grid(row=r, column=1, sticky="w"); r += 1

        ttk.Separator(left).grid(row=r, column=0, columnspan=3, sticky="we", pady=6); r += 1
        label("Printer", r)
        self.printer_cb = ttk.Combobox(left, textvariable=self.v["printer"], width=26, state="readonly")
        self.printer_cb.grid(row=r, column=1, columnspan=2, sticky="w"); r += 1
        label("Sheets to print", r)
        ttk.Entry(left, textvariable=self.v["sheet_copies"], width=6).grid(row=r, column=1, sticky="w")
        ttk.Button(left, text="Printer settings", command=self.printer_settings).grid(
            row=r, column=2, sticky="w", padx=4)
        r += 1

        bf = ttk.Frame(left)
        bf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Button(bf, text="PRINT", command=self.do_print).pack(side="left", fill="x", expand=True)
        ttk.Button(bf, text="Save PDF...", command=self.save_pdf).pack(side="left", padx=4)
        ttk.Button(bf, text="Update", command=lambda: self.check_update(False)).pack(side="left")
        r += 1

        # preview side
        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Button(top, text="<", width=3, command=lambda: self.nav(-1)).pack(side="left")
        self.page_lbl = ttk.Label(top, text="Sheet - / -")
        self.page_lbl.pack(side="left", padx=8)
        ttk.Button(top, text=">", width=3, command=lambda: self.nav(1)).pack(side="left")
        self.status = ttk.Label(top, text="Open a PDF to start", foreground="#555")
        self.status.pack(side="left", padx=16)
        self._build_mix_panel(right)
        self.canvas = tk.Canvas(right, bg="#d9d9d9", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas.bind("<Configure>", lambda e: self.draw_preview())

    def _build_mix_panel(self, parent):
        self.mix_frame = ttk.LabelFrame(parent, text="Designs on the sheet", padding=6)
        cols = ("name", "size", "copies", "scale")
        self.tree = ttk.Treeview(self.mix_frame, columns=cols, show="headings",
                                 height=5, selectmode="extended")
        for col, text, w in (("name", "Design", 300), ("size", "Size", 130),
                             ("copies", "Copies", 70), ("scale", "Size %", 70)):
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
        ttk.Entry(self.mix_frame, textvariable=self.v["item_copies"], width=6).grid(row=1, column=4)
        ttk.Label(self.mix_frame, text="Size %").grid(row=1, column=5, padx=(8, 2))
        ttk.Entry(self.mix_frame, textvariable=self.v["item_scale"], width=6).grid(row=1, column=6)
        ttk.Button(self.mix_frame, text="Apply to selected", command=lambda: self.apply_items(False)).grid(row=2, column=3, columnspan=3, pady=2, sticky="we")
        ttk.Button(self.mix_frame, text="Apply to all", command=lambda: self.apply_items(True)).grid(row=2, column=6, columnspan=2, pady=2, sticky="we")

    # ---------- helpers ----------
    def f(self, name):
        return float(self.v[name].get().replace(",", "."))

    def mm(self, name):
        return self.f(name) * UNITS[self.unit]

    def fmt(self, mm_value):
        d = DECIMALS[self.unit]
        s = ("%." + str(d) + "f") % (mm_value / UNITS[self.unit])
        return s.rstrip("0").rstrip(".") if "." in s else s

    def change_unit(self):
        new = self.v["unit"].get()
        old = self.unit
        if new == old:
            return
        self._muting = True
        for name in ("paper_w", "paper_h", "margin", "gap_x", "gap_y", "mark_len", "mark_off", "border_w"):
            try:
                val_mm = float(self.v[name].get().replace(",", ".")) * UNITS[old]
            except ValueError:
                continue
            d = DECIMALS[new]
            s = ("%." + str(d) + "f") % (val_mm / UNITS[new])
            self.v[name].set(s.rstrip("0").rstrip(".") if "." in s else s)
        self.unit = new
        self.unit_lbl1.config(text=new)
        self._muting = False
        self.refresh_table()
        self.schedule()

    def apply_preset(self):
        size = g.PAPER_SIZES.get(self.v["preset"].get())
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
            self.v["paper_w"].set(self.fmt(hi * UNITS[self.unit]))
            self.v["paper_h"].set(self.fmt(lo * UNITS[self.unit]))
        else:
            self.v["paper_w"].set(self.fmt(lo * UNITS[self.unit]))
            self.v["paper_h"].set(self.fmt(hi * UNITS[self.unit]))
        self._muting = False
        self.schedule()

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

    # ---------- actions ----------
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
        try:
            doc = composer.open_source(path, self.image_dpi())
            if len(doc) == 0:
                raise ValueError("This PDF has no pages")
        except Exception as e:
            messagebox.showerror("Cannot open PDF", str(e))
            return
        self.src, self.src_path = doc, path
        w, h = composer.design_size_mm(doc, 0)
        self.file_lbl.config(text="%s\n%d page(s), page 1 = %s x %s %s" % (
            os.path.basename(path), len(doc), self.fmt(w), self.fmt(h), self.unit))
        self.preview_index = 0
        self.rebuild()

    def image_dpi(self):
        try:
            return max(float(self.v["image_dpi"].get().replace(",", ".")), 1.0)
        except ValueError:
            return 300.0

    def schedule(self):
        if self._muting:
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(350, self.rebuild)

    def rebuild(self):
        self._after = None
        if self.v["layout"].get() == "mix":
            self.rebuild_mix()
            return
        if self.src is None:
            return
        try:
            if composer.is_image(self.src_path):  # new DPI = new real size
                self.src = composer.open_source(self.src_path, self.image_dpi())
            s = self.settings()
            out, info = composer.compose(self.src, s)
        except Exception as e:
            self.out = None
            self.status.config(text="Problem: %s" % e, foreground="#b00020")
            self.draw_preview()
            return
        if self.out is not None:
            self.out.close()
        self.out, self.info = out, info
        self.preview_index = min(self.preview_index, len(out) - 1)
        txt = "%d sheet(s) total" % info["sheets"]
        d = info["details"][0]
        txt += "  |  %d copies fit per sheet (rotation %s)" % (d[1], d[3])
        txt += "  |  design %s x %s %s" % (self.fmt(d[4]), self.fmt(d[5]), self.unit)
        if s["crop_marks"] and s["margin"] < s["mark_off"] + s["mark_len"] and not s["center"]:
            txt += "  |  Tip: margin is smaller than the crop marks"
        self.status.config(text=txt, foreground="#555")
        self.draw_preview()

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
        for path in paths:
            try:
                doc = composer.open_source(path, self.image_dpi())
                base = os.path.basename(path)
                if composer.is_image(path):
                    self.items.append({"name": base, "path": path, "page": 0, "doc": doc,
                                       "is_image": True, "copies": 1, "scale": 1.0})
                else:
                    for pi in g.parse_pages(self.v["pages"].get(), len(doc)):
                        name = base if len(doc) == 1 else "%s  (page %d)" % (base, pi + 1)
                        self.items.append({"name": name, "path": path, "page": pi, "doc": doc,
                                           "is_image": False, "copies": 1, "scale": 1.0})
            except Exception as e:
                messagebox.showerror("Cannot open file", "%s\n\n%s" % (path, e))
        self.refresh_table()
        self.rebuild()

    def refresh_table(self):
        if not hasattr(self, "tree"):
            return
        keep = [i for i in self.tree.selection()]
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            try:
                w, h = composer.design_size_mm(it["doc"], it["page"])
                size = "%s x %s %s" % (self.fmt(w * it["scale"]), self.fmt(h * it["scale"]), self.unit)
            except Exception:
                size = "?"
            self.tree.insert("", "end", iid=str(i), values=(
                it["name"], size, it["copies"], ("%g" % (it["scale"] * 100))))
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

    def rebuild_mix(self):
        self._after = None
        if not self.items:
            self.out = None
            self.status.config(text="Click 'Add files...' to put designs on the sheet",
                               foreground="#555")
            self.file_lbl.config(text="No designs in the list")
            self.draw_preview()
            return
        try:
            for it in self.items:
                if it["is_image"]:  # new DPI = new real size
                    it["doc"] = composer.open_source(it["path"], self.image_dpi())
            s = self.settings()
            self.refresh_table()
            out, info = composer.compose_mix(self.items, s)
        except Exception as e:
            self.out = None
            self.status.config(text="Problem: %s" % e, foreground="#b00020")
            self.draw_preview()
            return
        if self.out is not None:
            self.out.close()
        self.out, self.info = out, info
        self.preview_index = min(self.preview_index, len(out) - 1)
        self.file_lbl.config(text="%d design(s) in the list" % len(self.items))
        txt = "%d sheet(s)  |  %d designs placed" % (info["sheets"], info["placed"])
        if info["shrunk"]:
            txt += "  |  some designs were too big and were shrunk to fit"
        self.status.config(text=txt, foreground="#555")
        self.draw_preview()

    def nav(self, step):
        if self.out is None:
            return
        self.preview_index = (self.preview_index + step) % len(self.out)
        self.draw_preview()

    def draw_preview(self):
        c = self.canvas
        c.delete("all")
        if self.out is None:
            self.page_lbl.config(text="Sheet - / -")
            return
        from PIL import ImageTk
        cw, ch = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        page = self.out[self.preview_index]
        pw, ph = page.rect.width, page.rect.height
        scale = min((cw - 30) / pw, (ch - 30) / ph)
        dpi = max(int(72 * scale), 20)
        img = composer.render_page(self.out, self.preview_index, dpi)
        self._img = ImageTk.PhotoImage(img)
        x, y = (cw - img.width) // 2, (ch - img.height) // 2
        c.create_rectangle(x + 4, y + 4, x + img.width + 4, y + img.height + 4, fill="#b5b5b5", outline="")
        c.create_image(x, y, anchor="nw", image=self._img)
        c.create_rectangle(x, y, x + img.width, y + img.height, outline="#888")
        self.page_lbl.config(text="Sheet %d / %d" % (self.preview_index + 1, len(self.out)))

    def save_pdf(self):
        if self.out is None:
            messagebox.showinfo("Nothing to save", "Open a PDF and fix any problems first.")
            return
        base = (os.path.splitext(os.path.basename(self.src_path))[0] or "mixed") + "_sheet.pdf"
        if self.v["layout"].get() == "mix":
            base = "mixed_sheet.pdf"
        path = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=base,
                                            filetypes=[("PDF", "*.pdf")])
        if path:
            self.out.save(path)
            messagebox.showinfo("Saved", "Saved:\n" + path)

    def _load_printers(self):
        try:
            import printing
            names, default = printing.list_printers()
        except Exception:
            names, default = [], ""
        self.printer_cb["values"] = names
        if default:
            self.v["printer"].set(default)

    def printer_settings(self):
        if not self.v["printer"].get():
            return
        import printing
        printing.open_printer_preferences(self.v["printer"].get())

    def do_print(self):
        if self.out is None:
            messagebox.showinfo("Nothing to print", "Open a PDF and fix any problems first.")
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
        self.status.config(text="Printing...", foreground="#555")

        def work():
            try:
                import printing
                printing.print_document(doc, printer, copies=n,
                                        title=os.path.basename(self.src_path) or "Sheet Printer")
                self.after(0, lambda: self.status.config(text="Sent to printer", foreground="#2e7d32"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Print failed", str(e)))
        threading.Thread(target=work, daemon=True).start()

    # ---------- update ----------
    def check_update(self, silent):
        def work():
            try:
                tag, url, newer = updater.check_latest()
            except Exception as e:
                if not silent:
                    self.after(0, lambda: messagebox.showerror(
                        "Update", "Could not check for updates.\n(The GitHub repository must be public.)\n\n%s" % e))
                return
            if not newer:
                if not silent:
                    self.after(0, lambda: messagebox.showinfo("Update", "You have the latest version (%s)." % __version__))
                return
            self.after(0, lambda: self.offer_update(tag, url))
        threading.Thread(target=work, daemon=True).start()

    def offer_update(self, tag, url):
        if not url:
            if messagebox.askyesno("Update", "Version %s is out, but no SheetPrinter.exe is attached yet.\nOpen the releases page?" % tag):
                webbrowser.open(updater.RELEASES_PAGE)
            return
        if not messagebox.askyesno("Update available", "New version %s is available (you have %s).\nUpdate now? The app will restart." % (tag, __version__)):
            return
        try:
            updater.download_and_replace(url)
        except Exception as e:
            messagebox.showerror("Update failed", str(e))
            return
        self.destroy()
        sys.exit(0)


if __name__ == "__main__":
    App().mainloop()
