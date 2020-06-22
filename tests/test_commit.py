import json
import os
import re
import shlex
import subprocess
import time

import pytest

import pygit2

from sno.repo_files import fallback_editor
from sno.structure import RepositoryStructure
from sno.working_copy import WorkingCopy

from sno.exceptions import INVALID_ARGUMENT, NO_CHANGES, NO_DATA, NO_REPOSITORY


H = pytest.helpers.helpers()


def edit_points(dbcur):
    dbcur.execute(H.POINTS.INSERT, H.POINTS.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POINTS.LAYER} SET fid=9998 WHERE fid=1;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POINTS.LAYER} SET name='test' WHERE fid=2;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"DELETE FROM {H.POINTS.LAYER} WHERE fid IN (3,30,31,32,33);")
    assert dbcur.getconnection().changes() == 5
    pk_del = 3
    return pk_del


def edit_polygons_pk(dbcur):
    dbcur.execute(H.POLYGONS.INSERT, H.POLYGONS.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.POLYGONS.LAYER} SET id=9998 WHERE id=1424927;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(
        f"UPDATE {H.POLYGONS.LAYER} SET survey_reference='test' WHERE id=1443053;"
    )
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(
        f"DELETE FROM {H.POLYGONS.LAYER} WHERE id IN (1452332, 1456853, 1456912, 1457297, 1457355);"
    )
    assert dbcur.getconnection().changes() == 5
    pk_del = 1452332
    return pk_del


def edit_table(dbcur):
    dbcur.execute(H.TABLE.INSERT, H.TABLE.RECORD)
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.TABLE.LAYER} SET OBJECTID=9998 WHERE OBJECTID=1;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"UPDATE {H.TABLE.LAYER} SET name='test' WHERE OBJECTID=2;")
    assert dbcur.getconnection().changes() == 1
    dbcur.execute(f"DELETE FROM {H.TABLE.LAYER} WHERE OBJECTID IN (3,30,31,32,33);")
    assert dbcur.getconnection().changes() == 5
    pk_del = 3
    return pk_del


def _count_tracking_table_changes(db, working_copy, layer):
    with db:
        cur = db.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM {working_copy.TRACKING_TABLE} WHERE table_name=?;",
            [layer],
        )
        change_count = cur.fetchone()[0]
    return change_count


@pytest.mark.parametrize(
    "partial", [pytest.param(False, id=""), pytest.param(True, id="partial")],
)
@pytest.mark.parametrize(
    "archive,layer",
    [
        pytest.param("points", H.POINTS.LAYER, id="points"),
        pytest.param("polygons", H.POLYGONS.LAYER, id="polygons_pk"),
        pytest.param("table", H.TABLE.LAYER, id="table"),
    ],
)
def test_commit(
    archive, layer, partial, data_working_copy, geopackage, cli_runner, request
):
    """ commit outstanding changes from the working copy """
    param_ids = H.parameter_ids(request)

    with data_working_copy(archive) as (repo_dir, wc_path):
        # empty
        r = cli_runner.invoke(["commit", "-m", "test-commit-empty"])
        assert r.exit_code == NO_CHANGES, r
        assert r.stderr.splitlines() == ["Error: No changes to commit"]

        # empty
        r = cli_runner.invoke(["commit", "-m", "test-commit-empty", "--allow-empty"])
        assert r.exit_code == 0, r

        # make some changes
        db = geopackage(wc_path)
        with db:
            cur = db.cursor()
            try:
                edit_func = globals()[f"edit_{param_ids[0]}"]
                pk_del = edit_func(cur)
            except KeyError:
                raise NotImplementedError(f"layer={layer}")

        print(f"deleted fid={pk_del}")

        repo = pygit2.Repository(str(repo_dir))
        rs = RepositoryStructure(repo)
        wc = rs.working_copy
        original_change_count = _count_tracking_table_changes(db, wc, layer)

        if partial:
            r = cli_runner.invoke(
                ["commit", "-m", "test-commit-1", "-o", "json", f"{layer}:{pk_del}"]
            )
        else:
            r = cli_runner.invoke(["commit", "-m", "test-commit-1", "-o", "json"])

        assert r.exit_code == 0, r
        commit_id = json.loads(r.stdout)["sno.commit/v1"]["commit"]
        print("commit:", commit_id)

        assert str(repo.head.target) == commit_id
        commit = repo.head.peel(pygit2.Commit)
        assert commit.message == "test-commit-1"
        assert time.time() - commit.commit_time < 3

        dataset = rs[layer]
        tree = repo.head.peel(pygit2.Tree)
        assert dataset.get_feature_path(pk_del) not in tree

        wc = WorkingCopy.open(repo)
        wc.assert_db_tree_match(tree)
        change_count = _count_tracking_table_changes(db, wc, layer)

        if partial:
            # All but one change should still be in the tracking table
            assert change_count == original_change_count - 1

            # Changes should still be visible in the working copy:
            r = cli_runner.invoke(["diff", "--exit-code"])
            assert r.exit_code == 1, r
            assert r.stdout != ""

        else:
            assert (
                change_count == 0
            ), f"Changes still listed in {wc.TRACKING_TABLE} after full commit"

            r = cli_runner.invoke(["diff", "--exit-code"])
            assert r.exit_code == 0, r
            assert r.stdout == ""


