"""Entry point: `cloakbrowser-mcp` runs the MCP server; `cloakbrowser-mcp memory ...` manages site memory."""
import sys


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["memory"]:
        from .memory import cli_main

        raise SystemExit(cli_main(argv[1:]))
    if argv[:1] in (["--version"], ["-V"]):
        from . import __version__

        print(__version__)
        return
    from .server import main as run_server

    run_server()


if __name__ == "__main__":
    main()
