"""Package initializer for the RH Kelly recruitment agent.

This file ensures that the agent defined in ``agent.py`` is discoverable by
Google's Agent Development Kit (ADK). It imports the module so that the
``root_agent`` variable is available at the package level.
"""

# ADK requires that ``__init__.py`` exposes the ``agent`` module so that
# ``root_agent`` can be discovered automatically. See the ADK docs for details.
from . import agent  # noqa: F401  