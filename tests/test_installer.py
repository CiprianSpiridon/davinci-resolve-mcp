"""Tests for the setup/doctor installer — no Resolve or MCP client required."""

from __future__ import annotations

from davinci_resolve_mcp import installer


def test_merge_config_preserves_other_servers_and_keys():
    existing = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}
    merged = installer.merge_config(existing, {"command": "/abs/davinci-resolve-mcp"})
    assert merged["mcpServers"]["other"] == {"command": "x"}
    assert merged["mcpServers"]["davinci-resolve"]["command"] == "/abs/davinci-resolve-mcp"
    assert merged["theme"] == "dark"  # unrelated top-level keys untouched


def test_merge_config_from_empty():
    merged = installer.merge_config(None, {"command": "srv"})
    assert merged == {"mcpServers": {"davinci-resolve": {"command": "srv"}}}


def test_merge_config_overwrites_our_own_entry_only():
    existing = {"mcpServers": {"davinci-resolve": {"command": "old"}, "keep": {"command": "k"}}}
    merged = installer.merge_config(existing, {"command": "new"})
    assert merged["mcpServers"]["davinci-resolve"]["command"] == "new"
    assert merged["mcpServers"]["keep"] == {"command": "k"}


def test_run_setup_manual_dry_run_writes_nothing(capsys):
    rc = installer.run_setup(["manual"], dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "davinci-resolve" in out
    assert "mcpServers" in out


def test_all_clients_shape():
    assert "claude-code" in installer.ALL_CLIENTS
    assert "manual" in installer.ALL_CLIENTS
    # file-based clients all map to a config path helper
    for name in ("claude-desktop", "cursor", "windsurf"):
        assert name in installer.ALL_CLIENTS
