"""Windows printing through the normal printer driver (GDI)."""
import composer


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
    """Send every sheet of `doc` to the printer at 1:1 physical size."""
    import win32con
    import win32print
    import win32ui
    from PIL import ImageWin

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    dpi_x = hdc.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
    phys_w = hdc.GetDeviceCaps(win32con.PHYSICALWIDTH)
    phys_h = hdc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
    off_x = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
    off_y = hdc.GetDeviceCaps(win32con.PHYSICALOFFSETY)
    render_dpi = min(max(dpi_x, dpi_y), max_dpi)

    hdc.StartDoc(title)
    try:
        for _ in range(max(int(copies), 1)):
            for i in range(len(doc)):
                img = composer.render_page(doc, i, render_dpi)
                hdc.StartPage()
                dib = ImageWin.Dib(img)
                # Stretch the sheet over the full physical paper so the
                # printed size is exactly the size set in the app.
                dib.draw(hdc.GetHandleOutput(),
                         (-off_x, -off_y, -off_x + phys_w, -off_y + phys_h))
                hdc.EndPage()
        hdc.EndDoc()
    except Exception:
        hdc.AbortDoc()
        raise
    finally:
        hdc.DeleteDC()


def open_printer_preferences(printer_name):
    """Opens the printer's own settings window (paper tray, quality...)."""
    import subprocess
    subprocess.Popen(["rundll32", "printui.dll,PrintUIEntry", "/e", "/n", printer_name])
