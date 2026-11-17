import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gdexws import cli
from gdexws.tools import create_group as cg
from gdexws.tools.create_group import CreateGroupError, create_group


@pytest.fixture(autouse=True)
def log():
    """Capture service_log calls instead of writing the jsonl log."""
    with patch.object(cg, "service_log") as mock:
        yield mock


def _make_tree(root: Path, rel_files):
    """Create empty files at the given paths relative to ``root``."""
    for rel in rel_files:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()


def _indices(path: Path) -> dict:
    """Return {group_key: group_index} from a saved settings JSON."""
    return {k: v["group_index"] for k, v in json.loads(path.read_text()).items()}


@pytest.fixture
def source(tmp_path):
    """Source with two branching runs: runA, runB."""
    src = tmp_path / "src"
    _make_tree(src, ["runA/diag.1.nc", "runA/hist.1.nc", "runB/diag.1.nc"])
    return src


class TestFreshRun:
    def test_one_group_per_branch_sorted_from_1(self, source, tmp_path):
        out = tmp_path / "groups.json"
        groups = create_group(source, out)
        assert _indices(out) == {"runA": 1, "runB": 2}
        assert json.loads(out.read_text()) == groups
        assert groups["runA"]["parent_index"] == 0
        assert groups["runA"]["group_name"] == "runA_gidx_1"

    def test_default_include_is_every_file(self, source, tmp_path):
        groups = create_group(source, tmp_path / "groups.json")
        assert groups["runA"]["file_pattern"] == ["runA/*"]

    def test_include_patterns(self, source, tmp_path):
        groups = create_group(source, tmp_path / "groups.json", include=["diag.*.nc", "hist.*.nc"])
        assert groups["runA"]["file_pattern"] == ["runA/diag.*.nc", "runA/hist.*.nc"]
        assert groups["runA"]["webfile_pattern"] == ["runA/diag.*.nc", "runA/hist.*.nc"]

    def test_nested_branch_chains_parent_index(self, tmp_path):
        src = tmp_path / "src"
        _make_tree(src, ["a/x/f.nc", "a/y/f.nc", "b/f.nc"])
        groups = create_group(src, tmp_path / "groups.json")
        assert {k: (v["group_index"], v["parent_index"]) for k, v in groups.items()} == {
            "a": (1, 0), "b": (2, 0), "a_x": (3, 1), "a_y": (4, 1),
        }

    def test_single_child_dir_is_not_a_new_group(self, tmp_path):
        """runA/out holds the files; it is runA's only subdir, so files join group runA."""
        src = tmp_path / "src"
        _make_tree(src, ["runA/out/f.nc", "runB/f.nc"])
        groups = create_group(src, tmp_path / "groups.json")
        assert set(groups) == {"runA", "runB"}
        assert groups["runA"]["file_pattern"] == ["runA/out/*"]

    def test_files_under_root_are_skipped_with_warning(self, source, tmp_path, log):
        (source / "stray.nc").touch()
        groups = create_group(source, tmp_path / "groups.json")
        assert set(groups) == {"runA", "runB"}
        warnings = [c for c in log.call_args_list if c.args[1] == "WARNING"]
        assert len(warnings) == 1
        assert "directly under scan root" in warnings[0].args[2]

    def test_existing_output_is_overwritten_without_existing_json(self, source, tmp_path):
        out = tmp_path / "groups.json"
        out.write_text(json.dumps({"old": {"group_index": 7}}))
        create_group(source, out)
        assert _indices(out) == {"runA": 1, "runB": 2}


class TestExistingJson:
    @pytest.fixture
    def first(self, source, tmp_path):
        """Settings from a first run over runA/runB."""
        path = tmp_path / "first.json"
        create_group(source, path)
        return path

    def test_different_output_creates_new_file_and_keeps_old(self, source, tmp_path, first):
        before = first.read_text()
        _make_tree(source, ["runC/diag.1.nc"])
        out = tmp_path / "second.json"

        create_group(source, out, existing=first)

        assert out.exists()
        assert _indices(out) == {"runA": 1, "runB": 2, "runC": 3}
        assert first.read_text() == before  # old JSON untouched

    def test_same_path_overwrites_old_file(self, source, first):
        _make_tree(source, ["runC/diag.1.nc"])

        create_group(source, first, existing=first)

        assert _indices(first) == {"runA": 1, "runB": 2, "runC": 3}

    def test_existing_indices_win_over_sort_order(self, source, tmp_path, first):
        """A new dir that sorts first (0new) still gets the next index, not 1."""
        _make_tree(source, ["0new/f.nc"])
        out = tmp_path / "second.json"
        create_group(source, out, existing=first)
        assert _indices(out) == {"runA": 1, "runB": 2, "0new": 3}

    def test_new_index_follows_highest_existing(self, source, tmp_path):
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"legacy": {"group_index": 10, "file_pattern": ["legacy/*"],
                                              "webfile_pattern": ["legacy/*"]}}))
        out = tmp_path / "new.json"
        create_group(source, out, existing=old)
        assert _indices(out) == {"legacy": 10, "runA": 11, "runB": 12}

    def test_groups_missing_from_source_are_kept(self, source, tmp_path, first):
        import shutil
        shutil.rmtree(source / "runB")
        _make_tree(source, ["runC/f.nc"])
        out = tmp_path / "second.json"
        create_group(source, out, existing=first)
        assert _indices(out) == {"runA": 1, "runB": 2, "runC": 3}

    def test_existing_patterns_are_not_replaced(self, source, tmp_path, first):
        """file_pattern already set in the old JSON is kept even with new --include."""
        out = tmp_path / "second.json"
        groups = create_group(source, out, existing=first, include=["diag.*.nc"])
        assert groups["runA"]["file_pattern"] == ["runA/*"]

    def test_missing_existing_json_fails_and_writes_nothing(self, source, tmp_path):
        out = tmp_path / "groups.json"
        with pytest.raises(CreateGroupError, match="not found"):
            create_group(source, out, existing=tmp_path / "nope.json")
        assert not out.exists()


