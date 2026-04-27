"""Test wishlist and stock response parsing."""

from __future__ import annotations

from decimal import Decimal

from avtozyabr.poller.wishlist import _parse_wishlist, _parse_stock


class TestWishlistParsing:
    def test_flat_list(self):
        raw = [
            {"id": 101, "name": "Крем", "url": "/p/101", "imageUrl": "https://img/1.jpg"},
            {"id": 102, "name": "Тоник", "url": "/p/102"},
        ]
        items = _parse_wishlist(raw)
        assert len(items) == 2
        assert items[0].product_id == 101
        assert items[0].name == "Крем"
        assert items[1].image_url is None

    def test_nested_product_key(self):
        raw = [{"product": {"id": 55, "name": "Маска", "url": "/p/55"}}]
        items = _parse_wishlist(raw)
        assert len(items) == 1
        assert items[0].product_id == 55

    def test_malformed_entry_skipped(self):
        raw = [{"bad": "data"}, {"id": 10, "name": "OK", "url": "/p/10"}]
        items = _parse_wishlist(raw)
        # Entry with id=10 has no product key, but also no "product" wrapper;
        # the parser tries direct access first
        assert any(i.product_id == 10 for i in items)


class TestStockParsing:
    def test_parse_variants(self):
        raw = {
            "variants": [
                {"id": 1001, "name": "Белый", "inStock": True, "price": 999},
                {"id": 1002, "name": "Чёрный", "inStock": False, "price": 1100},
            ]
        }
        stock = _parse_stock(product_id=5, raw=raw)
        assert len(stock.variants) == 2
        assert stock.variants[0].in_stock is True
        assert stock.variants[1].price == Decimal("1100")

    def test_fallback_single_variant(self):
        raw = {"inStock": True, "price": 500}
        stock = _parse_stock(product_id=7, raw=raw)
        assert len(stock.variants) == 1
        assert stock.variants[0].in_stock is True
        assert stock.variants[0].variant_id == 7
