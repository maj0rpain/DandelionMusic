"""Structural tests over the repository's CI and release pipeline files."""

import yaml

from helpers import REPO_ROOT, dockerfile_commands, shell_commands

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


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
    assert len(commands) >= 5
    for source, command in commands:
        assert "--locked" in command.split(), (source, command)
        assert "--frozen" not in command.split(), (source, command)


def test_checks_workflow_is_callable_and_runs_on_branches_and_prs():
    triggers = _triggers(_load("checks.yml"))
    assert "workflow_call" in triggers
    assert triggers["push"] == {"branches": ["**"]}
    assert "pull_request" in triggers


def _checks_jobs(release):
    return [
        name
        for name, job in release["jobs"].items()
        if job.get("uses", "").startswith("./.github/workflows/checks.yml")
    ]


def test_release_calls_the_checks_and_the_exe_build_needs_them():
    release = _load("release.yml")
    checks = _checks_jobs(release)
    assert checks
    needs = release["jobs"]["build"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert set(checks) <= set(needs)


def test_release_permissions_are_narrow():
    release = _load("release.yml")
    assert release["permissions"] == {"contents": "read"}
    assert release["jobs"]["build"]["permissions"] == {"contents": "write"}
    for name, job in release["jobs"].items():
        if name != "build":
            assert job.get("permissions", {"contents": "read"}) == {
                "contents": "read"
            }, name


def test_docker_publish_runs_only_on_a_push_to_master():
    job = _load("checks.yml")["jobs"]["docker-publish"]
    assert job["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/master'"
    )
