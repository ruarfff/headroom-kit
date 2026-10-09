"""Codex config overrides belong to the same scope as routing settings."""

from headroom_kit.session import codex_arguments


def test_config_overrides_share_the_subcommand_scope() -> None:
    endpoint = "http://127.0.0.1:8788/v1"
    args = [
        "-c",
        'model_reasoning_effort="low"',
        "exec",
        "--config=model_verbosity=low",
        "-cweb_search=disabled",
        "--",
        "a prompt with --config=literal",
    ]
    actual = codex_arguments(args, endpoint)
    # Codex 0.154.0 replaces global -c values when a subcommand has its
    # own -c values. Keep user settings and routing in that same scope.
    assert actual[0] == "exec"
    assert actual[-2:] == args[-2:]
    settings = dict(value.split("=", 1) for value in actual[2:-2:2])
    assert settings["model_reasoning_effort"] == '"low"'
    assert settings["model_verbosity"] == "low"
    assert settings["web_search"] == "disabled"
    assert settings["model_provider"] == '"openai"'
    assert settings["openai_base_url"] == f'"{endpoint}"'
