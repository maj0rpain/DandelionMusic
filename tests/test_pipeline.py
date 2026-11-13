"""Structural tests over the repository's CI and release pipeline files."""

import yaml

from helpers import REPO_ROOT, dockerfile_commands, shell_commands

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _dependabot():
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text()
    )


def _triggers(workflow):
    # PyYAML parses the bare key `on` as boolean True.
    return workflow[True] if True in workflow else workflow["on"]


def _uv_commands():
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job in _load(path.name)["jobs"].values():
            for step in job.get("steps", []):
                for c in shell_commands(step.get("run", "")):
                    found.append((path.name, c))
    for line in dockerfile_commands():
        if line.startswith("RUN "):
            for c in shell_commands(line[4:]):
                found.append(("Dockerfile", c))
    return [
        (f, c)
        for f, c in found
        if c.startswith("uv run") or c.startswith("uv sync")
    ]


def test_every_uv_command_is_locked_and_none_frozen():
    commands = _uv_commands()
    assert len(commands) >= 3
    for source, command in commands:
        assert "--locked" in command.split(), (source, command)
        assert "--frozen" not in command.split(), (source, command)


def test_checks_workflow_runs_on_branches_and_prs():
    triggers = _triggers(_load("checks.yml"))
    assert triggers["push"] == {"branches": ["**"]}
    assert "pull_request" in triggers


def test_dependabot_config_covers_every_ecosystem_weekly_in_one_group():
    config = _dependabot()
    entries = config["updates"]
    ecosystems = {entry["package-ecosystem"] for entry in entries}
    assert ecosystems == {
        "uv",
        "pre-commit",
        "docker",
        "docker-compose",
        "github-actions",
    }
    for entry in entries:
        assert (REPO_ROOT / entry["directory"].lstrip("/")).is_dir()
        assert entry["schedule"]["interval"] == "weekly"
        groups = list(entry["groups"].values())
        assert len(groups) == 1
        assert sorted(groups[0]["update-types"]) == ["minor", "patch"]


def test_dependabot_ignores_python_minor_and_major_for_docker():
    config = _dependabot()
    by_ecosystem = {e["package-ecosystem"]: e for e in config["updates"]}
    assert by_ecosystem["docker"]["ignore"] == [
        {
            "dependency-name": "python",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
            ],
        }
    ]
    others = [
        e
        for e in config["updates"]
        if e["package-ecosystem"] not in ("docker", "docker-compose", "uv")
    ]
    assert [e for e in others if "ignore" in e] == []


def test_dependabot_compose_entry_ignores_only_the_bgutil_provider_image():
    config = _dependabot()
    (entry,) = [
        e
        for e in config["updates"]
        if e["package-ecosystem"] == "docker-compose"
    ]
    assert entry["ignore"] == [
        {"dependency-name": "brainicism/bgutil-ytdlp-pot-provider"}
    ]


def test_dependabot_uv_entry_ignores_only_davey_0_2():
    config = _dependabot()
    (entry,) = [e for e in config["updates"] if e["package-ecosystem"] == "uv"]
    assert entry["ignore"] == [
        {"dependency-name": "davey", "versions": [">=0.2.0"]}
    ]
