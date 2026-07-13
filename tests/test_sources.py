"""Registry completeness: every source a FieldSpec priority names must have a
registered adapter, so resolution can never point at a source that doesn't
exist."""

from argus.fields import SPECS, Source
from argus.sources import ALL_SOURCE_TYPES


def test_every_priority_source_has_a_registered_adapter():
    registered = {cls.source_id for cls in ALL_SOURCE_TYPES}
    needed = {source for spec in SPECS.values() for source in spec.priority}
    missing = needed - registered
    assert not missing, f"priority sources without an adapter: {missing}"


def test_registered_adapters_have_distinct_source_ids():
    ids = [cls.source_id for cls in ALL_SOURCE_TYPES]
    assert len(ids) == len(set(ids))
    assert all(isinstance(s, Source) for s in ids)
