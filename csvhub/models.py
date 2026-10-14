from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Column(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=200)
    kind: Literal["text", "integer", "date"] = "text"
    indexed: bool = True
    trim: bool = False
    nullable: bool = True


class DatasetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    columns: list[Column] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_columns(self):
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)) or "_row" in names:
            raise ValueError("Column names must be unique; _row is reserved")
        if sum(c.indexed for c in self.columns) > 16:
            raise ValueError("At most 16 indexed columns per dataset")
        return self


class UploadCreate(StrictModel):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ImportCreate(StrictModel):
    upload_id: str
    sheet: str | None = Field(default=None, min_length=1, max_length=200)
    encoding: Literal["utf-8-sig", "utf-8", "gb18030"] = "utf-8-sig"
    delimiter: str = ","
    on_error: Literal["fail", "skip"] = "fail"
    row_mode: Literal["standard", "single_line"] = "standard"

    @field_validator("delimiter")
    @classmethod
    def valid_delimiter(cls, value):
        if len(value) != 1 or value in ('"', "\r", "\n", "\0"):
            raise ValueError("Delimiter must be one character, excluding quotes/newlines/NUL")
        return value


class Filter(StrictModel):
    field: str = Field(min_length=1, max_length=80)
    op: Literal["eq", "prefix", "gte", "lte"] = "eq"
    value: str | int = Field(union_mode="left_to_right")

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value):
        if isinstance(value, str) and (len(value) > 512 or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
            raise ValueError("Text filter must have at most 512 Unicode characters")
        if isinstance(value, int) and not -(2**63) <= value < 2**63:
            raise ValueError("Integer filter must fit signed 64-bit range")
        return value


class Query(StrictModel):
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=16384)
    explain: bool = False

