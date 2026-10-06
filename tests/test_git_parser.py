from __future__ import annotations

from pathlib import Path

from conftest import _git, commit_all

from rat.git_parser import GitHistoryParser
from rat.models import ObjectType


def test_aggregate_changes_rolls_files_up_to_all_ancestors() -> None:
    changes = GitHistoryParser._aggregate_changes(
        [("src/api/main.py", 4, 1), ("src/models.py", 2, 3)]
    )
    indexed = {(change.object_type, change.path): change for change in changes}

    assert indexed[(ObjectType.REPOSITORY, "/")].added == 6
    assert indexed[(ObjectType.REPOSITORY, "/")].removed == 4
    assert indexed[(ObjectType.DIRECTORY, "src")].churn == 10
    assert indexed[(ObjectType.DIRECTORY, "src/api")].churn == 5
    assert indexed[(ObjectType.FILE, "src/api/main.py")].churn == 5


def test_rename_destination_supports_git_compact_and_full_formats() -> None:
    assert GitHistoryParser._rename_destination("old.py => new.py") == "new.py"
    assert GitHistoryParser._rename_destination("src/{old.py => new.py}") == "src/new.py"
    assert GitHistoryParser._rename_destination("{old => new}/file.txt") == "new/file.txt"


def test_parser_applies_mailmap_ignores_binary_and_handles_renames(
    git_repository: Path,
) -> None:
    (git_repository / ".mailmap").write_text(
        "Canonical Author <canonical@example.com> Alias Author <alias@example.com>\n",
        encoding="utf-8",
    )
    (git_repository / "old.txt").write_text("one\ntwo\n", encoding="utf-8")
    (git_repository / "image.bin").write_bytes(b"\x00\x01\x02")
    commit_all(
        git_repository,
        "initial",
        author_name="Alias Author",
        author_email="alias@example.com",
    )

    _git(git_repository, "mv", "old.txt", "renamed.txt")
    commit_all(git_repository, "pure rename", timestamp=1_700_000_100)

    (git_repository / "src").mkdir()
    _git(git_repository, "mv", "renamed.txt", "src/main.txt")
    with (git_repository / "src/main.txt").open("a", encoding="utf-8") as stream:
        stream.write("three\n")
    commit_all(git_repository, "rename and edit", timestamp=1_700_000_200)

    parser = GitHistoryParser(git_repository)
    parser.validate()
    commits = list(parser.iter_commits(parser.resolve_ref()))

    assert len(commits) == 3
    oldest = commits[-1]
    assert oldest.commit.author_display == "Canonical Author <canonical@example.com>"
    assert all(change.path != "image.bin" for change in oldest.changes)

    pure_rename = commits[1]
    assert pure_rename.changes
    assert all(change.churn == 0 for change in pure_rename.changes)
    assert any(
        change.object_type == ObjectType.FILE and change.path == "renamed.txt"
        for change in pure_rename.changes
    )

    newest = commits[0]
    indexed = {(change.object_type, change.path): change for change in newest.changes}
    assert indexed[(ObjectType.FILE, "src/main.txt")].added == 1
    assert indexed[(ObjectType.FILE, "src/main.txt")].removed == 0
    assert indexed[(ObjectType.DIRECTORY, "src")].churn == 1
    assert indexed[(ObjectType.REPOSITORY, "/")].churn == 1
