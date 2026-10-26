import os
import subprocess
import sys
import textwrap

import pysteamsdk


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("STEAM")}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_imports_without_steam() -> None:
    result = run_python("import pysteamsdk; print(pysteamsdk.__version__)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == pysteamsdk.__version__


def test_import_loads_no_native_library() -> None:
    result = run_python(
        """
        import _ctypes
        import ctypes
        import sys

        loads = []

        def record(*args, **kwargs):
            loads.append(args)
            raise OSError("native library load during import")

        ctypes.CDLL.__init__ = record
        for name in ("dlopen", "LoadLibrary"):
            if hasattr(_ctypes, name):
                setattr(_ctypes, name, record)

        import pysteamsdk

        sys.exit(f"native library loaded: {loads}" if loads else 0)
        """
    )
    assert result.returncode == 0, result.stderr


def test_import_leaves_process_environment_unchanged() -> None:
    result = run_python(
        """
        import os
        import sys

        before = dict(os.environ), os.getcwd()
        import pysteamsdk

        sys.exit(0 if (dict(os.environ), os.getcwd()) == before else "environment changed")
        """
    )
    assert result.returncode == 0, result.stderr
