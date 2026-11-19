"""Release notes for ``build_release`` come from a local ``--notes-file``.

Discord notifications still read the published GitHub release body.
These tests never create a release, package an EXE, or call Discord.
"""

import os
import subprocess

import pytest

import build_release

_VALID_NOTES = "Bilingual release notes: 繁體中文 and English.\n"


def _assert_exit(capsys, fn, *args, needle):
    with pytest.raises(SystemExit) as exc:
        fn(*args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "ERROR:" in out
    assert needle in out
    assert "Release notes OK" not in out
    return out


@pytest.mark.parametrize("notes_file", [None, "", "   ", "\n\t"])
def test_verify_requires_notes_file(capsys, notes_file):
    out = _assert_exit(
        capsys, build_release.verify_release_notes, notes_file,
        needle="--notes-file <path> is required",
    )
    assert "--skip-release" in out
    assert "untracked" in out


def test_verify_rejects_missing_file(tmp_path, capsys):
    missing = tmp_path / "no-such-notes.md"
    expected = os.path.abspath(str(missing))
    out = _assert_exit(
        capsys, build_release.verify_release_notes, str(missing),
        needle=f"release notes file not found: {expected}",
    )
    assert expected in out


def test_verify_rejects_directory(tmp_path, capsys):
    expected = os.path.abspath(str(tmp_path))
    _assert_exit(
        capsys, build_release.verify_release_notes, str(tmp_path),
        needle=f"release notes file not found: {expected}",
    )


@pytest.mark.parametrize("text", ["", "   \n\t", "x" * 19])
def test_verify_rejects_short_file(tmp_path, capsys, text):
    notes = tmp_path / "notes.md"
    notes.write_text(text, encoding="utf-8")
    out = _assert_exit(
        capsys, build_release.verify_release_notes, str(notes),
        needle="empty or too short",
    )
    assert f"({len(text.strip())} chars)" in out


def test_verify_rejects_invalid_utf8(tmp_path, capsys):
    notes = tmp_path / "notes.md"
    notes.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    expected = os.path.abspath(str(notes))
    out = _assert_exit(
        capsys, build_release.verify_release_notes, str(notes),
        needle=f"cannot read release notes file {expected}",
    )
    assert "UnicodeDecodeError" in out or "utf-8" in out


def test_verify_accepts_file_outside_repo(tmp_path, capsys):
    notes = tmp_path / "release-notes.md"
    notes.write_text(_VALID_NOTES, encoding="utf-8")
    result = build_release.verify_release_notes(str(notes))
    expected = os.path.abspath(str(notes))
    repo = os.path.dirname(os.path.abspath(build_release.__file__))
    assert result == expected
    assert os.path.isabs(result)
    assert os.path.commonpath([repo, result]) != os.path.normpath(repo)
    out = capsys.readouterr().out
    assert f"Release notes OK ({expected}, {len(_VALID_NOTES.strip())} chars)" in out


def test_verify_accepts_relative_path(tmp_path, monkeypatch, capsys):
    notes = tmp_path / "notes.md"
    notes.write_text(_VALID_NOTES, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = build_release.verify_release_notes("notes.md")
    expected = os.path.abspath("notes.md")
    assert result == expected
    assert os.path.isabs(result)
    out = capsys.readouterr().out
    assert f"Release notes OK ({expected}, {len(_VALID_NOTES.strip())} chars)" in out


def _stub_flow(monkeypatch, *, sha="abc123def0", dirty=False):
    """Stub everything past the notes/dirty gates. No EXE, git, or Discord."""
    state = {
        "package": 0,
        "release": 0,
        "head": 0,
        "notify": 0,
        "notes": None,
        "notify_sha": None,
    }

    monkeypatch.setattr(build_release, "git_commit_info", lambda: (sha, dirty))

    def fail_build(**_kwargs):
        raise AssertionError("PyInstaller build must not run in these tests")

    def package():
        state["package"] += 1
        return "dist/x.zip"

    def create(zip_path, sha_path, got_sha, notes_file):
        state["release"] += 1
        state["notes"] = notes_file
        state["create_args"] = (zip_path, sha_path, got_sha)
        return True

    def verify_head():
        state["head"] += 1

    def notify(got_sha, *, force=False):
        state["notify"] += 1
        state["notify_sha"] = got_sha
        state["notify_force"] = force

    monkeypatch.setattr(build_release, "build", fail_build)
    monkeypatch.setattr(build_release, "verify_bundle_matches_source", lambda: None)
    monkeypatch.setattr(build_release, "write_build_stamp", lambda *_a, **_k: None)
    monkeypatch.setattr(build_release, "package", package)
    monkeypatch.setattr(build_release, "write_checksum", lambda _path: "dist/x.zip.sha256")
    monkeypatch.setattr(build_release, "verify_head_pushed", verify_head)
    monkeypatch.setattr(build_release, "create_github_release", create)
    monkeypatch.setattr(build_release, "notify_release", notify)
    return state


def test_main_skip_build_requires_notes_file_before_package(monkeypatch, capsys):
    state = _stub_flow(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        build_release.main(["--skip-build"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "--notes-file <path> is required" in out
    assert state["package"] == 0
    assert state["release"] == 0
    assert state["head"] == 0
    assert state["notify"] == 0


def test_main_skip_build_passes_resolved_notes_path(tmp_path, monkeypatch):
    notes = tmp_path / "release-notes.md"
    notes.write_text(_VALID_NOTES, encoding="utf-8")
    state = _stub_flow(monkeypatch, sha="abc123def0")
    build_release.main(["--skip-build", "--notes-file", str(notes)])
    assert state["notes"] == os.path.abspath(str(notes))
    assert state["head"] == 1
    assert state["notify"] == 1
    assert state["notify_sha"] == "abc123def0"
    assert state["release"] == 1
    assert state["package"] == 1


def test_main_skip_release_does_not_require_notes_file(monkeypatch, capsys):
    state = _stub_flow(monkeypatch, sha="abc123def0")

    def forbid_notes(_path):
        raise AssertionError("--skip-release must not read --notes-file")

    monkeypatch.setattr(build_release, "verify_release_notes", forbid_notes)
    build_release.main(["--skip-build", "--skip-release"])
    out = capsys.readouterr().out
    assert "--notes-file <path-to-notes.md>" in out
    assert state["release"] == 0
    assert state["notify"] == 0

    build_release.main([
        "--skip-build", "--skip-release", "--notes-file", "foo.md",
    ])
    out = capsys.readouterr().out
    assert "--notes-file foo.md" in out
    assert "<path-to-notes.md>" not in out
    assert state["release"] == 0


def test_main_notify_only_does_not_require_notes_file(monkeypatch):
    state = _stub_flow(monkeypatch, sha="abc123def0")

    def forbid_notes(_path):
        raise AssertionError("--notify-only must not read --notes-file")

    monkeypatch.setattr(build_release, "verify_release_notes", forbid_notes)
    monkeypatch.setattr(
        build_release, "verify_github_release_exists", lambda: True,
    )
    build_release.main(["--notify-only"])
    assert state["notify"] == 1
    assert state["notify_sha"] == "abc123def0"
    assert state["package"] == 0
    assert state["release"] == 0
    assert state["head"] == 0


def test_main_dirty_tree_exits_before_release(tmp_path, monkeypatch, capsys):
    notes = tmp_path / "release-notes.md"
    notes.write_text(_VALID_NOTES, encoding="utf-8")
    state = _stub_flow(monkeypatch, dirty=True)
    with pytest.raises(SystemExit) as exc:
        build_release.main(["--skip-build", "--notes-file", str(notes)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "uncommitted changes" in out
    assert state["package"] == 0
    assert state["release"] == 0
    assert state["notify"] == 0


def test_load_release_notes_reads_github_release_only(monkeypatch):
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        body = "Published bilingual notes from GitHub.\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

    def guarding_open(*_args, **_kwargs):
        raise AssertionError("release notes must not read a local file")

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.open", guarding_open)
    text = build_release._load_release_notes()
    assert text == "Published bilingual notes from GitHub."
    assert calls == [[
        "gh", "release", "view", f"v{build_release.VERSION}",
        "--json", "body", "-q", ".body",
    ]]


@pytest.mark.parametrize("stdout,code", [("   \n", 0), ("local fallback", 1)])
def test_load_release_notes_does_not_fall_back_to_a_local_file(
    monkeypatch, stdout, code,
):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")

    def guarding_open(*_args, **_kwargs):
        raise AssertionError("release notes must not read a local file")

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.open", guarding_open)
    assert build_release._load_release_notes() == ""
