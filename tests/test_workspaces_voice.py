"""Workspaces in the conductor: the voice fast path and the [workspace: X] turn header."""
import asyncio
import re

import pytest

from walter import workspaces as W

from test_barge_in import make


@pytest.fixture(autouse=True)
def _example_projects(example_workspaces):
    """Home + Acme (normal) + Vault (walled): generic example projects (conftest.py)."""
    return example_workspaces



def test_no_workspaces_no_header_no_fast_path():
    async def go():
        c, _ = make("aec")
        assert c._context_notes("voice", {}) == ""
        assert c._fast_match("switch to acme corp") is None
    asyncio.run(go())


def test_switch_by_voice_and_header():
    async def go():
        W.WorkspaceStore().ensure()
        c, _ = make("aec")
        assert c._context_notes("voice", {}).startswith("[workspace: Home]")
        c.submit("voice", "Walter, switch to acme corp")
        assert c.inbox.empty()
        assert "Acme" in c.voice.said[-1]
        assert W.WorkspaceStore().current_id("wall") == "acme"
        assert W.WorkspaceStore().current_id("kitchen") == "home"     # only this face moved
        notes = c._context_notes("voice", {})
        assert notes.startswith("[workspace: Acme]")
        assert "switched this face's workspace to Acme" in notes      # the brain hears it was done
        c.submit("voice", "work on the vault")
        assert "walled" in c.voice.said[-1]
        assert "[workspace: Vault (walled)]" in c._context_notes("voice", {})
        c.submit("voice", "which workspace am I in")
        assert c.voice.said[-1] == "We're in Vault."
    asyncio.run(go())


def test_unknown_workspace_goes_to_the_brain():
    async def go():
        W.WorkspaceStore().ensure()
        c, _ = make("aec")
        assert c._fast_match("work on the garage") is None
        assert c._fast_match("switch to local ears").kind == "ears"   # existing commands still win
    asyncio.run(go())
