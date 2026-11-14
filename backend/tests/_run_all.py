"""
Zero-dependency test runner for environments without pytest installed.
Not a replacement for `python -m pytest backend/tests/ -q` (which remains
the intended way to run these -- pytest is in requirements), but this lets
the plain, fixture-free test_*.py functions run with nothing beyond the
stdlib.

Previously this runner called EVERY test_* function with no arguments,
so each test that legitimately takes a pytest fixture (tmp_path,
monkeypatch, a TestClient fixture) or is parametrized raised TypeError
and was printed as "[FAIL]" -- a long list of false failures that made
the real result unreadable. Such tests are now reported as SKIPPED
(pytest required), and async tests are executed on a fresh event loop
instead of being counted as passing without running.

Run: python3 backend/tests/_run_all.py
"""
import asyncio
import importlib
import importlib.util
import inspect
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_DIR = os.path.dirname(__file__)


def _needs_pytest(fn) -> str:
    """Why this test can't run without pytest ("" = it can)."""
    if getattr(fn, "pytestmark", None):
        for mark in fn.pytestmark:
            if getattr(mark, "name", "") in ("parametrize", "skip", "skipif", "xfail"):
                return f"pytest mark @{mark.name}"
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return "signature could not be read"
    required = [
        p.name for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    if required:
        return "fixture(s): " + ", ".join(required)
    return ""


def main() -> int:
    failures = 0
    passed = 0
    skipped = 0
    skip_detail: list[str] = []
    for fname in sorted(os.listdir(TEST_DIR)):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        modname = fname[:-3]
        spec = importlib.util.spec_from_file_location(modname, os.path.join(TEST_DIR, fname))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            print(f"[IMPORT ERROR] {fname}")
            traceback.print_exc()
            failures += 1
            continue
        for name in dir(mod):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            reason = _needs_pytest(fn)
            if reason:
                skipped += 1
                skip_detail.append(f"{modname}.{name} ({reason})")
                continue
            try:
                if inspect.iscoroutinefunction(fn):
                    asyncio.run(fn())
                else:
                    result = fn()
                    if inspect.isawaitable(result):
                        asyncio.run(result)
                passed += 1
            except Exception:
                failures += 1
                print(f"[FAIL] {modname}.{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failures} failed, {skipped} skipped (need pytest)")
    if skipped:
        print("Run `python -m pytest tests -q` for the skipped tests:")
        for line in skip_detail[:40]:
            print(f"  - {line}")
        if len(skip_detail) > 40:
            print(f"  ... and {len(skip_detail) - 40} more")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
