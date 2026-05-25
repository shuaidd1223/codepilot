"""Tests for webapp file search caching."""

from __future__ import annotations

import uuid

import pytest

from codepilot.storage import database as db
from codepilot.webapp.server import _search_project_files, _get_project_files_cached


class TestFileSearchCache:
    """Test that _search_project_files uses caching for performance."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        """Set up test project."""
        monkeypatch.setenv("CODEPILOT_DB_PATH", str(tmp_path / "tasks.db"))

        # Use unique project name to avoid conflicts
        self.project_name = f"test_project_{uuid.uuid4().hex[:8]}"
        self.project_path = tmp_path / "test_project"
        self.project_path.mkdir()

        # Create some test files
        (self.project_path / "file1.txt").write_text("content1")
        (self.project_path / "file2.py").write_text("print('hello')")
        (self.project_path / "subdir").mkdir()
        (self.project_path / "subdir" / "file3.md").write_text("# heading")

        # Register project
        db.init_db()
        db.register_project(self.project_name, str(self.project_path))

        # Clear LRU cache before each test
        _get_project_files_cached.cache_clear()

    def test_file_search_returns_results(self):
        """Basic sanity check: file search returns results."""
        result = _search_project_files(self.project_name, "")
        assert "files" in result
        assert len(result["files"]) > 0

    def test_file_search_with_query(self):
        """File search with query works."""
        result = _search_project_files(self.project_name, "file1")
        assert "files" in result
        # Should match file1.txt
        assert any("file1" in f["name"] for f in result["files"])

    def test_cached_function_is_callable(self):
        """Verify the cached function exists and is callable."""
        # The cached function should exist for direct testing
        assert callable(_get_project_files_cached)

    def test_cache_returns_same_result(self):
        """Cached function returns same results for same project."""
        result1 = _get_project_files_cached(str(self.project_path))
        result2 = _get_project_files_cached(str(self.project_path))
        # Results should be equivalent (same files)
        assert len(result1) == len(result2)
        # result is tuple of (path, name) tuples
        paths1 = {f[0] for f in result1}
        paths2 = {f[0] for f in result2}
        assert paths1 == paths2

    def test_cache_key_based_on_path(self):
        """Cache is keyed by project path."""
        # Different project paths should have different cache entries
        other_path = self.project_path.parent / "other_project"
        other_path.mkdir()
        (other_path / "other.txt").write_text("content")

        result1 = _get_project_files_cached(str(self.project_path))
        result2 = _get_project_files_cached(str(other_path))

        # Different sets of files
        assert {f[0] for f in result1} != {f[0] for f in result2}
