import csv, io, json, os, re, shutil, subprocess, tempfile, uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from email.parser import BytesParser
from email.policy import default
from openpyxl import load_workbook
try:
    import xlrd  # supports legacy binary .xls files without requiring LibreOffice
except ImportError:  # launcher installs requirements.txt on first run
    xlrd = None
from urllib.parse import urlsplit, parse_qs
import state_store

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("APEX_PORT", "8765"))
HOST = os.environ.get("APEX_HOST", "127.0.0.1")   # set APEX_HOST=0.0.0.0 to let other devices on your network connect


def find_soffice():
    for name in ("soffice", "libreoffice", "soffice.exe", "libreoffice.exe"):
        p = shutil.which(name)
        if p:
            return p
    # LibreOffice on Windows is often installed but not added to PATH.
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            candidate = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if candidate.exists():
                return str(candidate)
    return None


def parse_xlsx(data):
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    cell_rows = list(ws.iter_rows())
    if not cell_rows:
        return []
    headers = [re.sub(r"[^a-z0-9]+", " ", str(c.value or "").lower()).strip() for c in cell_rows[0]]
    rows = []
    for row_index, cells in enumerate(cell_rows):
        out = []
        for col_index, cell in enumerate(cells):
            value = cell.value
            if value is None:
                out.append("")
                continue
            header = headers[col_index] if col_index < len(headers) else ""
            if row_index > 0 and re.search(r"(contact no|contact number|phone|tel)", header) and isinstance(value, (int, float)) and not isinstance(value, bool):
                digits = str(int(value)) if float(value).is_integer() else str(value)
                fmt = str(getattr(cell, "number_format", "") or "").split(";")[0]
                zero_mask = len(fmt) if fmt and set(fmt) == {"0"} else 0
                out.append(digits.zfill(max(10, zero_mask)))
            else:
                text_value = str(value)
                if row_index > 0 and re.search(r"(contact no|contact number|phone|tel)", header) and re.fullmatch(r"\d{9}", text_value):
                    text_value = "0" + text_value
                out.append(text_value)
        rows.append(out)
    wb.close()
    return rows


