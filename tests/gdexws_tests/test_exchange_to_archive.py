import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gdexws import cli
from gdexws.tools import exchange_to_archive as eta
from gdexws.tools.exchange_to_archive import ExchangeToArchiveError, exchange_to_archive

DSID = "d430681"


@pytest.fixture(autouse=True)
def log():
    """Capture service_log calls instead of writing the jsonl log."""
    with patch.object(eta, "service_log") as mock:
        yield mock


@pytest.fixture
def roots(tmp_path):
    """Temporary exchange (allowed source) and /gdex/data stand-ins."""
    exchange = tmp_path / "exchange"
    archive = tmp_path / "gdexdata"
    exchange.mkdir()
    archive.mkdir()
    with patch.object(eta, "ALLOWED_SOURCE_ROOTS", (exchange,)), \
            patch.object(eta, "GLADE_DIR", archive):
        yield exchange, archive


class FakeGdexcp:
    """Stands in for subprocess.run: records commands and the -i list contents."""

    def __init__(self, fail_on=None):
        self.commands = []
        self.lists = {}
        self.fail_on = fail_on

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if "-i" in command:
            listing = Path(command[command.index("-i") + 1])
            self.lists[command[command.index("-t") + 1]] = listing.read_text().split()
            assert oct(listing.stat().st_mode & 0o777) == "0o644"  # gdexdata must read it
        if self.fail_on and self.fail_on in " ".join(command):
            return subprocess.CompletedProcess(command, 1, "", "boom")
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture
def gdexcp():
    fake = FakeGdexcp()
    with patch.object(eta.shutil, "which", return_value="/bin/gdexcp"), \
            patch.object(eta.subprocess, "run", side_effect=fake):
        yield fake


def _setup(exchange: Path, tmp_path: Path, groups=None) -> Path:
    """Create exchange/web/{runA,runB} files and a group JSON for them."""
    src = exchange / "web"
    for rel in ["runA/diag.1.nc", "runA/diag.2.nc", "runA/hist.1.nc", "runB/diag.1.nc"]:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).touch()
    if groups is None:
        groups = {
            "runA": {"web_path": "runA", "file_pattern": ["runA/diag.*.nc"], "scan_root_path": str(src)},
            "runB": {"web_path": "runB", "file_pattern": ["runB/*"], "scan_root_path": str(src)},
        }
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(groups))
    return path


