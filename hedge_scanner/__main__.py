"""Enable `python -m hedge_scanner`.

The `hedge-scanner` console script resolves the package through the editable
install's `.pth` file, which uv marks with the macOS `UF_HIDDEN` flag and
CPython >= 3.11.14 then refuses to process (`site.addpackage` skips hidden
`.pth` files). Running as a module resolves the package from the working
directory instead, so it works regardless of that interaction. See README.
"""

from hedge_scanner.cli import main

if __name__ == "__main__":
    main()
