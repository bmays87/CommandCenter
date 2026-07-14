"""Reusable conformance suite every adapter must pass.

Usage in an adapter package::

    from prodeo.adapters.testing import AdapterConformanceSuite

    class TestMyAdapterConformance(AdapterConformanceSuite):
        @pytest.fixture
        def adapter(self, tmp_path):
            ...  # return a fresh adapter with at least one observable session

Override :meth:`AdapterConformanceSuite.provoke_activity` if the watched
session only produces observations when poked.
"""

from prodeo.adapters.testing.suite import AdapterConformanceSuite, recording_context

__all__ = ["AdapterConformanceSuite", "recording_context"]