class TestGlade:
    def test_one_list_per_group_into_web_path(self, roots, tmp_path, gdexcp):
        exchange, archive = roots
        copied = exchange_to_archive(_setup(exchange, tmp_path), DSID, "glade")
        src = exchange / "web"
        dest = archive / DSID
        assert len(gdexcp.commands) == 2  # one gdexcp call per group
        assert gdexcp.lists == {
            f"{dest / 'runA'}/": [str(src / "runA/diag.1.nc"), str(src / "runA/diag.2.nc")],
            f"{dest / 'runB'}/": [str(src / "runB/diag.1.nc")],
        }
        assert gdexcp.commands[0][3:] == ["-t", f"{dest / 'runA'}/", "-m", "16"]
        assert copied == [str(dest / "runA/diag.1.nc"), str(dest / "runA/diag.2.nc"),
                          str(dest / "runB/diag.1.nc")]

    def test_fixed_subfolder_is_kept(self, roots, tmp_path, gdexcp):
        """runA/out/diag.*.nc (no wildcard folder) goes to <web_path>/out/, not <web_path>/."""
        exchange, archive = roots
        src = exchange / "web"
        (src / "runA/out").mkdir(parents=True)
        (src / "runA/out/diag.1.nc").touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/out/diag.*.nc"],
                           "scan_root_path": str(src)}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        dest = archive / DSID / "runA"
        assert gdexcp.lists == {f"{dest / 'out'}/": [str(src / "runA/out/diag.1.nc")]}
        assert copied == [str(dest / "out/diag.1.nc")]

    def test_same_name_in_different_folders_both_copied(self, roots, tmp_path, gdexcp):
        """runA/diag.1.nc and runA/out/diag.1.nc keep their folders, so nothing collides."""
        exchange, archive = roots
        src = exchange / "web"
        (src / "runA/out").mkdir(parents=True)
        (src / "runA/out/diag.1.nc").touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/diag.*.nc", "runA/out/*"],
                           "scan_root_path": str(src)}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        dest = archive / DSID / "runA"
        assert str(dest / "diag.1.nc") in copied
        assert str(dest / "out/diag.1.nc") in copied

    def test_file_outside_web_path_fails_group(self, roots, tmp_path, gdexcp):
        """A pattern reaching outside <scan_root>/<web_path> fails that group only."""
        exchange, archive = roots
        src = str(exchange / "web")
        groups = {
            "runA": {"web_path": "runA", "file_pattern": ["runB/*"], "scan_root_path": src},
            "runB": {"web_path": "runB", "file_pattern": ["runB/*"], "scan_root_path": src},
        }
        with pytest.raises(ExchangeToArchiveError, match="1 item"):
            exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        assert list(gdexcp.lists) == [f"{archive / DSID / 'runB'}/"]  # runB still copied

    def test_file_matched_twice_is_copied_once(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        src = str(exchange / "web")
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/*.nc", "runA/diag.*"],
                           "scan_root_path": src}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        assert len(copied) == 3

    def test_symlink_skipped(self, roots, tmp_path, gdexcp, log):
        exchange, _ = roots
        group_json = _setup(exchange, tmp_path)
        (exchange / "web/runB/link.nc").symlink_to(tmp_path / "groups.json")
        copied = exchange_to_archive(group_json, DSID, "glade")
        assert not any("link.nc" in c for c in copied)
        assert any(c.args[2] == "Skipping symlink" for c in log.call_args_list)


class TestObject:
    def test_one_list_per_group(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        copied = exchange_to_archive(_setup(exchange, tmp_path), DSID, "object", bucket="b")
        src = exchange / "web"
        assert gdexcp.lists == {
            f"{DSID}/runA/": [str(src / "runA/diag.1.nc"), str(src / "runA/diag.2.nc")],
            f"{DSID}/runB/": [str(src / "runB/diag.1.nc")],
        }
        assert len(gdexcp.commands) == 2
        assert gdexcp.commands[0][3:] == ["-tb", "b", "-t", f"{DSID}/runA/", "-m", "16"]
        assert copied[0] == f"b/{DSID}/runA/diag.1.nc"

    def test_list_file_is_deleted(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        exchange_to_archive(_setup(exchange, tmp_path), DSID, "object")
        listing = gdexcp.commands[0][gdexcp.commands[0].index("-i") + 1]
        assert not Path(listing).exists()


class TestSkipsAndFailures:
    def test_group_without_pattern_is_skipped(self, roots, tmp_path, gdexcp, log):
        exchange, _ = roots
        src = str(exchange / "web")
        groups = {"runA": {"web_path": "runA", "file_pattern": None, "scan_root_path": src},
                  "runB": {"web_path": "runB", "file_pattern": ["runB/*"], "scan_root_path": src}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        assert len(copied) == 1

    def test_failure_continues_then_raises(self, roots, tmp_path):
        exchange, _ = roots
        fake = FakeGdexcp(fail_on=f"{DSID}/runA/")
        with patch.object(eta.shutil, "which", return_value="/bin/gdexcp"), \
                patch.object(eta.subprocess, "run", side_effect=fake):
            with pytest.raises(ExchangeToArchiveError, match="2 item"):  # runA's 2 files
                exchange_to_archive(_setup(exchange, tmp_path), DSID, "glade")
        assert len(fake.commands) == 2  # runB was still attempted

    def test_nothing_matched_fails(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/*.zarr"],
                           "scan_root_path": str(exchange / "web")}}
        with pytest.raises(ExchangeToArchiveError, match="Nothing to copy"):
            exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")

    def test_dry_run_runs_nothing(self, roots, tmp_path):
        exchange, _ = roots
        with patch.object(eta.subprocess, "run") as run, patch.object(eta.shutil, "which") as which:
            copied = exchange_to_archive(_setup(exchange, tmp_path), DSID, "glade", dry_run=True)
        run.assert_not_called()
        which.assert_not_called()
        assert len(copied) == 3

    def test_gdexcp_missing(self, roots, tmp_path):
        exchange, _ = roots
        with patch.object(eta.shutil, "which", return_value=None):
            with pytest.raises(ExchangeToArchiveError, match="not found on PATH"):
                exchange_to_archive(_setup(exchange, tmp_path), DSID, "glade")


class TestValidation:
    @pytest.mark.parametrize("dsid", ["430681", "d43068", "d4306811", "../d430681", "D430681"])
    def test_bad_dataset_id(self, dsid, tmp_path):
        with pytest.raises(ExchangeToArchiveError, match="Dataset ID"):
            exchange_to_archive(tmp_path / "g.json", dsid, "glade")

    def test_bad_json(self, tmp_path):
        bad = tmp_path / "g.json"
        bad.write_text("{not json")
        with pytest.raises(ExchangeToArchiveError, match="Could not read"):
            exchange_to_archive(bad, DSID, "glade")

    def test_source_outside_exchange_rejected(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        outside = tmp_path / "outside"
        (outside / "runA").mkdir(parents=True)
        (outside / "runA/f.nc").touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/*"], "scan_root_path": str(outside)}}
        with pytest.raises(ExchangeToArchiveError, match="1 item"):
            exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        assert gdexcp.commands == []

    @pytest.mark.parametrize("pattern", ["../../etc/*", "/etc/*"])
    def test_patterns_escaping_source_rejected(self, roots, tmp_path, gdexcp, pattern):
        exchange, _ = roots
        group = {"web_path": "runA", "file_pattern": [pattern], "scan_root_path": str(exchange / "web")}
        with pytest.raises(ExchangeToArchiveError, match="1 item"):
            exchange_to_archive(_setup(exchange, tmp_path, {"runA": group}), DSID, "glade")
        assert gdexcp.commands == []


class TestSubfolders:
    def test_unique_destinations_check(self):
        """Last-line check before copying: two sources for one destination is an error."""
        pairs = [(Path("/x/runA/a.nc"), "a.nc"), (Path("/y/runA/a.nc"), "a.nc"),
                 (Path("/x/runA/b.nc"), "b.nc")]
        with pytest.raises(ExchangeToArchiveError, match="overwrite each other"):
            eta._check_unique_destinations("runA", pairs)
        eta._check_unique_destinations("runA", pairs[1:])  # no clash: no error

    def test_wildcard_folders_are_kept(self, roots, tmp_path, gdexcp):
        """runA/*/diag.*.nc: same file name in out1 and out2 lands in separate subfolders."""
        exchange, archive = roots
        src = exchange / "web"
        for rel in ["runA/out1/diag.1.nc", "runA/out2/diag.1.nc"]:
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/*/diag.*.nc"],
                           "scan_root_path": str(src)}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        dest = archive / DSID / "runA"
        assert gdexcp.lists == {
            f"{dest / 'out1'}/": [str(src / "runA/out1/diag.1.nc")],
            f"{dest / 'out2'}/": [str(src / "runA/out2/diag.1.nc")],
        }
        assert copied == [str(dest / "out1/diag.1.nc"), str(dest / "out2/diag.1.nc")]

    def test_wildcard_folders_object(self, roots, tmp_path, gdexcp):
        exchange, _ = roots
        src = exchange / "web"
        (src / "runA/out1").mkdir(parents=True)
        (src / "runA/out1/diag.1.nc").touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/*/diag.*.nc"],
                           "scan_root_path": str(src)}}
        copied = exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "object", bucket="b")
        assert list(gdexcp.lists) == [f"{DSID}/runA/out1/"]
        assert copied == [f"b/{DSID}/runA/out1/diag.1.nc"]

    def test_fixed_and_wildcard_patterns_together(self, roots, tmp_path, gdexcp):
        """Files directly in the group folder and in a subfolder get one gdexcp call each."""
        exchange, archive = roots
        src = exchange / "web"
        (src / "runA/out1").mkdir(parents=True)
        (src / "runA/out1/diag.1.nc").touch()
        groups = {"runA": {"web_path": "runA", "file_pattern": ["runA/hist.*.nc", "runA/*/diag.*.nc"],
                           "scan_root_path": str(src)}}
        exchange_to_archive(_setup(exchange, tmp_path, groups), DSID, "glade")
        dest = archive / DSID / "runA"
        assert gdexcp.lists == {
            f"{dest}/": [str(src / "runA/hist.1.nc")],
            f"{dest / 'out1'}/": [str(src / "runA/out1/diag.1.nc")],
        }


class TestWebPath:
    @pytest.mark.parametrize("web_path", [None, "", ".", "../..", "/etc", "runA/../../x"])
    def test_bad_web_path_rejected(self, roots, tmp_path, gdexcp, web_path):
        exchange, _ = roots
        group = {"web_path": web_path, "file_pattern": ["runA/*"], "scan_root_path": str(exchange / "web")}
        with pytest.raises(ExchangeToArchiveError, match="1 item"):
            exchange_to_archive(_setup(exchange, tmp_path, {"runA": group}), DSID, "glade")
        assert gdexcp.commands == []

    def test_nested_web_path(self, roots, tmp_path, gdexcp):
        exchange, archive = roots
        src = exchange / "web"
        (src / "model/runA").mkdir(parents=True)
        (src / "model/runA/f.nc").touch()
        group = {"web_path": "model/runA", "file_pattern": ["model/runA/*"], "scan_root_path": str(src)}
        copied = exchange_to_archive(_setup(exchange, tmp_path, {"runA": group}), DSID, "glade")
        assert copied == [str(archive / DSID / "model/runA/f.nc")]


class TestMain:
    def _run(self, *argv):
        with patch("sys.argv", ["gdexws", "exchange-to-archive", *argv]):
            cli.main()

    @pytest.mark.parametrize("flags", [
        ["--group-json", "G", "--dataset-id", DSID, "--storage", "object", "--bucket", "b", "--dry-run"],
        ["-g", "G", "-i", DSID, "--storage", "object", "--bucket", "b", "--dry-run"],
    ])
    def test_options_reach_function(self, flags):
        with patch.object(eta, "exchange_to_archive") as mock:
            self._run(*flags)
        mock.assert_called_once_with(Path("G"), DSID, "object", bucket="b",
                                     dry_run=True, debug=False)

    def test_whole_folder_option_is_gone(self):
        with pytest.raises(SystemExit) as exc:
            self._run("-g", "G", "-i", DSID, "--storage", "glade", "--whole-folder")
        assert exc.value.code == 2

    def test_error_exits_1(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            self._run("-g", str(tmp_path / "nope.json"), "-i", DSID, "--storage", "glade")
        assert exc.value.code == 1

    @pytest.mark.parametrize("argv", [
        ["-i", DSID, "--storage", "glade"],              # --group-json missing
        ["-g", "G", "--storage", "glade"],               # --dataset-id missing
        ["-g", "G", "-i", DSID],                         # --storage missing
        ["-g", "G", "-i", DSID, "--storage", "s3"],      # not a choice
    ])
    def test_bad_arguments_exit_2(self, argv):
        with pytest.raises(SystemExit) as exc:
            self._run(*argv)
        assert exc.value.code == 2
