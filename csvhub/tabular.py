"""Format adapters shared by preview and durable imports; worksheet rows are streamed."""
from contextlib import contextmanager, ExitStack
from datetime import date, datetime, time, timedelta
from itertools import zip_longest
import math
from pathlib import Path
import re
from zipfile import ZipFile, BadZipFile

from .parsing import csv_reader, CellError, RejectedRecord


class TableError(ValueError):
    pass


def source_format(filename):
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".xls", ".xlsx"):
        raise TableError("Supported file formats: CSV, XLS and XLSX")
    return suffix[1:]


def scalar(value, number_format=""):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value.total_seconds())
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return CellError("Non-finite Excel number")
        if value == int(value):
            text = str(int(value))
            if value >= 0 and re.fullmatch(r"0{1,64}", number_format):
                text = text.zfill(len(number_format))
            return text
    return str(value)


class TableSource:
    def __init__(self, format, size, binary=None, lines=None):
        self.format, self.size, self.binary, self.lines = format, size, binary, lines
        self.header, self.sheets, self.sheet = [], [], None
        self.rows = iter(())
        self.record_bytes = 0

    @property
    def read_bytes(self):
        return min(self.binary.tell(), self.size) if self.binary is not None else 0

    def __iter__(self):
        return self.rows


def excel_rows(source, rows, max_bytes):
    width = None
    for row in rows:
        # Formatting-only cells and trailing empty columns do not create data.
        while row and row[-1] == "":
            row.pop()
        if width is None:
            if not row or len(row) > 1024:
                raise TableError("Excel first row must contain 1 to 1024 column names")
            if any(not value or isinstance(value, CellError) or len(value) > 200 for value in row):
                raise TableError("Excel column names must be non-empty text of at most 200 characters")
            if len(row) != len(set(row)):
                raise TableError("Excel header contains duplicate names")
            width = len(row)
        elif not row:
            continue  # Blank worksheet rows are not logical data records.
        elif len(row) < width:
            row.extend([""] * (width - len(row)))
        source.record_bytes = sum(len(value.encode("utf-8")) for value in row)
        if source.record_bytes > max_bytes:
            raise TableError("Excel logical record exceeds configured size limit")
        yield row


def check_xlsx(path, settings):
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 100000 or len({entry.filename for entry in entries}) != len(entries):
                raise TableError("Excel archive has too many or duplicate entries")
            if sum(entry.file_size for entry in entries) > settings.max_excel_uncompressed_bytes:
                raise TableError("Excel expanded content exceeds configured size limit")
            # Read-only openpyxl streams rows, but shared strings/styles are workbook metadata.
            metadata = sum(entry.file_size for entry in entries if entry.filename in (
                "xl/sharedStrings.xml", "xl/styles.xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"))
            if metadata > settings.max_xlsx_metadata_bytes:
                raise TableError("Excel shared strings/styles exceed configured metadata limit; use CSV")
    except BadZipFile:
        raise TableError("Excel workbook is invalid, encrypted or unsupported") from None


def selected_sheet(names, requested):
    if not names or len(names) > 256:
        raise TableError("Excel workbook must contain 1 to 256 worksheets")
    sheet = requested if requested is not None else names[0]
    if sheet not in names:
        raise TableError("Selected Excel worksheet does not exist")
    return sheet


def xlsx_rows(cached, formulas):
    cached_rows, formula_rows = cached.iter_rows(), formulas.iter_rows()
    try:
        # A second streaming view identifies formulas without saved values. Neither
        # view calculates formulas, accesses external links, or loads worksheet cells in bulk.
        for values, originals in zip_longest(cached_rows, formula_rows, fillvalue=()):
            row = []
            for cell, original in zip_longest(values, originals):
                value = cell.value if cell is not None else None
                if original is not None and original.data_type == "f" and value is None and (cell is None or cell.data_type not in ("s", "str", "inlineStr")):
                    row.append(CellError("Excel formula has no saved result"))
                elif cell is not None and cell.data_type == "e":
                    row.append(CellError("Excel cell contains an error"))
                else:
                    row.append(scalar(value, cell.number_format if cell is not None else ""))
            yield row
    except Exception:
        raise TableError("Excel worksheet could not be read; check workbook format") from None
    finally:
        cached_rows.close()
        formula_rows.close()