def test_tag(data_working_copy, cli_runner):
    """ review commit history """
    with data_working_copy("points") as (repo_dir, wc):
        # create a tag
        r = cli_runner.invoke(["tag", "version1"])
        assert r.exit_code == 0, r

        repo = pygit2.Repository(str(repo_dir))
        assert "refs/tags/version1" in repo.references
        ref = repo.lookup_reference_dwim("version1")
        assert ref.target.hex == H.POINTS.HEAD_SHA


def test_commit_message(
    data_working_copy, cli_runner, monkeypatch, geopackage, tmp_path
):
    """ commit message handling """
    editor_in = None
    editor_out = None
    editor_cmd = None

    def monkey_editor(cmdline, **kwargs):
        nonlocal editor_cmd, editor_in
        editor_cmd = cmdline
        print("EDITOR", cmdline)
        editmsg_file = shlex.split(cmdline)[-1]
        with open(editmsg_file, "r+", encoding="utf-8") as ef:
            editor_in = ef.read()
            if editor_out:
                ef.seek(0)
                ef.truncate()
                ef.write(editor_out)
                return 0
            else:
                assert False, "Didn't expect editor to launch"

    monkeypatch.setattr(subprocess, "check_call", monkey_editor)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("GIT_EDITOR", raising=False)

    with data_working_copy("points") as (repo_dir, wc_path):
        repo = pygit2.Repository(str(repo_dir))

        def last_message():
            return repo.head.peel(pygit2.Commit).message

        # normal
        r = cli_runner.invoke(
            ["commit", "--allow-empty", "-m", "the messagen\n\n\n\n\n"]
        )
        assert r.exit_code == 0, r
        assert last_message() == "the messagen"

        # E: empty
        r = cli_runner.invoke(["commit", "--allow-empty", "-m", ""])
        assert r.exit_code == INVALID_ARGUMENT, r

        # file
        f_commit_message = str(tmp_path / "commit-message.txt")
        with open(f_commit_message, mode="w", encoding="utf8") as f:
            f.write("\ni am a message\n\n\n")
            f.flush()

        r = cli_runner.invoke(["commit", "--allow-empty", "-F", f_commit_message])
        assert r.exit_code == 0, r
        assert last_message() == "i am a message"

        # E: conflict
        r = cli_runner.invoke(
            ["commit", "--allow-empty", "-F", f_commit_message, "-m", "foo"]
        )
        assert r.exit_code == INVALID_ARGUMENT, r
        assert "exclusive" in r.stderr

        # multiple
        r = cli_runner.invoke(
            [
                "commit",
                "--allow-empty",
                "-m",
                "one",
                "-m",
                "two\nthree\n",
                "-m",
                "four\n\n",
            ]
        )
        assert r.exit_code == 0, r
        assert last_message() == "one\n\ntwo\nthree\n\nfour"

        # default editor

        # make some changes
        db = geopackage(wc_path)
        with db:
            cur = db.cursor()
            edit_points(cur)

        editor_out = "I am a message\n#of hope, and\nof warning\n\t\n"
        r = cli_runner.invoke(["commit"])
        assert r.exit_code == 0, r
        editmsg_path = f"{repo_dir}{os.sep}COMMIT_EDITMSG"
        assert re.match(
            rf'{fallback_editor()} "?{re.escape(editmsg_path)}"?$', editor_cmd
        )
        assert editor_in == (
            "\n"
            "# Please enter the commit message for your changes. Lines starting\n"
            "# with '#' will be ignored, and an empty message aborts the commit.\n"
            "#\n"
            "# On branch master\n"
            "#\n"
            "# Changes to be committed:\n"
            "#\n"
            "#   nz_pa_points_topo_150k/\n"
            "#     modified:  2 features\n"
            "#     new:       1 feature\n"
            "#     deleted:   5 features\n"
            "#\n"
        )
        print(last_message())
        assert last_message() == "I am a message\nof warning"

        monkeypatch.setenv("EDITOR", "/path/to/some/editor -abc")
        editor_out = "sqwark 🐧\n"
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == 0, r
        editmsg_path = f"{repo_dir}{os.sep}COMMIT_EDITMSG"
        assert re.match(
            rf'/path/to/some/editor -abc "?{re.escape(editmsg_path)}"?$', editor_cmd
        )
        assert editor_in == (
            "\n"
            "# Please enter the commit message for your changes. Lines starting\n"
            "# with '#' will be ignored, and an empty message aborts the commit.\n"
            "#\n"
            "# On branch master\n"
            "#\n"
            "# Changes to be committed:\n"
            "#\n"
            "#   No changes (empty commit)\n"
            "#\n"
        )
        print(last_message())
        assert last_message() == "sqwark 🐧"


def test_empty(tmp_path, cli_runner, chdir):
    repo_path = tmp_path / "one.sno"

    # empty repo
    r = cli_runner.invoke(["init", str(repo_path)])
    assert r.exit_code == 0, r
    with chdir(repo_path):
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == NO_DATA, r
        assert "Empty repository" in r.stderr

    # empty dir
    empty_path = tmp_path / "two"
    empty_path.mkdir()
    with chdir(empty_path):
        r = cli_runner.invoke(["commit", "--allow-empty"])
        assert r.exit_code == NO_REPOSITORY, r
        assert "not an existing repository" in r.stderr
