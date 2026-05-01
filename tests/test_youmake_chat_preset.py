"""Tests for the ``youmake-chat`` toolset preset.

The Youmake fork bakes its chat-surface toolset into ``toolsets.py``
instead of patching it post-install via cloud-init YAML.  This test
guards the contract: the preset exists, resolves to a non-empty tool
list, and pulls in exactly the four expected building blocks
(web, terminal, file, todo).
"""

from toolsets import TOOLSETS, get_toolset, resolve_toolset


class TestYoumakeChatPreset:
    def test_preset_is_registered(self):
        assert "youmake-chat" in TOOLSETS
        ts = TOOLSETS["youmake-chat"]
        assert "Youmake" in ts["description"]
        assert ts["includes"] == ["web", "terminal", "file", "todo"]

    def test_resolve_includes_each_building_block(self):
        tools = set(resolve_toolset("youmake-chat"))
        # web
        assert "web_search" in tools
        assert "web_extract" in tools
        # terminal
        assert "terminal" in tools
        assert "process" in tools
        # file
        assert "read_file" in tools
        assert "write_file" in tools
        assert "patch" in tools
        assert "search_files" in tools
        # todo
        assert "todo" in tools

    def test_get_toolset_returns_definition(self):
        ts = get_toolset("youmake-chat")
        assert ts is not None
        assert ts["includes"] == ["web", "terminal", "file", "todo"]
