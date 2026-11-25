"""Bounded research retrieval. This never creates a literature check."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from typing import Any, Protocol
from unicodedata import category
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .kernel import GateError, require
from .store import Store, canonical, digest


_CROSSREF_WORKS = "https://api.crossref.org/works"
_BANKNOTE_SOURCE = "https://archive.ics.uci.edu/dataset/267/banknote%2Bauthentication"
_ARXIV_HTML = re.compile(r"https://arxiv\.org/html/[0-9]{4}\.[0-9]{4,5}v[1-9][0-9]{0,3}")
_DOI = re.compile(r"10\.[0-9]{4,9}/[^\s\x00-\x1f\x7f]{1,180}", re.IGNORECASE)
_MAX_BYTES = 1024 * 1024
_MAX_ROWS = 10
_EXCERPT_CHARS = 4000
_FIXTURE = {
    "banknote-prior-art-v1": (
        "Offline fixture. Paraphrase of the UCI dataset catalog.\n"
        "Lohweg, V. (2012). Banknote Authentication [Dataset]. UCI Machine Learning "
        "Repository. DOI 10.24432/C55P57.\n"
        "The catalog already defines banknote authentication as a classification task. "
        "The public table contains 1372 examples and four image-derived features, "
        "including variance and skewness of the wavelet-transformed image.\n"
        f"Catalog: {_BANKNOTE_SOURCE}\n"
    ).encode("utf-8"),
}


class LiteratureFetch(Protocol):
    def __call__(self, url: str, *, max_bytes: int) -> bytes: ...


def validate_literature_action(action: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize one explicit request selected by the campaign operator."""
    require(type(action) is dict, "literature action must be an object")
    kind = action.get("kind")
    if kind == "crossref_search":
        require(set(action) == {"kind", "query", "rows"}, "invalid Crossref action fields")
        query, rows = action["query"], action["rows"]
        require(type(query) is str and 0 < len(query) <= 240 and query == query.strip()
                and all(category(char) not in {"Cc", "Cs"} for char in query),
                "invalid Crossref query")
        require(type(rows) is int and 1 <= rows <= _MAX_ROWS, "invalid Crossref result limit")
        return dict(kind=kind, query=query, rows=rows)
    if kind == "open_source":
        require(set(action) == {"kind", "url"}, "invalid open source action fields")
        url = action["url"]
        require(type(url) is str and (url == _BANKNOTE_SOURCE or _ARXIV_HTML.fullmatch(url)),
                "literature source URL is outside the public source allowlist")
        return dict(kind=kind, url=url)
    if kind == "fixture_source":
        require(set(action) == {"kind", "fixture_id"}, "invalid fixture action fields")
        fixture_id = action["fixture_id"]
        require(type(fixture_id) is str and fixture_id in _FIXTURE, "unknown literature fixture")
        return dict(kind=kind, fixture_id=fixture_id)
    raise GateError("unknown literature action")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_https(url: str, *, max_bytes: int) -> bytes:
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={"User-Agent": "EpistemeOS-literature/1",
                                    "Accept-Encoding": "identity"})
    try:
        with opener.open(request, timeout=10) as response:
            require(response.status == 200 and response.geturl() == url,
                    "literature endpoint did not return the approved URL")
            return response.read(max_bytes + 1)
    except GateError:
        raise
    except (OSError, ValueError) as exc:
        # Do not include a URL with the approved external query in an error.
        code = getattr(exc, "code", None)
        reason = f"HTTP {code}" if type(code) is int else type(exc).__name__
        raise GateError(f"literature fetch failed ({reason})") from None


def _single_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:limit].rstrip()
    if any(category(char) in {"Cc", "Cs"} for char in text):
        return None
    return text or None


