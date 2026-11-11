"""The shared test helpers' own tests: the import-reading helpers on small
sources under `tmp_path`, and the other shared helpers."""

import types

import pytest

from db_seam import FailingSessionFactory, with_db
from helpers import (
    assert_logged_with_traceback,
    RedirectFollower,
    attribute_names,
    dockerfile_commands,
    imported_names,
    imported_roots,
    run,
    shell_commands,
    type_checking_imports,
)


def module_of(tmp_path, source):
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return types.SimpleNamespace(__file__=str(path))


GUARDED = """\
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from musicbot import song
    import yt_dlp
"""


def test_imports_and_from_imports_give_modules_and_pairs(tmp_path):
    module = module_of(tmp_path, "import a.b\nfrom c import d, e\n")
    assert imported_names(module) == {"a.b", "c", "c.d", "c.e"}


def test_a_relative_import_has_an_empty_module(tmp_path):
    module = module_of(tmp_path, "from . import x\n")
    assert imported_names(module) == {"", ".x"}


def test_skip_type_checking_skips_only_that_block(tmp_path):
    module = module_of(tmp_path, GUARDED)
    assert "yt_dlp" in imported_names(module)
    assert imported_names(module, skip_type_checking=True) == {
        "os",
        "typing",
        "typing.TYPE_CHECKING",
    }


def test_roots_reduce_names_to_their_first_part(tmp_path):
    module = module_of(tmp_path, "import a.b.c\nfrom d.e import f\n")
    assert imported_roots(module) == {"a", "d"}


def test_roots_honour_skip_type_checking(tmp_path):
    module = module_of(tmp_path, GUARDED)
    assert imported_roots(module) == {"os", "typing", "musicbot", "yt_dlp"}
    assert imported_roots(module, skip_type_checking=True) == {
        "os",
        "typing",
    }


def test_type_checking_imports_are_the_block_statements_in_order(tmp_path):
    module = module_of(tmp_path, GUARDED)
    assert type_checking_imports(module) == [
        "from musicbot import song",
        "import yt_dlp",
    ]


def test_type_checking_imports_are_empty_without_a_block(tmp_path):
    module = module_of(tmp_path, "import os\n")
    assert type_checking_imports(module) == []


def test_a_block_nested_in_a_function_is_skipped_and_read(tmp_path):
    source = (
        "def f():\n"
        "    if TYPE_CHECKING:\n"
        "        import hidden\n"
        "    import shown\n"
        "if TYPE_CHECKING:\n"
        "    import later\n"
    )
    module = module_of(tmp_path, source)
    assert imported_names(module, skip_type_checking=True) == {"shown"}
    assert type_checking_imports(module) == ["import hidden", "import later"]


def test_a_dotted_type_checking_test_is_recognised_by_neither(tmp_path):
    source = "import typing\nif typing.TYPE_CHECKING:\n    import seen\n"
    module = module_of(tmp_path, source)
    assert "seen" in imported_names(module, skip_type_checking=True)
    assert type_checking_imports(module) == []


def test_attribute_names_are_name_dot_attr_pairs(tmp_path):
    module = module_of(tmp_path, "x = discord.ui.View\ny = a.b.c\n")
    assert attribute_names(module) == {"discord.ui", "a.b"}


def test_an_attribute_on_a_non_name_gives_nothing(tmp_path):
    module = module_of(tmp_path, "x = f().attr\ny = a[0].attr\n")
    assert attribute_names(module) == set()


def test_dockerfile_commands_normalise_keywords_and_spacing():
    text = "from x\nRun\tapt  get\n  Arg   A=1\nCMD\n"
    assert dockerfile_commands(text) == [
        "FROM x",
        "RUN apt  get",
        "ARG A=1",
        "CMD",
    ]


def test_dockerfile_commands_join_continuations_and_drop_noise():
    text = "# c\n\nRUN a \\\n  && b\n   # indented\n"
    assert dockerfile_commands(text) == ["RUN a    && b"]


def test_shell_commands_split_in_order_stripped_and_drop_empties():
    text = "a && && b\nc \\\n d && e\n"
    assert shell_commands(text) == ["a", "b", "c   d", "e"]


IN_FUNCTIONS = """\
import os


def f():
    import json
    from re import match


async def g():
    import abc


if os:
    import sys
"""


def test_module_level_only_skips_imports_inside_function_bodies(tmp_path):
    module = module_of(tmp_path, IN_FUNCTIONS)
    assert imported_names(module) >= {"json", "re", "re.match", "abc"}
    assert imported_names(module, module_level_only=True) == {"os", "sys"}


def test_roots_honour_module_level_only(tmp_path):
    module = module_of(tmp_path, IN_FUNCTIONS)
    assert imported_roots(module, module_level_only=True) == {"os", "sys"}


def test_failing_session_factory_fails_counts_and_then_runs_for_real():
    async def test(factory):
        failing = FailingSessionFactory(factory, "flush", ValueError("boom"))
        async with failing() as session:
            with pytest.raises(ValueError):
                await session.flush()
        assert failing.attempts == 1
        failing.failing = False
        async with failing() as session:
            await session.flush()
        assert failing.attempts == 2

        made = FailingSessionFactory(factory, "flush", lambda: KeyError("k"))
        async with made() as session:
            with pytest.raises(KeyError):
                await session.flush()

    with_db(test)


def test_logged_with_traceback_passes_when_message_precedes_it():
    assert_logged_with_traceback(
        "boom: failed\nTraceback (most recent call last):\n", "boom"
    )


def test_logged_with_traceback_fails_when_message_missing():
    with pytest.raises(AssertionError):
        assert_logged_with_traceback("Traceback (most recent)", "boom")


def test_logged_with_traceback_fails_when_message_follows_traceback():
    with pytest.raises(AssertionError):
        assert_logged_with_traceback("Traceback (most recent)\nboom", "boom")


def test_logged_with_traceback_fails_without_a_traceback():
    with pytest.raises(AssertionError):
        assert_logged_with_traceback("boom: failed\n", "boom")


def test_redirect_follower_looks_up_a_mapping_proxy_by_url():
    answers = types.MappingProxyType({"a": "A!", "b": "B!"})
    follower = RedirectFollower(answers)
    assert [run(follower("a")), run(follower("b"))] == ["A!", "B!"]
    assert follower.calls == ["a", "b"]


def test_redirect_follower_raises_an_exception_found_in_a_mapping_proxy():
    boom = RuntimeError("boom")
    follower = RedirectFollower(types.MappingProxyType({"a": boom}))
    with pytest.raises(RuntimeError) as caught:
        run(follower("a"))
    assert caught.value is boom


def test_redirect_follower_raises_key_error_for_a_url_absent_from_a_proxy():
    follower = RedirectFollower(types.MappingProxyType({"a": "A!"}))
    with pytest.raises(KeyError):
        run(follower("b"))
