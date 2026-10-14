import csv
from datetime import date
import io
import re


class CellError(str):
    """An Excel error is visible in previews and cannot become valid mapped text."""

    def __new__(cls, reason):
        return super().__new__(cls, reason)


class BoundedLines:
    """Bound input of a logical CSV record, including embedded newlines, before parsing."""

    def __init__(self, file, max_bytes, encoding):
        self.file = file
        self.max_bytes = max_bytes
        self.encoding = encoding
        self.record_bytes = 0
        self.line_number = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = self.file.readline(self.max_bytes + 1)
        if not line:
            raise StopIteration
        self.line_number += 1
        self.record_bytes += len(line.encode(self.encoding))
        if self.record_bytes > self.max_bytes:
            raise ValueError("CSV logical record exceeds configured size limit")
        return line

    def reset(self):
        self.record_bytes = 0


class CSVParseError(ValueError):
    """Only fixed error codes and positions cross the import/API boundary."""

    def __init__(self, code, record, first_line, last_line):
        self.code = code
        self.record = record  # Zero denotes the header; data records start at one.
        self.first_line, self.last_line = first_line, last_line
        super().__init__(f"CSV parse error: {code}; record {record}; "
                         f"physical lines {first_line}-{last_line}")
        if code in ("record_too_large", "field_too_large"):
            self.args = (self.args[0] + "; configured size limit exceeded",)


class RejectedRecord:
    """A bounded, complete physical line that cannot be parsed; no source values."""

    def __init__(self, error):
        self.error = error


def csv_error_code(error):
    # Never preserve arbitrary parser exception text or source values.
    message = str(error)
    if message == "unexpected end of data":
        return "unexpected_eof"
    if "expected after" in message:
        return "invalid_after_quote"
    if message.startswith("field larger than field limit"):
        return "field_too_large"
    if message.startswith("new-line character seen in unquoted field"):
        return "unexpected_newline"
    return "invalid_csv"


class CSVRecords:
    def __init__(self, reader, lines):
        self.reader, self.lines = reader, lines
        self.records_read = 0

    def __iter__(self):
        return self

    def __next__(self):
        first_line = self.lines.line_number + 1
        self.lines.reset()
        try:
            row = next(self.reader)
        except csv.Error as error:
            raise CSVParseError(csv_error_code(error), self.records_read, first_line,
                                max(first_line, self.lines.line_number)) from None
        except ValueError as error:
            if str(error) != "CSV logical record exceeds configured size limit":
                raise
            raise CSVParseError("record_too_large", self.records_read, first_line,
                                max(first_line, self.lines.line_number)) from None
        self.records_read += 1
        return row


class SingleLineCSVRecords:
    """Opt-in contract: one physical line per record, never join subsequent lines."""

    def __init__(self, lines, delimiter):
        self.lines, self.delimiter = lines, delimiter
        self.records_read = 0

    def __iter__(self):
        return self

    def __next__(self):
        first_line = self.lines.line_number + 1
        self.lines.reset()
        try:
            line = next(self.lines)
        except ValueError as error:
            if isinstance(error, UnicodeError) or str(error) != "CSV logical record exceeds configured size limit":
                raise
            # Capacity/encoding errors still stop; a capped read is not a complete line.
            raise CSVParseError("record_too_large", self.records_read, first_line,
                                max(first_line, self.lines.line_number)) from None
        try:
            # A fresh parser for every physical line prevents a broken quote from
            # consuming later records. CSV escaping/delimiters remain standard.
            row = next(csv.reader([line], delimiter=self.delimiter, strict=True), [])
        except csv.Error as error:
            row = RejectedRecord(CSVParseError(csv_error_code(error), self.records_read,
                                               first_line, self.lines.line_number))
        self.records_read += 1
        return row


def csv_reader(binary, encoding, delimiter, max_record_bytes, row_mode="standard"):
    text = io.TextIOWrapper(binary, encoding=encoding, errors="strict", newline="")
    lines = BoundedLines(text, max_record_bytes, encoding)
    csv.field_size_limit(max_record_bytes)
    reader = (SingleLineCSVRecords(lines, delimiter) if row_mode == "single_line" else
              CSVRecords(csv.reader(lines, delimiter=delimiter, strict=True), lines))
    return text, lines, reader


def normalize(value, column):
    if isinstance(value, CellError):
        raise ValueError(str(value))
    if value is None or value == "":
        if not column["nullable"]:
            raise ValueError("Required field is empty")
        return None
    value = value.strip() if column["trim"] else value
    if value == "":
        if not column["nullable"]:
            raise ValueError("Required field is empty")
        return None
    if column["kind"] == "integer":
        number = int(value)
        if not -(2**63) <= number < 2**63:
            raise ValueError("Integer is outside signed 64-bit range")
        return number
    if column["kind"] == "date":
        # Eight-digit YYYYMMDD and ISO YYYY-MM-DD accepted; all stored as ISO.
        if len(value) == 8 and value.isdigit():
            value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Date must use YYYY-MM-DD or YYYYMMDD")
        return date.fromisoformat(value).isoformat()
    if "\x00" in value:
        raise ValueError("NUL characters are not supported")
    if column["indexed"] and len(value) > 512:
        raise ValueError("Indexed text fields must have at most 512 Unicode characters")
    return value