def _metadata(data: bytes, limit: int) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise GateError("Crossref returned invalid JSON") from None
    message = payload.get("message") if isinstance(payload, dict) else None
    items = message.get("items") if isinstance(message, dict) else None
    require(isinstance(items, list) and len(items) <= limit, "invalid Crossref result envelope")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        doi = item.get("DOI")
        titles = item.get("title")
        title = _single_line(titles[0], 500) if isinstance(titles, list) and titles else None
        if (not isinstance(doi, str) or not _DOI.fullmatch(doi)
                or _single_line(doi, 200) != doi or title is None):
            continue
        doi = doi.lower()
        if doi in seen:
            continue
        seen.add(doi)
        author_data = item.get("author")
        authors = []
        for author in author_data[:32] if isinstance(author_data, list) else ():
            if isinstance(author, dict):
                joined_name = " ".join(part for name in ("given", "family")
                                       if (part := _single_line(author.get(name), 100)))
                name = joined_name[:200] or _single_line(author.get("name"), 200)
                if name:
                    authors.append(name)
        issued = item.get("issued")
        parts = issued.get("date-parts") if isinstance(issued, dict) else None
        year = None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            value = parts[0][0]
            if type(value) is int and 1 <= value <= 9999:
                year = value
        results.append(dict(doi=doi, title=title, authors=authors, year=year))
    return results


def _source_text(data: bytes) -> str:
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GateError("literature source must be UTF-8 text or HTML") from None
    require(source.strip() and not source.lstrip().startswith("%PDF")
            and not any(ord(char) < 32 and char not in "\t\r\n\f" for char in source),
            "literature source must be UTF-8 text or HTML")
    return source


def _result(action: dict[str, Any], data: bytes) -> dict[str, Any]:
    require(type(data) is bytes and 0 < len(data) <= _MAX_BYTES,
            "literature response must be bounded bytes")
    kind = action["kind"]
    if kind == "crossref_search":
        content_kind = "bibliographic_metadata"
        metadata = _metadata(data, action["rows"])
    else:
        content_kind = "fixture_source" if kind == "fixture_source" else "source_bytes"
        if kind == "fixture_source":
            require(data == _FIXTURE[action["fixture_id"]], "literature fixture bytes changed")
        _source_text(data)
        metadata = []
    return dict(action=action, artifact_digest=digest(data), content_kind=content_kind,
                metadata=metadata, scientific_validity="not_assessed")


def retrieve_literature(store: Store, action: dict[str, Any], *,
                        fetch: LiteratureFetch | None = None) -> dict[str, Any]:
    """Fetch one frozen action and put its bytes in CAS without appending events."""
    action = validate_literature_action(action)
    kind = action["kind"]
    if kind == "fixture_source":
        data = _FIXTURE[action["fixture_id"]]
    else:
        url = action.get("url")
        if kind == "crossref_search":
            query = urlencode({"query.bibliographic": action["query"], "rows": action["rows"],
                               "select": "DOI,title,author,issued"})
            url = f"{_CROSSREF_WORKS}?{query}"
        data = (fetch if fetch is not None else _fetch_https)(url, max_bytes=_MAX_BYTES)
    result = _result(action, data)
    store.put(data)
    return result


def validate_literature_result(store: Store, action: dict[str, Any],
                               result: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a persisted result from its frozen request and immutable bytes."""
    action = validate_literature_action(action)
    require(type(result) is dict and type(result.get("artifact_digest")) is str,
            "invalid literature result")
    expected = _result(action, store.read(result["artifact_digest"]))
    try:
        identical = canonical(result) == canonical(expected)
    except (TypeError, ValueError, RecursionError):
        identical = False
    require(identical, "literature result differs from its request or bytes")
    return expected


class _ExcerptParser(HTMLParser):
    _SKIP = frozenset({"head", "script", "style", "template", "svg", "math", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skipped.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skipped and tag == self.skipped[-1]:
            self.skipped.pop()

    def handle_data(self, data: str) -> None:
        if not self.skipped:
            self.parts.append(data)


def project_literature_result(store: Store, result: dict[str, Any], *,
                              start: int = 0) -> dict[str, Any]:
    """Project bounded untrusted material for a model, never a checked passage."""
    require(type(result) is dict and "action" in result, "invalid literature result")
    result = validate_literature_result(store, result["action"], result)
    require(type(start) is int and 0 <= start <= _MAX_BYTES, "invalid literature excerpt offset")
    if result["content_kind"] == "bibliographic_metadata":
        return result
    text = _source_text(store.read(result["artifact_digest"]))
    if result["content_kind"] == "source_bytes" and text.lstrip().startswith("<"):
        parser = _ExcerptParser()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    normalized = " ".join(text.split())
    return dict(result, untrusted_source_text=normalized[start:start + _EXCERPT_CHARS],
                excerpt_offset=start, checked_passage=False)
