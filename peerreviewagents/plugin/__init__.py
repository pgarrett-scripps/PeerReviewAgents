"""The Python side of the 0.9 plugin: MCP servers that make no model calls.

Claude Code owns the orchestration in 0.9. Everything in this package is a
tool server: it reads the immutable manuscript, guards what a reviewer may
say and stores the run's files. Nothing here talks to a model or to a
provider CLI.
"""
