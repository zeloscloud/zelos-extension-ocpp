"""Tests for charger ID sanitization and path resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zelos_extension_ocpp.charger_id import (
    ChargerPathResolver,
    sanitize_path,
    sanitize_segment,
)


class TestSanitizeSegment:
    def test_strips_dots(self):
        # Dots are catalog path separators in Zelos — must be replaced.
        assert sanitize_segment("192.168.1.42") == "192_168_1_42"

    def test_strips_colons(self):
        # MAC addresses are a common cp_id source.
        assert sanitize_segment("AA:BB:CC:DD:EE:FF") == "AA_BB_CC_DD_EE_FF"

    def test_strips_slashes_within_segment(self):
        assert sanitize_segment("foo/bar") == "foo_bar"

    def test_collapses_runs(self):
        assert sanitize_segment("foo...bar:::baz") == "foo_bar_baz"

    def test_strips_whitespace(self):
        assert sanitize_segment("Bay 3 (north)") == "Bay_3_north"

    def test_strips_leading_trailing_separators(self):
        assert sanitize_segment("...cp...") == "cp"

    def test_empty_input_returns_empty(self):
        assert sanitize_segment("") == ""

    def test_all_unsafe_returns_empty(self):
        assert sanitize_segment("...") == ""

    def test_truncates_long_input(self):
        long_id = "a" * 200
        result = sanitize_segment(long_id)
        assert len(result) <= 64
        assert result == "a" * 64

    def test_keeps_alnum_underscore_hyphen(self):
        assert sanitize_segment("Wallbox-Pro_001") == "Wallbox-Pro_001"


class TestSanitizePath:
    def test_preserves_hierarchy_slashes(self):
        assert sanitize_path("depot/bay_3") == "depot/bay_3"

    def test_sanitizes_each_segment(self):
        # Dots in alias segments are still stripped per-segment.
        assert sanitize_path("site.east/bay 3") == "site_east/bay_3"

    def test_drops_empty_segments(self):
        assert sanitize_path("depot//bay_3//") == "depot/bay_3"

    def test_all_empty_returns_empty(self):
        assert sanitize_path("//...//") == ""


class TestChargerPathResolver:
    def test_no_aliases_uses_sanitized_cp_id(self):
        r = ChargerPathResolver()
        assert r.resolve("CP_001") == "CP_001"

    def test_alias_takes_precedence(self):
        r = ChargerPathResolver({"AABBCCDDEEFF": "depot_north/bay_3"})
        assert r.resolve("AABBCCDDEEFF") == "depot_north/bay_3"

    def test_alias_is_sanitized(self):
        # An alias with a dot still gets cleaned up.
        r = ChargerPathResolver({"raw": "site.1/bay_3"})
        assert r.resolve("raw") == "site_1/bay_3"

    def test_invalid_alias_is_ignored(self):
        # Alias that sanitizes away falls through to cp_id sanitization.
        r = ChargerPathResolver({"raw_id": "..."})
        assert r.resolve("raw_id") == "raw_id"

    def test_unresolvable_id_gets_fallback(self):
        r = ChargerPathResolver()
        assert r.resolve("...") == "cp_0"
        assert r.resolve("///") == "cp_1"

    def test_resolution_is_stable(self):
        # Same input -> same output across calls.
        r = ChargerPathResolver()
        first = r.resolve("...")  # gets cp_0
        assert r.resolve("...") == first

    def test_dotted_ip_address(self):
        r = ChargerPathResolver()
        assert r.resolve("192.168.1.42") == "192_168_1_42"

    def test_mac_address(self):
        r = ChargerPathResolver()
        assert r.resolve("AA:BB:CC:DD:EE:FF") == "AA_BB_CC_DD_EE_FF"

    def test_mappings_snapshot(self):
        r = ChargerPathResolver({"x": "alias_x"})
        r.resolve("x")
        r.resolve("y")
        assert r.mappings == {"x": "alias_x", "y": "y"}

    def test_from_file_loads_aliases(self, tmp_path: Path):
        p = tmp_path / "aliases.json"
        p.write_text(json.dumps({"raw1": "fleet/bay_1", "raw2": "fleet/bay_2"}))
        r = ChargerPathResolver.from_file(p)
        assert r.resolve("raw1") == "fleet/bay_1"
        assert r.resolve("raw2") == "fleet/bay_2"

    def test_from_file_missing_returns_empty(self, tmp_path: Path):
        r = ChargerPathResolver.from_file(tmp_path / "does_not_exist.json")
        assert r.resolve("raw") == "raw"

    def test_from_file_invalid_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all")
        r = ChargerPathResolver.from_file(p)
        assert r.resolve("raw") == "raw"

    def test_from_file_non_object_returns_empty(self, tmp_path: Path):
        p = tmp_path / "list.json"
        p.write_text(json.dumps(["not", "an", "object"]))
        r = ChargerPathResolver.from_file(p)
        assert r.resolve("raw") == "raw"

    def test_from_file_none_path(self):
        r = ChargerPathResolver.from_file(None)
        assert r.resolve("raw") == "raw"


class TestCollisionBehavior:
    """Document the (intentional) behavior when two raw IDs sanitize to the same path."""

    def test_distinct_ids_can_collide_after_sanitization(self):
        # "cp.001" and "cp:001" both sanitize to "cp_001" — they will share a
        # trace path. Operators must use aliases to disambiguate.
        r = ChargerPathResolver()
        assert r.resolve("cp.001") == "cp_001"
        assert r.resolve("cp:001") == "cp_001"

    def test_aliases_resolve_collisions(self):
        r = ChargerPathResolver({"cp.001": "bay_1", "cp:001": "bay_2"})
        assert r.resolve("cp.001") == "bay_1"
        assert r.resolve("cp:001") == "bay_2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
