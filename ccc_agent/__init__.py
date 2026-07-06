"""ccc_agent: trusted BranchFS agent-containment supervisor for CCC.

Stdlib-only by design: this package runs as the trusted launcher/supervisor
inside arbitrary CCC containers and must not depend on site-packages the
container may not have.
"""

__version__ = "0.4"
