"""One module per CLI subcommand. Each exposes `register(app: typer.Typer)`.

Keeping commands in separate modules lets them be developed independently;
cli.py only wires them together.
"""
