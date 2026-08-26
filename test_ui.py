from pathlib import Path

def test_manual_refresh_feedback_present():
    html=(Path(__file__).parent/'static'/'index.html').read_text()
    assert 'id="refreshBtn"' in html
    assert 'Обновляю…' in html
    assert 'Обновлено ✓' in html
    assert 'Последнее обновление:' in html
    assert "cache:'no-store'" in html

def test_pro_12_sw_registration():
    html=(Path(__file__).parent/'static'/'index.html').read_text()
    assert 'service-worker.js?v=pro12' in html
