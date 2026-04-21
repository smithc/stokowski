"""Tests for the CLI workflow-path discovery helper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from stokowski.main import resolve_workflow_paths


def _cd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_zero_args_auto_detects_workflow_yaml(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    (tmp_path / "workflow.yaml").write_text("tracker:\n  kind: linear\n")
    paths = resolve_workflow_paths([])
    assert paths == [Path("./workflow.yaml")]


def test_zero_args_prefers_yaml_over_yml(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    (tmp_path / "workflow.yaml").write_text("x: 1")
    (tmp_path / "workflow.yml").write_text("x: 1")
    paths = resolve_workflow_paths([])
    assert paths == [Path("./workflow.yaml")]


def test_zero_args_falls_through_to_workflow_md(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    (tmp_path / "WORKFLOW.md").write_text("# legacy")
    paths = resolve_workflow_paths([])
    assert paths == [Path("./WORKFLOW.md")]


def test_zero_args_no_files_raises(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError):
        resolve_workflow_paths([])


def test_single_file_arg_preserves_legacy_one_entry(tmp_path):
    p = tmp_path / "workflow.yaml"
    p.write_text("x: 1")
    paths = resolve_workflow_paths([str(p)])
    assert paths == [p]


def test_single_directory_arg_enumerates_yaml_files_sorted(tmp_path):
    (tmp_path / "workflow.b.yaml").write_text("x: 1")
    (tmp_path / "workflow.a.yaml").write_text("x: 1")
    paths = resolve_workflow_paths([str(tmp_path)])
    assert [p.name for p in paths] == ["workflow.a.yaml", "workflow.b.yaml"]


def test_single_directory_includes_both_yaml_and_yml(tmp_path):
    (tmp_path / "workflow.a.yaml").write_text("x: 1")
    (tmp_path / "workflow.b.yml").write_text("x: 1")
    paths = resolve_workflow_paths([str(tmp_path)])
    assert sorted(p.suffix for p in paths) == [".yaml", ".yml"]


def test_single_directory_empty_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no .yaml"):
        resolve_workflow_paths([str(tmp_path)])


def test_single_directory_ignores_non_yaml(tmp_path):
    (tmp_path / "workflow.yaml").write_text("x: 1")
    (tmp_path / "notes.md").write_text("hi")
    (tmp_path / "README.txt").write_text("hi")
    paths = resolve_workflow_paths([str(tmp_path)])
    assert [p.name for p in paths] == ["workflow.yaml"]


def test_explicit_list_of_paths(tmp_path):
    a = tmp_path / "workflow.a.yaml"
    b = tmp_path / "workflow.b.yaml"
    a.write_text("x: 1")
    b.write_text("x: 1")
    paths = resolve_workflow_paths([str(b), str(a)])
    # Sorted case-insensitive
    assert [p.name for p in paths] == ["workflow.a.yaml", "workflow.b.yaml"]


def test_glob_expansion(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    (tmp_path / "workflow.a.yaml").write_text("x: 1")
    (tmp_path / "workflow.b.yaml").write_text("x: 1")
    (tmp_path / "other.yaml").write_text("x: 1")
    paths = resolve_workflow_paths(["workflow.*.yaml"])
    names = sorted(p.name for p in paths)
    assert names == ["workflow.a.yaml", "workflow.b.yaml"]


def test_glob_no_matches_raises(tmp_path, monkeypatch):
    _cd(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError, match="matched no files"):
        resolve_workflow_paths(["workflow.*.yaml"])


def test_duplicate_paths_dedup_by_resolved_path(tmp_path):
    p = tmp_path / "workflow.yaml"
    p.write_text("x: 1")
    paths = resolve_workflow_paths([str(p), str(p)])
    assert len(paths) == 1


def test_nonexistent_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_workflow_paths([str(tmp_path / "nope.yaml")])


def test_case_insensitive_sort_across_platforms(tmp_path):
    a = tmp_path / "Workflow.a.yaml"
    b = tmp_path / "workflow.b.yaml"
    a.write_text("x: 1")
    b.write_text("x: 1")
    paths = resolve_workflow_paths([str(b), str(a)])
    # 'Workflow.a' casefold sorts before 'workflow.b', so Workflow.a.yaml first.
    assert [p.name for p in paths] == ["Workflow.a.yaml", "workflow.b.yaml"]
