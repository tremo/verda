"""Every panel script must at least parse: one stray quote in studio.js stopped the whole agent map from drawing (15 Oct 2026)."""
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / 'src' / 'verda' / 'ui'


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
@pytest.mark.parametrize('script', sorted(p.name for p in UI.glob('*.js')))
def test_the_panel_script_parses(script):
    result = subprocess.run(['node', '--check', str(UI / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-400:]
