"""OSV.dev and the package registries over HTTP (faked): batching, paging, names in URLs and failures."""

import httpx
import pytest

from openport.upgrade import osv, registry
from openport.upgrade.lockfiles import Dependency


class FakeHttp:
    """Stands in for httpx.post/get, answering by URL and keeping each request."""

    def __init__(self, answer):
        self.answer = answer
        self.requests: list[tuple[str, str, dict | None]] = []

    def post(self, url, json=None, timeout=None):
        self.requests.append(("POST", url, json))
        return self._response("POST", url, json)

    def get(self, url, timeout=None):
        self.requests.append(("GET", url, None))
        return self._response("GET", url, None)

    def _response(self, method, url, body):
        status, data = self.answer(method, url, body)
        return httpx.Response(status, json=data, request=httpx.Request(method, url))


def deps(*names: str, ecosystem: str = "PyPI") -> list[Dependency]:
    return [Dependency(name, "1.0.0", ecosystem, "requirements.txt") for name in names]


def test_a_batch_keeps_each_dependencys_vulnerabilities_in_order(monkeypatch):
    http = FakeHttp(lambda m, url, body: (200, {"results": [{"vulns": [{"id": "A"}, {"id": "B"}]}, {}, {"vulns": [{"id": "C"}]}]}))
    monkeypatch.setattr(httpx, "post", http.post)
    found = osv._http_batch([osv.query_body(d) for d in deps("one", "two", "three")])
    assert found == [["A", "B"], [], ["C"]]
    assert http.requests[0][1] == osv.OSV_BATCH_URL


def test_a_version_with_more_than_a_page_is_asked_for_in_full(monkeypatch):
    def answer(method, url, body):
        if url == osv.OSV_BATCH_URL:
            return 200, {"results": [{"vulns": [{"id": "A"}], "next_page_token": "more"}]}
        if body.get("page_token") is None:
            return 200, {"vulns": [{"id": "A"}, {"id": "B"}], "next_page_token": "p2"}
        return 200, {"vulns": [{"id": "C"}]}

    http = FakeHttp(answer)
    monkeypatch.setattr(httpx, "post", http.post)
    assert osv._http_batch([osv.query_body(deps("big")[0])]) == [["A", "B", "C"]]
    assert [body.get("page_token") for _, url, body in http.requests if url == osv.OSV_QUERY_URL] == [None, "p2"]


def test_large_dependency_lists_are_split_into_osvs_batch_size(monkeypatch):
    sizes = []

    def answer(method, url, body):
        sizes.append(len(body["queries"]))
        return 200, {"results": [{} for _ in body["queries"]]}

    monkeypatch.setattr(httpx, "post", FakeHttp(answer).post)
    monkeypatch.setattr(osv, "BATCH", 2)
    assert osv._http_batch([osv.query_body(d) for d in deps("a", "b", "c", "d", "e")]) == [[]] * 5
    assert sizes == [2, 2, 1]


def test_an_osv_error_is_raised_not_read_as_no_vulnerabilities(monkeypatch):
    monkeypatch.setattr(httpx, "post", FakeHttp(lambda m, url, body: (503, {"error": "unavailable"})).post)
    with pytest.raises(httpx.HTTPStatusError):
        osv._http_batch([osv.query_body(deps("one")[0])])


def test_a_record_is_fetched_by_its_id(monkeypatch):
    http = FakeHttp(lambda m, url, body: (200, {"id": "GHSA-1", "affected": []}))
    monkeypatch.setattr(httpx, "get", http.get)
    assert osv._http_record("GHSA-1")["id"] == "GHSA-1"
    assert http.requests[0][1] == "https://api.osv.dev/v1/vulns/GHSA-1"


def test_pypi_is_asked_under_the_normalized_name_and_npm_under_the_scoped_one(monkeypatch):
    http = FakeHttp(lambda m, url, body: (200, {"releases": {}, "time": {}}))
    monkeypatch.setattr(httpx, "get", http.get)
    registry.fetch_releases(Dependency("Typing_Extensions", "4.0.0", "PyPI", "r"))
    registry.fetch_releases(Dependency("@scope/pkg", "1.0.0", "npm", "r"))
    assert [url for _, url, _ in http.requests] == ["https://pypi.org/pypi/typing-extensions/json", "https://registry.npmjs.org/@scope/pkg"]


def test_npm_release_dates_skip_the_created_and_modified_stamps():
    dates = registry.parse_npm_releases({"time": {"created": "2015-01-01T00:00:00Z", "modified": "2026-01-01T00:00:00Z",
                                                  "9.0.0": "2022-12-21T00:00:00.000Z", "9.0.1": "not a date"}})
    assert list(dates) == ["9.0.0"]


def test_pypi_release_dates_use_each_versions_earliest_file():
    dates = registry.parse_pypi_releases({"releases": {
        "2.4.0": [{"upload_time_iso_8601": "2022-05-13T00:00:00Z"}, {"upload_time_iso_8601": "2022-05-12T00:00:00Z"}],
        "2.5.0": [],  # a yanked or empty release has no date
    }})
    assert list(dates) == ["2.4.0"] and dates["2.4.0"].day == 12


def test_project_homes_come_from_the_registry_metadata():
    assert registry.parse_pypi_homes({"info": {"project_urls": {"Source": "https://github.com/jpadilla/pyjwt"},
                                               "home_page": "", "package_url": "https://pypi.org/project/PyJWT/"}}) == [
        "https://github.com/jpadilla/pyjwt", "https://pypi.org/project/PyJWT/"]
    assert registry.parse_npm_homes({"homepage": "https://example.dev",
                                     "repository": {"url": "git+https://github.com/auth0/node-jsonwebtoken.git"}}) == [
        "https://example.dev", "https://github.com/auth0/node-jsonwebtoken.git"]


def test_a_registry_that_is_down_raises(monkeypatch):
    monkeypatch.setattr(httpx, "get", FakeHttp(lambda m, url, body: (500, {})).get)
    with pytest.raises(httpx.HTTPStatusError):
        registry.fetch_releases(deps("requests")[0])
