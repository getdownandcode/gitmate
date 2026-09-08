"""Tests for DiffExtractor against scratch repos with real git (never mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import commit_file, stage_file

from gitmate.diff_extractor import (
    DiffExtractor,
    GitCommandError,
    NotAGitRepo,
    _parse_name_status,
    matches_any,
)


def test_staged_modify_reports_counts(git_repo: Path) -> None:
    commit_file(git_repo, "a.py", "l1\nl2\n", "add a")
    stage_file(git_repo, "a.py", b"l1\nl2\nl3\nl4\n")
    (diff,) = DiffExtractor(git_repo).staged()
    assert (diff.path, diff.status) == ("a.py", "modified")
    assert (diff.additions, diff.deletions) == (2, 0)
    assert "+l3" in diff.patch_text and not diff.is_binary


def test_staged_new_file_is_added(git_repo: Path) -> None:
    stage_file(git_repo, "new.py", b"x = 1\n")
    (diff,) = DiffExtractor(git_repo).staged()
    assert (diff.path, diff.status, diff.old_path) == ("new.py", "added", None)
    assert diff.additions == 1


def test_staged_delete(git_repo: Path) -> None:
    commit_file(git_repo, "gone.py", "x\n", "add gone")
    subprocess.run(["git", "rm", "gone.py"], cwd=git_repo, check=True, capture_output=True)
    (diff,) = DiffExtractor(git_repo).staged()
    assert (diff.path, diff.status) == ("gone.py", "deleted")


def test_rename_detected_without_ambient_config(git_repo: Path) -> None:
    commit_file(git_repo, "old.py", "x = 1\n", "add old")
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=git_repo, check=True)
    (diff,) = DiffExtractor(git_repo).staged()
    assert (diff.path, diff.status, diff.old_path) == ("new.py", "renamed", "old.py")


def test_rename_with_edits_keeps_counts(git_repo: Path) -> None:
    commit_file(git_repo, "old.py", "x = 1\ny = 2\n", "add old")
    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=git_repo, check=True)
    (git_repo / "new.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "new.py"], cwd=git_repo, check=True)
    (diff,) = DiffExtractor(git_repo).staged()
    assert (diff.path, diff.status, diff.old_path) == ("new.py", "renamed", "old.py")
    assert diff.additions >= 1


def test_binary_flagged_and_patch_dropped(git_repo: Path) -> None:
    stage_file(git_repo, "img.bin", b"\x00\x01\x02binary\xff")
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "img.bin" and diff.is_binary
    assert diff.patch_text == ""


def test_noise_filtered_including_nested_paths(git_repo: Path) -> None:
    stage_file(git_repo, "package-lock.json", b"{}\n")
    stage_file(git_repo, "pkg/package-lock.json", b"{}\n")
    stage_file(git_repo, "dist/assets/bundle.js", b"var x;\n")
    stage_file(git_repo, "app.min.js", b"var x;\n")
    stage_file(git_repo, "real.py", b"y = 2\n")
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "real.py"


def test_user_ignores_extend_defaults(git_repo: Path) -> None:
    stage_file(git_repo, "foo.gen.py", b"gen\n")
    stage_file(git_repo, "package-lock.json", b"{}\n")
    stage_file(git_repo, "keep.py", b"ok\n")
    diffs = DiffExtractor(git_repo, extra_ignores=["*.gen.py"]).staged()
    assert [d.path for d in diffs] == ["keep.py"]


def test_matcher_rules() -> None:
    assert matches_any("dist/assets/b.js", ["dist/*"])
    assert matches_any("pkg/package-lock.json", ["package-lock.json"])
    assert matches_any("a/b/c.min.js", ["*.min.js"])
    assert not matches_any("src/app.py", ["dist/*", "*.min.js"])
    assert matches_any("out/log.txt", ["out/"])


def test_branch_comparison(git_repo: Path) -> None:
    commit_file(git_repo, "base.py", "v1\n", "base")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=git_repo, check=True)
    commit_file(git_repo, "base.py", "v1\nv2\n", "bump")
    (diff,) = DiffExtractor(git_repo).branch_comparison("main")
    assert (diff.path, diff.additions) == ("base.py", 1)


def test_rev_range(git_repo: Path) -> None:
    commit_file(git_repo, "c.py", "1\n", "one")
    subprocess.run(["git", "tag", "v1"], cwd=git_repo, check=True)
    commit_file(git_repo, "c.py", "1\n2\n", "two")
    subprocess.run(["git", "tag", "v2"], cwd=git_repo, check=True)
    (diff,) = DiffExtractor(git_repo).rev_range("v1", "v2")
    assert (diff.path, diff.additions) == ("c.py", 1)


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(NotAGitRepo):
        DiffExtractor(tmp_path / "nope").staged()


def test_empty_diff(git_repo: Path) -> None:
    assert DiffExtractor(git_repo).staged() == []


def test_bad_ref_raises(git_repo: Path) -> None:
    with pytest.raises(GitCommandError):
        DiffExtractor(git_repo).branch_comparison("does-not-exist")


def test_filename_with_spaces_staged(git_repo: Path) -> None:
    commit_file(git_repo, "my cool file.py", "line 1\n", "initial")
    stage_file(git_repo, "my cool file.py", b"line 1\nline 2\n")
    stage_file(git_repo, "brand new file.py", b"print('hello')\n")
    diffs = DiffExtractor(git_repo).staged()
    diff_map = {d.path: d for d in diffs}
    assert "my cool file.py" in diff_map
    assert diff_map["my cool file.py"].status == "modified"
    assert diff_map["my cool file.py"].additions == 1
    assert "+line 2" in diff_map["my cool file.py"].patch_text
    assert "brand new file.py" in diff_map
    assert diff_map["brand new file.py"].status == "added"
    assert "+print('hello')" in diff_map["brand new file.py"].patch_text


def test_filename_with_spaces_binary(git_repo: Path) -> None:
    stage_file(git_repo, "bin space.bin", b"\x00\x01\x02\xffbinary\x00")
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "bin space.bin"
    assert diff.is_binary
    assert diff.patch_text == ""


def test_non_ascii_filename(git_repo: Path) -> None:
    stage_file(git_repo, "café.txt", b"coffee\n")
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "café.txt"
    assert diff.status == "added"
    assert diff.additions == 1
    assert not diff.is_binary
    assert "+coffee" in diff.patch_text


def test_mnemonic_prefix_ambient_config(git_repo: Path) -> None:
    # Ambient config already sets diff.mnemonicPrefix=true in git_repo fixture
    commit_file(git_repo, "prefix_test.py", "v1\n", "initial")
    stage_file(git_repo, "prefix_test.py", b"v1\nv2\n")
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "prefix_test.py"
    assert "+v2" in diff.patch_text
    assert diff.status == "modified"


def test_pure_rename_with_spaces(git_repo: Path) -> None:
    commit_file(git_repo, "old name.py", "content\n", "add old")
    subprocess.run(["git", "mv", "old name.py", "new name.py"], cwd=git_repo, check=True)
    (diff,) = DiffExtractor(git_repo).staged()
    assert diff.path == "new name.py"
    assert diff.old_path == "old name.py"
    assert diff.status == "renamed"
    assert "rename from old name.py" in diff.patch_text
    assert "rename to new name.py" in diff.patch_text


def test_copy_status_row() -> None:
    statuses = _parse_name_status("C100\told.py\tnew.py\n")
    assert statuses["new.py"] == ("added", "old.py")
