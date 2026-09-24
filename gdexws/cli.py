"""Unified `gdexws` CLI.

Every module in TOOL_PACKAGES that defines `add_arguments(parser)` and
`run(args)` becomes a subcommand (`add_global_meta.py` -> `gdexws add-global-meta`).
Modules may also define `DESCRIPTION`, shown in `gdexws --help`.
"""

import argparse
import importlib
import pkgutil

TOOL_PACKAGES = ["gdexws.tools", "gdexws.composers"]


def discover():
    """Return {subcommand: imported module} for every tool module found."""
    found = {}
    for pkg_name in TOOL_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.ispkg or info.name.startswith("_"):
                continue
            module = importlib.import_module(f"{pkg_name}.{info.name}")
            if not (hasattr(module, "add_arguments") and hasattr(module, "run")):
                continue
            name = info.name.replace("_", "-")
            if name in found:
                raise RuntimeError(f"Duplicate tool name '{name}': {found[name].__name__} and {module.__name__}")
            found[name] = module
    return found


def build_parser():
    """Build the argparse tree: `gdexws` -> one subparser per discovered tool."""
    # Parent parser with options shared by all tools; add_help=False avoids a duplicate -h.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")

    # Top-level parser: only understands the tool name, then delegates to that tool's subparser.
    parser = argparse.ArgumentParser(prog="gdexws", description="GDEX web service tools")

    # Slot for the tool name (like `commit` in `git commit`); stored as args.tool.
    subparsers = parser.add_subparsers(dest="tool", metavar="<tool>", required=True)

    for name, module in sorted(discover().items()):
        description = getattr(module, "DESCRIPTION", module.__doc__)

        # One independent parser per tool, inheriting the shared options from `common`.
        sub = subparsers.add_parser(name, help=description, description=description, parents=[common])

        # The tool declares its own flags on its own subparser only.
        module.add_arguments(sub)

        # Store the tool's run() as args.func for main() to call.
        sub.set_defaults(func=module.run)
    return parser


def main():
    # Parse sys.argv with the matching subparser, then call the chosen tool's run(args).
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
