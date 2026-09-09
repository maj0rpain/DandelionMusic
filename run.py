"""
Entry point wrapper. Head to musicbot/__main__.py for the real main file.

This used to re-exec itself as a detached background subprocess (a
`--run` flag, stdout forwarding, a Ctrl+C handler that sent a
"shutdown" line over stdin) so users could close the launching
terminal. That was removed in 67fd2b2 - the subprocess wrapping broke
auto-restart - and this file has just forwarded to the musicbot
package ever since.

It is kept because it is the PyInstaller entry point (see
config/build.py) and what README tells users to start.
"""

if __name__ == "__main__":
    import runpy

    runpy.run_module("musicbot", run_name="__main__")
