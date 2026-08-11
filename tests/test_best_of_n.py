"""Unit tests for the Best-of-N engine's config and command building."""

from __future__ import annotations

from codify.best_of_n import (
    DEFAULT_CRITERIA,
    DEFAULT_PROPOSAL_MODEL,
    BestOfNConfig,
    ProposerSpec,
    _build_command,
)


def test_default_config_roster_and_criteria() -> None:
    """Defaults: claude/codex/pi roster, DeepSeek models, four criteria."""

    cfg = BestOfNConfig()
    assert [p.harness for p in cfg.proposers] == ["claude", "codex", "pi"]
    assert cfg.proposal_model == DEFAULT_PROPOSAL_MODEL
    assert cfg.verifier.model == "deepseek-v4-flash"
    assert cfg.verifier.api_key_env == "DEEPSEEK_API_KEY"
    assert cfg.criteria == DEFAULT_CRITERIA


def test_config_round_trips_through_dict() -> None:
    """to_dict → from_dict preserves every tunable."""

    cfg = BestOfNConfig()
    cfg.proposers[1].enabled = False
    cfg.proposers[2].count = 3
    cfg.criteria = {"Correctness": "does it work"}
    cfg.verifier.n_evaluations = 8
    cfg.verifier.seed = 42
    cfg.keep_proposal_branches = True
    restored = BestOfNConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_from_dict_tolerates_partial_and_junk_input() -> None:
    """Unknown keys are ignored; missing keys keep defaults."""

    cfg = BestOfNConfig.from_dict({"unknown": 1, "verifier": {"pivots": 5}})
    assert cfg.verifier.pivots == 5
    assert cfg.criteria == DEFAULT_CRITERIA
    assert BestOfNConfig.from_dict(None) == BestOfNConfig()


def test_gateway_default_model_only_reaches_gateway_harnesses() -> None:
    """The run-level DeepSeek default applies to pi, not vendor-locked CLIs."""

    prompt = "fix the bug"
    pi_argv = _build_command(ProposerSpec(harness="pi"), prompt, DEFAULT_PROPOSAL_MODEL)
    assert "--model" in pi_argv and DEFAULT_PROPOSAL_MODEL in pi_argv

    claude_argv = _build_command(ProposerSpec(harness="claude"), prompt, DEFAULT_PROPOSAL_MODEL)
    assert "--model" not in claude_argv
    assert prompt in claude_argv


def test_explicit_model_override_always_applies() -> None:
    """A per-proposer model override reaches even vendor-locked CLIs."""

    argv = _build_command(
        ProposerSpec(harness="claude", model="claude-sonnet-5"), "task", DEFAULT_PROPOSAL_MODEL
    )
    assert argv[-2:] == ["--model", "claude-sonnet-5"]


def test_custom_command_template_substitutes_placeholders() -> None:
    """An explicit command template replaces the built-in launch entirely."""

    spec = ProposerSpec(
        harness="mytool", command=["mytool", "--task", "{prompt}", "-m", "{model}"]
    )
    argv = _build_command(spec, "do it", "some-model")
    assert argv == ["mytool", "--task", "do it", "-m", "some-model"]


def test_claude_stream_json_renders_readable_lines() -> None:
    """claude stream-json events become readable text; noise is dropped."""
    from codify.best_of_n import render_transcript_line

    assistant = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"Fixing"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"pytest -q"}}]}}'
    )
    assert render_transcript_line("claude", assistant) == "Fixing\n→ Bash(pytest -q)\n"
    assert render_transcript_line("claude", '{"type":"result","result":"done"}') == (
        "\n[result] done\n"
    )
    assert render_transcript_line("claude", '{"type":"user","message":{}}') is None
    assert render_transcript_line("claude", "plain\n") == "plain\n"


def test_pi_json_mode_renders_assistant_messages_only() -> None:
    """pi --mode json: assistant message_end renders; other events drop."""
    from codify.best_of_n import render_transcript_line

    msg = (
        '{"type":"message_end","message":{"role":"assistant","content":'
        '[{"type":"text","text":"Writing tests"},'
        '{"type":"toolCall","name":"bash","arguments":{"command":"ls"}}]}}'
    )
    assert render_transcript_line("pi", msg) == "Writing tests\n→ bash(ls)\n"
    assert render_transcript_line("pi", '{"type":"turn_start"}') is None
    user = '{"type":"message_end","message":{"role":"user","content":[]}}'
    assert render_transcript_line("pi", user) is None


def test_codex_output_passes_through_untouched() -> None:
    """Harnesses without a JSON stream keep their raw lines."""
    from codify.best_of_n import render_transcript_line

    assert render_transcript_line("codex", '{"looks":"like json"}') == '{"looks":"like json"}'


def test_ensure_repo_ready_initializes_plain_folders(tmp_path) -> None:
    """A never-init'd folder becomes a repo with its files as the baseline."""
    import subprocess

    from codify.best_of_n import ensure_repo_ready

    (tmp_path / "app.py").write_text("print('hi')\n")
    note = ensure_repo_ready(str(tmp_path))
    assert note is not None and "initialized" in note
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
    assert "Initial commit" in log.stdout
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    )
    assert status.stdout.strip() == ""


def test_ensure_repo_ready_commits_unborn_head(tmp_path) -> None:
    """An init'd-but-never-committed repo gets the baseline commit."""
    import subprocess

    from codify.best_of_n import ensure_repo_ready

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "notes.md").write_text("draft\n")
    note = ensure_repo_ready(str(tmp_path))
    assert note is not None and "baseline" in note
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    )
    assert head.returncode == 0


def test_ensure_repo_ready_leaves_established_repos_alone(tmp_path) -> None:
    """A repo with commits is untouched (dirty-tree policy stays upstream)."""
    import subprocess

    from codify.best_of_n import ensure_repo_ready

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "a.txt").write_text("dirty\n")
    assert ensure_repo_ready(str(tmp_path)) is None
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True)
    assert len(log.stdout.strip().splitlines()) == 1