class TestOneGroup:
    def test_whole_source_is_one_group(self, tmp_path):
        src = tmp_path / "src"
        _make_tree(src, ["run/a.nc", "run/b.nc"])
        groups = create_group(src, tmp_path / "groups.json", one_group=True)
        assert _indices(tmp_path / "groups.json") == {"run": 1}
        assert groups["run"]["file_pattern"] == ["run/*"]

    def test_descends_through_single_child_dirs(self, tmp_path):
        src = tmp_path / "src"
        _make_tree(src, ["outer/inner/a.nc"])
        groups = create_group(src, tmp_path / "groups.json", one_group=True)
        assert set(groups) == {"inner"}

    def test_several_subdirs_fail(self, source, tmp_path):
        with pytest.raises(CreateGroupError, match="Multiple subdirectories"):
            create_group(source, tmp_path / "groups.json", one_group=True)

    def test_existing_index_is_kept(self, tmp_path):
        src = tmp_path / "src"
        _make_tree(src, ["run/a.nc"])
        old = tmp_path / "old.json"
        create_group(src, old, one_group=True)
        create_group(src, old, existing=old, one_group=True)
        assert _indices(old) == {"run": 1}

    def test_new_group_follows_highest_existing(self, tmp_path):
        """A one-group source not in the old JSON is numbered after its highest index."""
        src = tmp_path / "src"
        _make_tree(src, ["run/a.nc"])
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"legacy": {"group_index": 4}}))
        out = tmp_path / "new.json"
        create_group(src, out, existing=old, one_group=True)
        assert _indices(out) == {"legacy": 4, "run": 5}


class TestErrors:
    def test_missing_source(self, tmp_path):
        with pytest.raises(CreateGroupError, match="not a directory"):
            create_group(tmp_path / "nope", tmp_path / "groups.json")

    def test_source_is_a_file(self, tmp_path):
        f = tmp_path / "file.nc"
        f.touch()
        with pytest.raises(CreateGroupError, match="not a directory"):
            create_group(f, tmp_path / "groups.json")

    def test_unwritable_output(self, source, tmp_path):
        with pytest.raises(CreateGroupError, match="Could not write"):
            create_group(source, tmp_path / "no_such_dir" / "groups.json")


class TestMain:
    def _run(self, *argv):
        with patch("sys.argv", ["gdexws", "create-group", *argv]):
            cli.main()

    def test_success_writes_output(self, source, tmp_path):
        out = tmp_path / "groups.json"
        self._run("--source", str(source), "--output", str(out), "--include", "diag.*.nc")
        assert _indices(out) == {"runA": 1, "runB": 2}

    @pytest.mark.parametrize("flags", [
        ["--source", "S", "--output", "O", "--existing-json", "E", "--include", "a", "--include", "b", "--one-group"],
        ["-s", "S", "-o", "O", "-e", "E", "--include", "a", "--include", "b", "--one-group"],
    ])
    def test_options_reach_create_group(self, flags):
        with patch.object(cg, "create_group") as mock:
            self._run(*flags)
        mock.assert_called_once_with(Path("S"), Path("O"), existing=Path("E"),
                                     include=["a", "b"], one_group=True, debug=False)

    def test_existing_json_defaults_to_none(self):
        with patch.object(cg, "create_group") as mock:
            self._run("-s", "S", "-o", "O")
        assert mock.call_args.kwargs["existing"] is None
        assert mock.call_args.kwargs["include"] is None

    def test_same_existing_and_output_overwrites(self, source, tmp_path):
        out = tmp_path / "groups.json"
        self._run("-s", str(source), "-o", str(out))
        _make_tree(source, ["runC/f.nc"])
        self._run("-s", str(source), "-o", str(out), "-e", str(out))
        assert _indices(out) == {"runA": 1, "runB": 2, "runC": 3}

    @pytest.mark.parametrize("make_args", [
        lambda src, tmp: ["-s", str(tmp / "nope"), "-o", str(tmp / "o.json")],
        lambda src, tmp: ["-s", str(src), "-o", str(tmp / "o.json"), "-e", str(tmp / "nope.json")],
    ])
    def test_error_exits_1_and_logs(self, source, tmp_path, log, make_args):
        with pytest.raises(SystemExit) as exc:
            self._run(*make_args(source, tmp_path))
        assert exc.value.code == 1
        assert log.call_args.args[1] == "ERROR"

    @pytest.mark.parametrize("argv", [
        ["-o", "O"],                 # --source missing
        ["-s", "S"],                 # --output missing
        ["S", "O"],                  # positional form is rejected
    ])
    def test_bad_arguments_exit_2(self, argv):
        with pytest.raises(SystemExit) as exc:
            self._run(*argv)
        assert exc.value.code == 2
