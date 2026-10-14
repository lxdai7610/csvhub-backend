from dataclasses import replace
import io
from pathlib import Path
import sqlite3
from zipfile import ZipFile, ZIP_DEFLATED
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
import pytest

from csvhub.api import create_app
from csvhub.parsing import CellError
from csvhub.tabular import open_table, TableError
from csvhub.worker import Worker, JobStopped
from conftest import upload, dataset

EXAMPLES = Path(__file__).resolve().parents[1] / "csvhub/web"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
COLUMNS = [{"name": "key", "source": "record_key"}, {"name": "label", "source": "label"},
           {"name": "date", "source": "event_date", "kind": "date"},
           {"name": "stock", "source": "quantity", "kind": "integer"},
           {"name": "formula", "source": "formula_qty", "kind": "integer"},
           {"name": "empty", "source": "empty"}]


def prepare(client, filename, content=None, **options):
    data = (EXAMPLES / filename).read_bytes() if content is None else content
    upload_id = upload(client, data, filename)
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 200
    dataset_id = dataset(client, COLUMNS)
    response = client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id, **options})
    assert response.status_code == 202, response.text
    return upload_id, dataset_id, response.json()["id"]


def patched_xlsx(patch):
    output = io.BytesIO()
    with ZipFile(EXAMPLES / "example.xlsx") as source, ZipFile(output, "w", ZIP_DEFLATED) as destination:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ET.fromstring(data)
                patch(root)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            destination.writestr(entry.filename, data)
    return output.getvalue()


@pytest.mark.parametrize("format", ["xls", "xlsx"])
def test_excel_preview_import_types_indexes_and_warm_cache(system, format):
    client, catalog, worker, _ = system
    upload_id, dataset_id, job_id = prepare(client, "example." + format, sheet="库存数据")
    preview = client.get(f"/api/v1/uploads/{upload_id}/preview").json()
    assert preview["format"] == format
    assert preview["sheets"] == ["库存数据", "备选数据"]
    assert preview["sheet"] == "库存数据" and len(preview["rows"]) == 3
    assert preview["rows"][1][0] == "000002"  # numeric zero-padding format
    assert preview["rows"][0][2] == "2026-10-09"
    assert preview["rows"][1][3:5] == ["0", "0"]
    assert preview["warnings"] == []
    worker.run_one()
    job = catalog.get("jobs", job_id)
    assert job["status"] == "completed", job
    assert job["accepted"] == 3 and job["rejected"] == 0
    rows = client.post(f"/api/v1/datasets/{dataset_id}/query", json={}).json()["rows"]
    assert [row["key"] for row in rows] == ["000001", "000002", "000003"]
    assert rows[0]["date"] == "2026-10-09" and rows[0]["stock"] == 7 and rows[0]["formula"] == 14
    assert rows[1]["stock"] == 0 and rows[1]["formula"] == 0 and rows[1]["empty"] is None
    assert rows[2]["label"] == "部件，多行\n文本"
    response = client.post(f"/api/v1/datasets/{dataset_id}/query", json={
        "filters": [{"field": "key", "op": "eq", "value": "000002"}], "explain": True}).json()
    assert len(response["rows"]) == 1 and any("ix_0" in line for line in response["plan"])
    assert client.post(f"/api/v1/datasets/{dataset_id}/query", json={}).json()["cache_hit"] is True


@pytest.mark.parametrize("format", ["xls", "xlsx"])
def test_excel_selected_sheet_and_unknown_sheet(system, format):
    client, catalog, worker, _ = system
    upload_id, dataset_id, job_id = prepare(client, "example." + format, sheet="备选数据")
    preview = client.get(f"/api/v1/uploads/{upload_id}/preview", params={"sheet": "备选数据"}).json()
    assert preview["rows"][0][0] == "009999"
    assert client.get(f"/api/v1/uploads/{upload_id}/preview", params={"sheet": "不存在"}).status_code == 422
    worker.run_one()
    assert catalog.get("jobs", job_id)["status"] == "completed"
    rows = client.post(f"/api/v1/datasets/{dataset_id}/query", json={}).json()["rows"]
    assert len(rows) == 1 and rows[0]["key"] == "009999"
    assert catalog.get("jobs", job_id)["options"]["sheet"] == "备选数据"


