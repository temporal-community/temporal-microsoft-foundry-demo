from __future__ import annotations

import pytest

from app.config import DEFAULT_PRESETS, Config

REQUIRED = {"TFD_ACCESS_CODE": "booth", "TFD_INTERNAL_TOKEN": "token"}


def test_an_access_code_and_an_internal_token_are_required() -> None:
    with pytest.raises(ValueError, match="TFD_ACCESS_CODE"):
        Config.from_env({"TFD_INTERNAL_TOKEN": "token"})
    with pytest.raises(ValueError, match="TFD_INTERNAL_TOKEN"):
        Config.from_env({"TFD_ACCESS_CODE": "booth"})


def test_defaults_suit_a_console_beside_the_temporal_server() -> None:
    config = Config.from_env(REQUIRED)
    assert config.temporal_address == "127.0.0.1:7233"
    assert config.temporal_namespace == "default"
    assert config.task_queue == "tfd"
    assert config.cookie_secure is True  # a cookie over plain http has to be asked for
    assert config.foundry_stop is False  # acting on Foundry sessions has to be asked for
    assert config.presets == DEFAULT_PRESETS
    assert len(config.session_secret) == 32


def test_presets_as_json_or_separated_by_bars() -> None:
    as_json = Config.from_env({**REQUIRED, "TFD_PRESETS": '["What is new?", "What changed, and why?"]'})
    assert as_json.presets == ("What is new?", "What changed, and why?")
    with_bars = Config.from_env({**REQUIRED, "TFD_PRESETS": "What is new? | What changed, and why?"})
    assert with_bars.presets == ("What is new?", "What changed, and why?")


@pytest.mark.parametrize("raw", ["[What is new?,What changed?]", "[1, 2]", "[]", '["ok", 3]', '["ok", ""]'])
def test_unreadable_presets_fall_back_to_the_built_in_questions(raw: str) -> None:
    assert Config.from_env({**REQUIRED, "TFD_PRESETS": raw}).presets == DEFAULT_PRESETS


def test_a_trailing_slash_on_urls_is_dropped() -> None:
    config = Config.from_env(
        {
            **REQUIRED,
            "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/demo/",
            "TFD_TEMPORAL_UI_URL": "/temporal-ui/",
        }
    )
    assert config.foundry_project_endpoint.endswith("/projects/demo")
    assert config.temporal_ui_url == "/temporal-ui"
