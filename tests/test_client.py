import pytest

from lucid_vault_exporter.lucid_client import LucidClient, LucidError
from lucid_vault_exporter.ratelimit import RateLimiter

API = "https://api.lucid.co"


def make_client(httpx_mock) -> LucidClient:
    rl = RateLimiter(budgets={"export": 1000, "search": 1000}, sleep=lambda s: None)
    return LucidClient(API, token_provider=lambda: "tok", ratelimiter=rl, sleep=lambda s: None)


def test_search_documents_follows_link_header(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/documents/search?pageSize=200",
        json=[{"documentId": "d1", "title": "One", "product": "lucidchart"}],
        headers={"Link": f'<{API}/documents/search?pageSize=200&pageToken=t2>; rel="next"'},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{API}/documents/search?pageSize=200&pageToken=t2",
        json=[{"documentId": "d2", "title": "Two", "product": "lucidspark"}],
    )
    client = make_client(httpx_mock)
    docs = list(client.search_documents(products=["lucidchart", "lucidspark"]))
    assert [d["documentId"] for d in docs] == ["d1", "d2"]


def test_429_honors_retry_after_then_succeeds(httpx_mock):
    httpx_mock.add_response(
        method="GET", url=f"{API}/documents/d1?page=1",
        status_code=429, headers={"Retry-After": "1"},
    )
    httpx_mock.add_response(
        method="GET", url=f"{API}/documents/d1?page=1",
        content=b"\x89PNG fake", headers={"Content-Type": "image/png"},
    )
    client = make_client(httpx_mock)
    data = client.export_page_png("d1", page=1)
    assert data.startswith(b"\x89PNG")


def test_404_page_raises_pagenotfound(httpx_mock):
    httpx_mock.add_response(method="GET", url=f"{API}/documents/d1?page=9", status_code=404)
    client = make_client(httpx_mock)
    from lucid_vault_exporter.lucid_client import PageNotFound
    with pytest.raises(PageNotFound):
        client.export_page_png("d1", page=9)


def test_get_document_metadata(httpx_mock):
    httpx_mock.add_response(
        method="GET", url=f"{API}/documents/d1",
        json={"documentId": "d1", "title": "One", "pageCount": 3},
    )
    client = make_client(httpx_mock)
    assert client.get_document("d1")["pageCount"] == 3


def test_folder_contents(httpx_mock):
    httpx_mock.add_response(
        method="GET", url=f"{API}/folders/f1/contents?pageSize=200",
        json=[{"id": "d1", "type": "document"}],
    )
    client = make_client(httpx_mock)
    assert list(client.folder_contents("f1"))[0]["id"] == "d1"


def test_auth_failure_raises(httpx_mock):
    httpx_mock.add_response(method="GET", url=f"{API}/documents/d1", status_code=401)
    client = make_client(httpx_mock)
    with pytest.raises(LucidError):
        client.get_document("d1")


def test_folder_contents_follows_link_header(httpx_mock):
    httpx_mock.add_response(
        method="GET", url=f"{API}/folders/f1/contents?pageSize=200",
        json=[{"id": "d1", "type": "document"}],
        headers={"Link": f'<{API}/folders/f1/contents?pageSize=200&pageToken=t2>; rel="next"'},
    )
    httpx_mock.add_response(
        method="GET", url=f"{API}/folders/f1/contents?pageSize=200&pageToken=t2",
        json=[{"id": "d2", "type": "document"}],
    )
    client = make_client(httpx_mock)
    ids = [item["id"] for item in client.folder_contents("f1")]
    assert ids == ["d1", "d2"]