def xls_rows(book, sheet):
    import xlrd
    for ordinal in range(sheet.nrows):
        row = []
        for cell in sheet.row(ordinal):
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    value = xlrd.xldate_as_datetime(cell.value, book.datemode)
                except (ValueError, OverflowError):
                    row.append(CellError("Invalid Excel date"))
                    continue
            elif cell.ctype == xlrd.XL_CELL_ERROR:
                row.append(CellError("Excel cell contains an error"))
                continue
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                value = bool(cell.value)
            else:
                value = cell.value
            format = book.format_map[book.xf_list[cell.xf_index].format_key].format_str
            row.append(scalar(value, format))
        yield row


@contextmanager
def open_table(path, filename, options, settings):
    format = source_format(filename)
    if format != "csv" and options.get("row_mode", "standard") != "standard":
        raise TableError("Single-line record mode is only supported for CSV files")
    with ExitStack() as resources:
        binary = resources.enter_context(path.open("rb"))
        source = TableSource(format, path.stat().st_size, binary)
        if format == "csv":
            if options.get("sheet") is not None:
                raise TableError("Worksheet selection is only supported for Excel files")
            text, lines, reader = csv_reader(binary, options.get("encoding", "utf-8-sig"),
                                            options.get("delimiter", ","), settings.max_record_bytes,
                                            options.get("row_mode", "standard"))
            resources.callback(text.detach)
            source.header = next(reader, [])
            if isinstance(source.header, RejectedRecord):
                raise source.header.error  # A damaged header cannot establish the schema.

            def csv_rows():
                while True:
                    row = next(reader, None)
                    if row is None:
                        return
                    source.record_bytes = lines.record_bytes
                    yield row

            source.rows = csv_rows()
        else:
            if format == "xlsx":
                import openpyxl
                check_xlsx(path, settings)
                formula_binary = resources.enter_context(path.open("rb"))
                try:
                    cached = openpyxl.load_workbook(binary, read_only=True, data_only=True, keep_links=False)
                    resources.callback(cached.close)
                    formulas = openpyxl.load_workbook(formula_binary, read_only=True, data_only=False, keep_links=False)
                    resources.callback(formulas.close)
                except Exception:
                    raise TableError("Excel workbook is invalid, encrypted or unsupported") from None
                source.sheets = [worksheet.title for worksheet in cached.worksheets]
                source.sheet = selected_sheet(source.sheets, options.get("sheet"))
                values, originals = cached[source.sheet], formulas[source.sheet]
                values.reset_dimensions()
                originals.reset_dimensions()
                rows = xlsx_rows(values, originals)
            else:
                import xlrd
                if source.size > settings.max_xls_bytes:
                    raise TableError("Legacy XLS exceeds configured size limit; use XLSX or CSV")
                try:
                    book = xlrd.open_workbook(str(path), on_demand=True, formatting_info=True, ragged_rows=True)
                    resources.callback(book.release_resources)
                    source.sheets = book.sheet_names()
                    source.sheet = selected_sheet(source.sheets, options.get("sheet"))
                    sheet = book.sheet_by_name(source.sheet)
                except TableError:
                    raise
                except Exception:
                    raise TableError("Excel workbook is invalid, encrypted or unsupported") from None
                rows = xls_rows(book, sheet)
            resources.callback(rows.close)
            source.rows = excel_rows(source, rows, settings.max_record_bytes)
            resources.callback(source.rows.close)
            source.header = next(source.rows, [])
        if format == "csv":
            resources.callback(source.rows.close)
        yield source
