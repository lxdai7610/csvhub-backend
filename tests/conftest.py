from dataclasses import replace
import hashlib

from fastapi.testclient import TestClient
import pytest

from csvhub.api import create_app
from csvhub.config import Settings
from csvhub.worker import Worker


@pytest.fixture
def system(tmp_path):
    settings = Settings(data_dir=tmp_path, api_token="test-token-with-32-characters-0000", batch_rows=2,
                        min_free_bytes=0, max_chunk_bytes=1024 * 1024)
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {settings.api_token}"
        yield client, app.state.catalog, Worker(settings, app.state.catalog), settings


def upload(client, content, filename="sample.csv", digest=None):
    response = client.post("/api/v1/uploads", json={"filename": filename, "size": len(content),
                                                   "sha256": digest or hashlib.sha256(content).hexdigest()})
    assert response.status_code == 201, response.text
    upload_id = response.json()["id"]
    response = client.put(f"/api/v1/uploads/{upload_id}/chunks?offset=0", content=content,
                          headers={"X-Chunk-SHA256": hashlib.sha256(content).hexdigest()})
    assert response.status_code == 200, response.text
    return upload_id


def dataset(client, columns=None):
    columns = columns or [{"name": "key", "source": "key"}, {"name": "label", "source": "label"}]
    response = client.post("/api/v1/datasets", json={"name": "Synthetic inventory", "columns": columns})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def import_data(system, content, dataset_id=None, **options):
    client, catalog, worker, _ = system
    dataset_id = dataset_id or dataset(client)
    upload_id = upload(client, content)
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 200
    response = client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id, **options})
    assert response.status_code == 202, response.text
    job = response.json()
    worker.run_one()
    return dataset_id, catalog.get("jobs", job["id"])

