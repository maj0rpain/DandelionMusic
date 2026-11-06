"""The shared library fakes' contract."""

import pytest

from library_fakes import external_stats


def test_external_stats_defaults_every_field_and_rejects_unknown_ones():
    stats = external_stats()

    assert stats.tags == ()
    assert all(
        getattr(stats, name) is None
        for name in stats._fields
        if name != "tags"
    )
    with pytest.raises(TypeError):
        external_stats(nope=1)
