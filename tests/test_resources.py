import os
from types import SimpleNamespace

import pytest

from ares import resources


@pytest.mark.skipif(os.name != "nt", reason="Windows commit accounting")
@pytest.mark.parametrize("ram,commit,allowed", [(64, 160, True), (63, 160, False), (64, 159, False)])
def test_startup_reserves_physical_and_commit_headroom(monkeypatch, ram, commit, allowed):
    def information(pointer, size):
        state = pointer._obj
        state.CommitTotal = 10 * 2**30 // 4096
        state.CommitLimit = state.CommitTotal + commit * 2**30 // 4096
        state.PageSize = 4096
        return 1

    monkeypatch.setattr(resources.ctypes.windll.psapi, "GetPerformanceInfo", information)
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: SimpleNamespace(available=ram * 2**30))
    if allowed:
        assert resources.require_memory_headroom(available_gib=64, commit_gib=160) == {
            "available_gib": ram,
            "free_commit_gib": commit,
        }
    else:
        with pytest.raises(MemoryError, match="protect other applications"):
            resources.require_memory_headroom(available_gib=64, commit_gib=160)
