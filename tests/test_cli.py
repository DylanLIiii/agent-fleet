import json

from typer.testing import CliRunner

from agent_fleet.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "control room" in result.output
    assert "credential" in result.output


def test_empty_json_status(store):
    result = runner.invoke(app, ["--home", str(store.home), "status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []
    assert not store.home.exists()


def test_cli_preview_setup(store, source):
    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "--dry-run",
            "setup",
            "--provider",
            "amp",
            "--name",
            "sample",
            "--source",
            str(source),
            "--backend",
            "process",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "[preview]" in result.output
    assert not store.home.exists()


def test_cli_configure_and_list(store, source):
    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "setup",
            "--provider",
            "amp",
            "--source",
            str(source),
            "--count",
            "2",
            "--backend",
            "process",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["--home", str(store.home), "status", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 2
    assert data[0]["state"] == "stopped"


def test_cli_manages_amp_directories(store, source, tmp_path):
    from agent_fleet.store import Store

    extra = tmp_path / "extra"
    extra.mkdir()
    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "setup",
            "--provider",
            "amp",
            "--name",
            "dirs",
            "--source",
            str(source),
            "--count",
            "1",
            "--backend",
            "process",
            "--extra-dir",
            str(extra),
        ],
    )
    assert result.exit_code == 0, result.output
    assert Store(store.home).load("dirs").extra_dirs == (str(extra.resolve()),)
    result = runner.invoke(app, ["--home", str(store.home), "--instance", "dirs", "dirs"])
    assert result.exit_code == 0, result.output
    assert "extra" in result.output
    root = tmp_path / "code"
    root.mkdir()
    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "--instance",
            "dirs",
            "dirs",
            "discover",
            str(root),
            "--depth",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = Store(store.home).load("dirs")
    assert saved.discover_dirs == (str(root.resolve()),) and saved.discover_depth == 3
    result = runner.invoke(
        app,
        ["--home", str(store.home), "--instance", "dirs", "dirs", "remove", str(extra)],
    )
    assert result.exit_code == 0, result.output
    assert Store(store.home).load("dirs").extra_dirs == ()


def test_cli_manages_devin_project_directories(store, tmp_path):
    from agent_fleet.store import Store

    projects = [tmp_path / "Jev-seen", tmp_path / "MiniCPM-V", tmp_path / "Vvbot"]
    for project in projects:
        project.mkdir()

    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "setup",
            "--provider",
            "devin",
            "--name",
            "devin-lab",
            "--outpost",
            "shared-outpost",
            "--backend",
            "process",
            "--project-dir",
            str(projects[0]),
            "--project-dir",
            str(projects[1]),
            "--project-dir",
            str(projects[2]),
        ],
        input="\n",
    )
    assert result.exit_code == 0, result.output
    assert "Link existing project" in result.output
    assert Store(store.home).load("devin-lab").project_dirs == tuple(
        str(project.resolve()) for project in projects
    )

    result = runner.invoke(
        app,
        ["--home", str(store.home), "--instance", "devin-lab", "dirs"],
    )
    assert result.exit_code == 0, result.output
    assert "project" in result.output
    assert "Path" in result.output

    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "--instance",
            "devin-lab",
            "dirs",
            "remove",
            str(projects[1]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert Store(store.home).load("devin-lab").project_dirs == (
        str(projects[0].resolve()),
        str(projects[2].resolve()),
    )
    assert projects[1].is_dir()


def test_cli_devin_requires_project_directories(store):
    result = runner.invoke(
        app,
        [
            "--home",
            str(store.home),
            "setup",
            "--provider",
            "devin",
            "--name",
            "devin-empty",
            "--outpost",
            "shared-outpost",
        ],
    )
    assert result.exit_code != 0
    assert "--project-dir" in str(result.exception)
    assert not store.home.exists()
