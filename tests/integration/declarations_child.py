"""Import generated declarations and attach them to the SDK's library, without starting Steam.

usage: declarations_child.py DECLARATIONS LIBRARY

Importing the declarations checks every record's layout against ctypes. Prints how many record
classes were checked and how many functions the library exports, as JSON.
"""

import importlib.util
import json
import sys

from pysteamsdk._native import bindings
from pysteamsdk._native.loader import load_library


def main(declarations: str, library: str) -> None:
    spec = importlib.util.spec_from_file_location("sdk_declarations", declarations)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attached = bindings.attach(module, load_library(library))
    returns = sum(1 for _, interface in attached.values() if interface is not None)
    report = {"records": len(module.LAYOUTS), "functions": len(attached), "interfaces": returns}
    print(json.dumps(report))


if __name__ == "__main__":
    main(*sys.argv[1:])
