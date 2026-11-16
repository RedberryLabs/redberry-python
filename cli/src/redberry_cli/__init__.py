"""The ``redberry`` command: sign in, then wire an agent to send its spans to your Redberry install.

    redberry login       sign this machine in for your company (a device code you approve in the portal)
    redberry whoami      the company this machine is signed in for
    redberry connect     wire an agent's code (--platform aws-lambda | azure-functions)
    redberry logout      forget the session

See ``redberry_cli.main`` for what each command does.
"""

from redberry_cli._version import __version__

__all__ = ["__version__"]
