"""Tests for type constructors and edge cases."""

from __future__ import annotations

from beatport_collector.types import (
    Artist,
    Cart,
    Price,
)


class TestArtistConstructors:
    def test_from_dict_with_role(self) -> None:
        a = Artist.from_dict({"id": 1, "name": "Remixer A", "role": "remixer"})
        assert a.role == "remixer"

    def test_from_dict_with_type(self) -> None:
        a = Artist.from_dict({"id": 2, "name": "Remixer B", "type": "remixer"})
        assert a.role == "remixer"


class TestCart:
    def test_from_dict_default(self) -> None:
        c = Cart.from_dict(
            {"id": 123, "name": "cart", "default": True, "person_id": 456}
        )
        assert c.id == 123
        assert c.name == "cart"
        assert c.is_default is True
        assert c.person_id == 456

    def test_from_dict_non_default(self) -> None:
        c = Cart.from_dict({"id": 456, "name": "hold-bin", "default": False})
        assert c.is_default is False


class TestPrice:
    def test_from_dict(self) -> None:
        p = Price.from_dict(
            {"code": "AUD", "symbol": "AU$", "value": 2.09, "display": "AU$2.09"}
        )
        assert p.code == "AUD"
        assert p.display == "AU$2.09"
        assert p.value == 2.09

    def test_from_dict_empty(self) -> None:
        p = Price.from_dict({})
        assert p.code == ""
        assert p.value == 0.0

    def test_from_dict_none(self) -> None:
        p = Price.from_dict(None)  # type: ignore
        assert p.value == 0.0
