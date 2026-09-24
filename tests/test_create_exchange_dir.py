import os
import pwd
from unittest.mock import patch

import pytest

from gdexws import cli
from gdexws.tools import create_exchange_dir as ced
from gdexws.tools.create_exchange_dir import ExchangeDirError, create_exchange_dir

USER = "alice"


@pytest.fixture
def exchange(tmp_path):
    """Return a temporary directory standing in for the exchange root."""
    root = tmp_path / "exchange"
    root.mkdir()
    return str(root)


@pytest.fixture(autouse=True)
def fake_user():
    """Make every test see USER as an existing, non-root account."""
    entry = pwd.struct_passwd(("alice", "x", 1001, 1001, "", "/home/alice", "/bin/bash"))
    with patch.object(ced.pwd, "getpwnam", side_effect=lambda n: entry if n == USER else (_ for _ in ()).throw(KeyError(n))):
        yield


def _acl_ok(args):
    """Fake _run_acl_tool: setfacl succeeds, getfacl echoes the expected entries."""
    if args[0] == "getfacl":
        return f"user:{USER}:rwx\t#effective:rwx\ndefault:user:{USER}:rwx\n"
    return ""


class TestValidateDirname:
    @pytest.mark.parametrize("name", [
        "", ".", "..", "../x", "a/b", "/abs", ".hidden", "-rf", "a b", "a\x00b",
        "a;b", "$(x)", "x" * 256, "a\\b", "~", "a\n",
    ])
    def test_rejects_bad_names(self, name):
        with pytest.raises(ExchangeDirError):
            ced.validate_dirname(name)

    @pytest.mark.parametrize("name", ["data", "proj-1", "run_2.5", "A1"])
    def test_accepts_good_names(self, name):
        assert ced.validate_dirname(name) == name


class TestValidateUsername:
    @pytest.mark.parametrize("name", ["", "Alice", "a b", "a/b", "-x", "x" * 33, "a;b"])
    def test_rejects_malformed(self, name):
        with pytest.raises(ExchangeDirError):
            ced.validate_username(name)

    def test_unknown_user(self):
        with pytest.raises(ExchangeDirError, match="does not exist"):
            ced.validate_username("nobodyhere")

    def test_root_rejected(self):
        root = pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/sh"))
        with patch.object(ced.pwd, "getpwnam", return_value=root):
            with pytest.raises(ExchangeDirError, match="uid 0"):
                ced.validate_username("root")

    def test_valid_user(self):
        assert ced.validate_username(USER) == USER


class TestCreate:
    def test_creates_dir_and_calls_setfacl(self, exchange):
        with patch.object(ced, "_run_acl_tool", side_effect=_acl_ok) as run:
            path = create_exchange_dir(USER, "proj", exchange_dir=exchange)
        assert path == os.path.join(os.path.realpath(exchange), "proj")
        assert os.path.isdir(path)
        setfacl = run.call_args_list[0].args[0]
        assert setfacl[:2] == ["setfacl", "-P"]
        assert f"u:{USER}:rwx,d:u:{USER}:rwx" in setfacl
        assert setfacl[-2:] == ["--", path]

    def test_existing_dir_fails_and_is_untouched(self, exchange):
        os.mkdir(os.path.join(exchange, "proj"))
        with patch.object(ced, "_run_acl_tool") as run:
            with pytest.raises(ExchangeDirError, match="already exists"):
                create_exchange_dir(USER, "proj", exchange_dir=exchange)
        run.assert_not_called()

    def test_existing_symlink_fails(self, exchange, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, os.path.join(exchange, "link"))
        with patch.object(ced, "_run_acl_tool") as run:
            with pytest.raises(ExchangeDirError, match="already exists"):
                create_exchange_dir(USER, "link", exchange_dir=exchange)
        run.assert_not_called()

    @pytest.mark.parametrize("name", ["..", "../escape", "/etc", "a/b"])
    def test_traversal_creates_nothing(self, exchange, tmp_path, name):
        with patch.object(ced, "_run_acl_tool") as run:
            with pytest.raises(ExchangeDirError):
                create_exchange_dir(USER, name, exchange_dir=exchange)
        run.assert_not_called()
        assert os.listdir(exchange) == []
        assert not (tmp_path / "escape").exists()

    def test_unknown_user_creates_nothing(self, exchange):
        with pytest.raises(ExchangeDirError):
            create_exchange_dir("ghost", "proj", exchange_dir=exchange)
        assert os.listdir(exchange) == []

    def test_missing_exchange_dir(self, tmp_path):
        with pytest.raises(ExchangeDirError, match="does not exist"):
            create_exchange_dir(USER, "proj", exchange_dir=str(tmp_path / "nope"))

    def test_acl_failure_rolls_back_directory(self, exchange):
        with patch.object(ced, "_run_acl_tool", side_effect=ExchangeDirError("setfacl failed")):
            with pytest.raises(ExchangeDirError, match="setfacl failed"):
                create_exchange_dir(USER, "proj", exchange_dir=exchange)
        assert os.listdir(exchange) == []

    def test_acl_verification_failure_rolls_back(self, exchange):
        with patch.object(ced, "_run_acl_tool", return_value=""):
            with pytest.raises(ExchangeDirError, match="verification failed"):
                create_exchange_dir(USER, "proj", exchange_dir=exchange)
        assert os.listdir(exchange) == []


class TestMain:
    def test_error_exits_nonzero(self, exchange):
        with patch("sys.argv", ["gdexws", "create-exchange-dir", USER, ".."]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1
