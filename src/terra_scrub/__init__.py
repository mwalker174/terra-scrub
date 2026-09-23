"""terra-scrub: inventory, cleanup-candidate and delete-PLAN toolkit for Terra buckets.

Every module in this package except ``approve`` and ``estate`` is GET-only by
construction and lint-enforced (see ``tests/test_readonly_lint.py``). Nothing in
this package deletes a GCS object. The ``plan`` command writes a wrapper script
that a human must arm; running it is a deliberate act outside this tool.
"""

__version__ = "0.1.0"
