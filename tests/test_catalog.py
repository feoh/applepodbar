"""Offline contract fixtures matching the observed AMP chart/editorial shapes."""

from __future__ import annotations

from typing import Any

from applepodbar.catalog import Catalog

SHOW = {"id": "1", "type": "podcasts", "attributes": {"name": "The Daily"}}


class FakeAmp:
    storefront = "us"

    def charts(self) -> dict[str, Any]:
        return {"results": {"podcasts": [{"name": "Top Shows", "data": [SHOW]}]}}

    def genres(self) -> dict[str, Any]:
        return {
            "data": [{"id": "1489", "type": "genres", "attributes": {"name": "News"}}]
        }

    def editorial_groupings(self) -> dict[str, Any]:
        return {
            "data": [
                {
                    "id": "33",
                    "type": "groupings",
                    "attributes": {"name": "Podcasts"},
                    "relationships": {
                        "tabs": {
                            "data": [
                                {
                                    "id": "tab",
                                    "type": "editorial-elements",
                                    "attributes": {"name": "Podcasts"},
                                    "relationships": {
                                        "children": {
                                            "data": [
                                                {
                                                    "id": "top",
                                                    "type": "editorial-elements",
                                                    "attributes": {"name": "Top Shows"},
                                                    "relationships": {
                                                        "children": {
                                                            "data": [
                                                                {
                                                                    "id": "contents",
                                                                    "type": "editorial-elements",
                                                                    "attributes": {},
                                                                    "relationships": {
                                                                        "contents": {
                                                                            "data": [
                                                                                SHOW
                                                                            ]
                                                                        }
                                                                    },
                                                                }
                                                            ]
                                                        }
                                                    },
                                                }
                                            ]
                                        }
                                    },
                                }
                            ]
                        }
                    },
                }
            ]
        }

    def _catalog(self, suffix: str) -> str:
        return "catalog/us/" + suffix

    def get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        assert path == "catalog/us/charts"
        assert params["genre"] == "1489"
        return self.charts()


def test_chart_genre_and_nested_editorial_content() -> None:
    catalog = object.__new__(Catalog)
    catalog._use_rss = False
    catalog.amp = FakeAmp()  # type: ignore[assignment]
    assert [s.title for s in catalog.charts()] == ["The Daily"]
    assert [(i.id, i.title) for i in catalog.genres()] == [("genre:1489", "News")]
    assert [s.id for s in catalog.genre_shows("1489")] == ["1"]
    assert ("top", "Top Shows") in [(i.id, i.title) for i in catalog.editorial()]
    assert [s.title for s in catalog.grouping_shows("top")] == ["The Daily"]
