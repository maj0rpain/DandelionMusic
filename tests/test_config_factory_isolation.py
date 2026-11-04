"""config_factory restores every variable its .env wrote."""

from pathlib import Path

CONFTEST = Path(__file__).with_name("conftest.py")

INNER_TESTS = """
import os


def test_builds_a_config_from_a_dotenv_with_an_unknown_key(config_factory):
    config_factory(
        "BOT_TOKEN=leaky-token\\nMY_TYPO=1\\n"
        "LASTFM_API_KEY=k\\nGUILD_WHITELIST=[111]\\n"
    )


def test_runs_after_it_and_sees_neither_key():
    assert "BOT_TOKEN" not in os.environ
    assert "MY_TYPO" not in os.environ
"""


def test_config_factory_restores_known_and_unknown_dotenv_keys(
    pytester, monkeypatch
):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("MY_TYPO", raising=False)
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_inner=INNER_TESTS)

    result = pytester.runpytest()

    result.assert_outcomes(passed=2)
