"""Template-repo test: which tag `copier update` compatibility treats as the prior release.

A module of its own because test_copier_availability.py mocks `prior_release_ref` and runs
every case in test_update_compat.py, which would replace the function under test here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.<mod>` puts the root
    from test_update_compat import _commit, _git, prior_release_ref
except ImportError:  # the other invocation shape
    from tests.test_update_compat import _commit, _git, prior_release_ref


class PriorReleaseRef(unittest.TestCase):
    """The prior release is the newest one on this line, not the newest in the repository."""

    def test_a_later_release_on_another_line_is_not_the_prior_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q")
            (repo / "version").write_text("v1.1.0")
            _commit(repo, "v1.1.0")
            _git(repo, "tag", "v1.1.0")
            _git(repo, "branch", "release/1.1")
            (repo / "version").write_text("v1.2.0")
            _commit(repo, "v1.2.0")
            _git(repo, "tag", "v1.2.0")
            self.assertEqual(prior_release_ref(repo), "v1.2.0")
            _git(repo, "checkout", "-q", "release/1.1")
            self.assertEqual(prior_release_ref(repo), "v1.1.0")
            (repo / "version").write_text("v1.1.1")
            _commit(repo, "v1.1.1")
            _git(repo, "tag", "v1.1.1")
            self.assertEqual(prior_release_ref(repo), "v1.1.1")


if __name__ == "__main__":
    unittest.main()