def parse_xls(data):
    """Read legacy .xls directly with xlrd; LibreOffice is retained as a fallback."""
    if xlrd is not None:
        book = xlrd.open_workbook(file_contents=data, formatting_info=True)
        sheet = book.sheet_by_index(0)
        if sheet.nrows == 0:
            return []
        headers = [re.sub(r"[^a-z0-9]+", " ", str(sheet.cell_value(0, c) or "").lower()).strip()
                   for c in range(sheet.ncols)]
        rows = []
        for ri in range(sheet.nrows):
            row = []
            for ci in range(sheet.ncols):
                cell = sheet.cell(ri, ci)
                value = cell.value
                if value == "" or value is None:
                    row.append("")
                    continue
                header = headers[ci] if ci < len(headers) else ""
                if ri > 0 and re.search(r"(contact no|contact number|phone|tel)", header) and cell.ctype == xlrd.XL_CELL_NUMBER:
                    digits = str(int(value)) if float(value).is_integer() else str(value)
                    mask_width = 0
                    try:
                        xf = book.xf_list[cell.xf_index]
                        fmt = book.format_map[xf.format_key].format_str
                        first_section = str(fmt).split(";")[0]
                        if first_section and set(first_section) == {"0"}:
                            mask_width = len(first_section)
                    except Exception:
                        pass
                    row.append(digits.zfill(max(10, mask_width)))
                else:
                    text_value = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
                    if ri > 0 and re.search(r"(contact no|contact number|phone|tel)", header) and re.fullmatch(r"\d{9}", text_value):
                        text_value = "0" + text_value
                    row.append(text_value)
            rows.append(row)
        return rows

    # Fallback for environments where xlrd has not yet been installed.
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("Legacy .xls import requires xlrd. Start the OMS with start_server.bat / start_server.sh to install required dependencies, or save the file as .xlsx/.csv.")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "upload.xls"
        src.write_bytes(data)
        out = Path(td) / "out"
        out.mkdir()
        proc = subprocess.run([soffice, "--headless", "--convert-to", "xlsx", "--outdir", str(out), str(src)],
                              capture_output=True, text=True, timeout=60)
        xlsx_path = out / "upload.xlsx"
        if proc.returncode == 0 and xlsx_path.exists():
            return parse_xlsx(xlsx_path.read_bytes())
        detail = proc.stderr.strip() or "Could not convert XLS file"
        raise RuntimeError(detail)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/state":
            since = int((parse_qs(parts.query).get("since", ["-1"])[0] or "-1"))
            return self.end_json(state_store.get(since))
        return super().do_GET()

    def do_PUT(self):
        if urlsplit(self.path).path != "/api/state":
            return self.send_error(404)
        length = int(self.headers.get("Content-Length", "0"))
        if length > state_store.MAX_BYTES:
            return self.end_json({"error": "Payload too large"}, 413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self.end_json({"error": "Bad JSON"}, 400)
        return self.end_json(state_store.put(payload))

    def do_POST(self):
        if urlsplit(self.path).path == "/api/announcement-media":
            # Banner image/video files are saved as shared static assets instead of data URLs.
            # Keeping media out of localStorage/shared JSON allows larger looping clips to work
            # without exhausting browser storage; every signed-in user fetches the same asset.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    return self.end_json({"error": "No media file uploaded"}, 400)
                if length > 25 * 1024 * 1024 + 256 * 1024:
                    return self.end_json({"error": "Media must be smaller than 25 MB"}, 413)
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                msg = BytesParser(policy=default).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                item = next((part for part in msg.iter_attachments() if part.get_param("name", header="content-disposition") == "file"), None)
                if item is None:
                    return self.end_json({"error": "No media file uploaded"}, 400)
                data = item.get_payload(decode=True) or b""
                mime = (item.get_content_type() or "").lower()
                name = item.get_filename() or ""
                allowed = {
                    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                    "image/gif": ".gif", "image/avif": ".avif",
                    "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv"
                }
                if mime not in allowed:
                    # Browser MIME type can occasionally be omitted; infer only from a safe extension.
                    ext = Path(name).suffix.lower()
                    by_ext = {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp", ".gif":"image/gif", ".avif":"image/avif", ".mp4":"video/mp4", ".webm":"video/webm", ".ogv":"video/ogg", ".ogg":"video/ogg"}
                    mime = by_ext.get(ext, "")
                if mime not in allowed:
                    return self.end_json({"error": "Supported media: JPG, PNG, WebP, GIF, MP4, WebM or OGG"}, 415)
                if not data:
                    return self.end_json({"error": "The uploaded media file is empty"}, 400)
                if len(data) > 25 * 1024 * 1024:
                    return self.end_json({"error": "Media must be smaller than 25 MB"}, 413)
                target_dir = ROOT / "uploads" / "announcement"
                target_dir.mkdir(parents=True, exist_ok=True)
                filename = f"banner_{uuid.uuid4().hex}{allowed[mime]}"
                (target_dir / filename).write_bytes(data)
                return self.end_json({"url": f"/uploads/announcement/{filename}", "type": mime, "size": len(data)}, 201)
            except Exception as e:
                return self.end_json({"error": str(e)}, 500)

        if urlsplit(self.path).path != "/api/parse-excel":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            content_type = self.headers.get("Content-Type", "")
            msg = BytesParser(policy=default).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            item = next((p for p in msg.iter_attachments() if p.get_param("name", header="content-disposition") == "file"), None)
            if item is None:
                return self.end_json({"error": "No file uploaded"}, 400)
            data = item.get_payload(decode=True) or b""
            name = item.get_filename() or ""
            lower = name.lower()
            if lower.endswith(".xlsx"):
                rows = parse_xlsx(data)
            elif lower.endswith(".xls"):
                rows = parse_xls(data)
            else:
                return self.end_json({"error": "Only .xls and .xlsx are accepted here"}, 400)
            return self.end_json(rows)
        except Exception as e:
            return self.end_json({"error": str(e)}, 500)


if __name__ == "__main__":
    print(f"APEX HOLDINGS OMS running at http://{HOST if HOST != '0.0.0.0' else '127.0.0.1'}:{PORT}")
    print("Keep this window open while using XLS/XLSX import.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
