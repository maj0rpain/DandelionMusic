import os
import sys

import dotenv
import dotenv.main

from config import config


def test_session_config_resolved_no_env_file():
    assert config._env_path == os.devnull


def test_session_config_has_no_unknown_vars():
    assert config.unknown_vars == {}


def test_real_find_dotenv_is_restored_after_session_start():
    config_module = sys.modules["config.config"]
    assert dotenv.find_dotenv is dotenv.main.find_dotenv
    assert config_module.find_dotenv is dotenv.main.find_dotenv
