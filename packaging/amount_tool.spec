# Independent folder distribution; never writes NumberTool's application directory.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
hidden = collect_submodules('number_app.amount_mode')
excluded = ['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtPdf']
payload = [(str(root / 'requirements-dev.lock'), '.'),
           (str(root / '按套金额随机小组_验收样例.json'), 'fixtures')]

def analysis(entry):
    value = Analysis([str(root / entry)], pathex=[str(root / 'src')], binaries=[], datas=payload,
                     hiddenimports=hidden, hookspath=[], excludes=excluded)
    # Keep the Qt ABI resolved by the locked environment, not foreign PATH ICU DLLs.
    value.binaries = [e for e in value.binaries if Path(e[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]
    return value

a = analysis('amount_main.py')
tool = EXE(PYZ(a.pure), a.scripts, [], exclude_binaries=True, name='AmountTool', console=False, debug=False)
b = analysis('amount_verify_main.py')
verify = EXE(PYZ(b.pure), b.scripts, [], exclude_binaries=True, name='AmountVerify', console=False, debug=False)
coll = COLLECT(tool, verify, a.binaries, a.datas, b.binaries, b.datas, strip=False, upx=False, name='AmountTool')
