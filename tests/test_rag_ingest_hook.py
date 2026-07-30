import sys
import subprocess
from pathlib import Path

# Add repo root to sys.path so we can import hooks and src
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def test_dispatch_args(monkeypatch):
    import hooks.rag_ingest_hook as hook
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import rag_ingest
    
    captured_argv = []
    def mock_main():
        captured_argv.extend(sys.argv)
        
    monkeypatch.setattr(rag_ingest, "main", mock_main)
    
    # test constitution-merge -> rules
    monkeypatch.setattr(sys, "argv", ["rag_ingest_hook.py", "--on", "constitution-merge"])
    captured_argv.clear()
    try:
        hook.main()
    except SystemExit as e:
        assert e.code == 0
    assert captured_argv == ["rag_ingest.py", "--collection", "rules"]

    # test session-append -> sessions
    monkeypatch.setattr(sys, "argv", ["rag_ingest_hook.py", "--on", "session-append"])
    captured_argv.clear()
    try:
        hook.main()
    except SystemExit as e:
        assert e.code == 0
    assert captured_argv == ["rag_ingest.py", "--collection", "sessions"]
    
    # test inbox-note -> sessions
    monkeypatch.setattr(sys, "argv", ["rag_ingest_hook.py", "--on", "inbox-note"])
    captured_argv.clear()
    try:
        hook.main()
    except SystemExit as e:
        assert e.code == 0
    assert captured_argv == ["rag_ingest.py", "--collection", "sessions"]

def test_never_blocks_caller_subprocess():
    hook_path = Path(__file__).parent.parent / "hooks" / "rag_ingest_hook.py"
    
    result = subprocess.run(
        [sys.executable, str(hook_path), "--on", "invalid-arg"], 
        capture_output=True, text=True
    )
    # argparse will fail, but the hook must swallow it and exit 0
    assert result.returncode == 0

def test_never_blocks_caller_internal(monkeypatch):
    import hooks.rag_ingest_hook as hook
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import rag_ingest
    
    def raise_exception():
        raise Exception("forced error")
        
    monkeypatch.setattr(rag_ingest, "main", raise_exception)
    monkeypatch.setattr(sys, "argv", ["rag_ingest_hook.py", "--on", "constitution-merge"])
    
    try:
        hook.main()
    except SystemExit as e:
        assert e.code == 0
    else:
        assert False, "Should have called sys.exit(0)"
