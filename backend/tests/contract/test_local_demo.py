from fastapi.testclient import TestClient

from commitmentos.local_demo import create_app


def test_local_demo_reads_committed_data_without_cloud_credentials(monkeypatch):
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    client = TestClient(create_app())
    assert client.get('/demo/api/today').status_code == 200
    assert isinstance(client.get('/demo/api/commitments').json(), list)
    assert client.post('/demo/api/commitments', json={}).status_code == 403
    assert client.post('/api/v1/commitments', json={}).status_code == 404