@pytest.mark.parametrize("format", ["xls", "xlsx"])
def test_excel_pause_checkpoint_resume_no_duplicate_rows(system, format):
    client, catalog, worker, settings = system
    _, dataset_id, job_id = prepare(client, "example." + format, sheet="库存数据")

    class PausedWorker(Worker):
        def sync_progress(self, db, id):
            super().sync_progress(db, id)
            if db.execute("SELECT processed FROM checkpoint").fetchone()[0] >= 2:
                raise JobStopped("paused")

    PausedWorker(settings, catalog).run_one()
    assert catalog.get("jobs", job_id)["accepted"] == 2
    assert catalog.get("jobs", job_id)["status"] == "paused"
    assert client.post(f"/api/v1/jobs/{job_id}/resume").status_code == 200
    worker.run_one()
    assert catalog.get("jobs", job_id)["status"] == "completed"
    rows = client.post(f"/api/v1/datasets/{dataset_id}/query", json={}).json()["rows"]
    assert len(rows) == 3 and len({row["_row"] for row in rows}) == 3


def test_xlsx_uncached_formula_cannot_become_valid_text_or_zero(system):
    def remove_cache(root):
        cell = root.find('.//s:c[@r="E2"]', NS)
        cell.remove(cell.find("s:v", NS))
    client, catalog, worker, _ = system
    upload_id, dataset_id, job_id = prepare(client, "example.xlsx", patched_xlsx(remove_cache), on_error="skip")
    preview = client.get(f"/api/v1/uploads/{upload_id}/preview").json()
    assert preview["warnings"] == ["Excel formula has no saved result"]
    worker.run_one()
    job = catalog.get("jobs", job_id)
    assert (job["status"], job["accepted"], job["rejected"]) == ("completed", 2, 1)
    errors = client.get(f"/api/v1/jobs/{job_id}/errors").json()["items"]
    assert "no saved result" in errors[0]["reason"]


def test_xlsx_saved_empty_string_formula_is_nullable_not_an_error(system):
    def empty_string(root):
        cell = root.find('.//s:c[@r="E2"]', NS)
        cell.set("t", "str")
        cell.find("s:f", NS).text = '""'
        cell.find("s:v", NS).text = ""
    client, catalog, worker, _ = system
    upload_id, dataset_id, job_id = prepare(client, "example.xlsx", patched_xlsx(empty_string))
    preview = client.get(f"/api/v1/uploads/{upload_id}/preview").json()
    assert preview["warnings"] == [] and preview["rows"][0][4] == ""
    worker.run_one()
    assert catalog.get("jobs", job_id)["status"] == "completed"
    rows = client.post(f"/api/v1/datasets/{dataset_id}/query", json={}).json()["rows"]
    assert rows[0]["formula"] is None


def test_excel_malformed_archive_and_header_are_reported(system):
    client, catalog, worker, _ = system
    upload_id, _, job_id = prepare(client, "example.xlsx", b"not-a-workbook")
    assert client.get(f"/api/v1/uploads/{upload_id}/preview").status_code == 422
    worker.run_one()
    assert catalog.get("jobs", job_id)["status"] == "failed"

    def duplicate_header(root):
        root.find('.//s:c[@r="B1"]/s:v', NS).text = "record_key"
    upload_id = upload(client, patched_xlsx(duplicate_header), "duplicate.xlsx")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    response = client.get(f"/api/v1/uploads/{upload_id}/preview")
    assert response.status_code == 422 and "duplicate" in response.json()["detail"]


def test_csv_cannot_select_excel_worksheet(system):
    client, *_ = system
    upload_id = upload(client, b"key,label\n1,a\n")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert client.get(f"/api/v1/uploads/{upload_id}/preview?sheet=Sheet1").status_code == 422
    dataset_id = dataset(client)
    assert client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id, "sheet": "Sheet1"}).status_code == 422


def test_xlsx_bad_used_range_does_not_truncate_data(system):
    def wrong_dimension(root):
        element = ET.Element("{" + NS["s"] + "}dimension", {"ref": "A1:A1"})
        root.insert(0, element)
    client, catalog, worker, _ = system
    _, _, job_id = prepare(client, "example.xlsx", patched_xlsx(wrong_dimension))
    worker.run_one()
    job = catalog.get("jobs", job_id)
    assert job["status"] == "completed" and job["accepted"] == 3


def test_excel_capacity_limits_are_enforced_before_loading(system):
    _, _, _, settings = system
    with pytest.raises(TableError, match="metadata limit"):
        with open_table(EXAMPLES / "example.xlsx", "example.xlsx", {}, replace(settings, max_xlsx_metadata_bytes=1)):
            pass
    with pytest.raises(TableError, match="expanded content"):
        with open_table(EXAMPLES / "example.xlsx", "example.xlsx", {}, replace(settings, max_excel_uncompressed_bytes=1)):
            pass
    with pytest.raises(TableError, match="Legacy XLS"):
        with open_table(EXAMPLES / "example.xls", "example.xls", {}, replace(settings, max_xls_bytes=1)):
            pass
    limited = replace(settings, max_xls_bytes=1)
    with TestClient(create_app(limited)) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        response = client.post("/api/v1/uploads", json={"filename": "large.xls", "size": 2, "sha256": "0" * 64})
        assert response.status_code == 413
