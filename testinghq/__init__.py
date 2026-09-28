"""TestingHQ: self-testing tools for intake pipelines."""

#: Kept in step with `version` in pyproject.toml by a test, because the two are
#: written down separately and nothing else in the toolchain compares them: the
#: build reads pyproject, and `--version` reads this. They were both 0.0.1 for a
#: long time with nothing checking, which is exactly how a release ships
#: announcing one version and installing another.
__version__ = "0.1.0"
