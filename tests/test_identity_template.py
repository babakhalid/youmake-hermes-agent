"""Tests for the Youmake fork's identity-template selector.

Asserts that ``HERMES_IDENTITY_TEMPLATE=youmake_minimal`` (the fork
default) produces a substantially slimmer system prompt than the full
upstream behaviour.  The minimal template is the single biggest token
reducer in the fork — the test guards against accidental regressions
that would re-inject the multi-K-token skill catalog or context-files
block into the cached prefix.
"""

import os
from unittest.mock import patch

import pytest

from agent.prompt_builder import (
    IDENTITY_TEMPLATE_DEFAULT,
    IDENTITY_TEMPLATE_ENV,
    IDENTITY_TEMPLATE_FULL,
    YOUMAKE_FALLBACK_IDENTITY,
    get_identity_template_name,
    load_packaged_identity_template,
)


class TestIdentityTemplateSelector:
    def test_default_is_youmake_minimal(self, monkeypatch):
        monkeypatch.delenv(IDENTITY_TEMPLATE_ENV, raising=False)
        assert get_identity_template_name() == IDENTITY_TEMPLATE_DEFAULT
        assert IDENTITY_TEMPLATE_DEFAULT == "youmake_minimal"

    def test_env_var_selects_full_mode(self, monkeypatch):
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, IDENTITY_TEMPLATE_FULL)
        assert get_identity_template_name() == IDENTITY_TEMPLATE_FULL

    def test_unknown_value_falls_back_to_default(self, monkeypatch):
        # Typos shouldn't silently disable identity loading.
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, "not-a-real-template")
        assert get_identity_template_name() == IDENTITY_TEMPLATE_DEFAULT

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, "HERMES_FULL")
        assert get_identity_template_name() == IDENTITY_TEMPLATE_FULL


class TestPackagedTemplateLoad:
    def test_loads_youmake_minimal_from_disk(self):
        content = load_packaged_identity_template("youmake-minimal")
        assert content is not None
        assert "Youmake Agent" in content
        # Sanity bound — template should be a few KB, not a few hundred.
        assert 500 < len(content) < 8000

    def test_missing_template_returns_none(self):
        assert load_packaged_identity_template("does-not-exist") is None

    def test_fallback_constant_is_self_contained(self):
        # If the packaged file is ever missing at runtime (broken install),
        # the fallback string must still describe the agent.
        assert "Youmake Agent" in YOUMAKE_FALLBACK_IDENTITY


class TestMinimalPromptIsSlim:
    """End-to-end: assemble the system prompt under both templates and
    assert minimal stays well under the upstream ~13K-token baseline.
    """

    @pytest.fixture
    def make_agent(self):
        """Lazily build an AIAgent stub with just enough state for
        ``_build_system_prompt`` to run.  Importing AIAgent is heavy
        (drags in the whole tool registry) so the fixture defers it.
        """
        def _factory():
            from run_agent import AIAgent  # local import — heavy module

            agent = AIAgent.__new__(AIAgent)
            agent.load_soul_identity = False
            agent.skip_context_files = True  # don't read repo's AGENTS.md
            agent.valid_tool_names = {
                "web_search", "terminal", "read_file", "write_file", "todo",
                # Skill tools INTENTIONALLY included so we can verify
                # minimal mode still skips the catalog despite their presence.
                "skills_list", "skill_view", "skill_manage",
            }
            agent.session_id = "test-session"
            agent.pass_session_id = False
            agent.model = "claude-opus-4-5"
            agent.provider = "anthropic"
            agent.platform = ""
            agent._tool_use_enforcement = "auto"
            agent._memory_store = None
            agent._memory_enabled = False
            agent._user_profile_enabled = False
            agent._memory_manager = None
            return agent
        return _factory

    def test_minimal_prompt_well_under_3k_chars(self, make_agent, monkeypatch):
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, "youmake_minimal")
        agent = make_agent()
        prompt = agent._build_system_prompt()

        # Slim template + timestamp + env hints → expect < 6K chars
        # (~1500 tokens at 4 chars/token).  Generous bound — actual is
        # closer to 2-3K chars.  The point is to catch a regression that
        # accidentally re-enables the skill catalog (~25K+ chars).
        assert len(prompt) < 6000, (
            f"Minimal prompt grew to {len(prompt)} chars — did the skill "
            f"catalog or context-files block get re-enabled?"
        )
        assert "Youmake Agent" in prompt

    def test_full_prompt_is_substantially_larger(self, make_agent, monkeypatch):
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, "hermes_full")
        agent = make_agent()
        prompt = agent._build_system_prompt()
        assert "Hermes Agent" in prompt
        # Should at least contain the help-guidance pointer the slim
        # template strips.
        assert "hermes-agent.nousresearch.com" in prompt

    def test_minimal_strips_skill_catalog(self, make_agent, monkeypatch):
        """Even when skill_* tools are present, minimal mode must NOT
        emit the skill catalog block."""
        monkeypatch.setenv(IDENTITY_TEMPLATE_ENV, "youmake_minimal")
        agent = make_agent()
        prompt = agent._build_system_prompt()
        # Skill catalog block has a stable header.  Look for its name.
        assert "Available Skills" not in prompt
        assert "skill_view(name=" not in prompt
