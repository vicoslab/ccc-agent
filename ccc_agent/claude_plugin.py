"""Materialize the bundled Claude Code CCC plugin marketplace source.

This module is intended for image builds.  It writes a local marketplace source
that can be consumed by Claude Code's documented pre-seed flow:

    python -m ccc_agent.claude_plugin \
      --write-to /opt/claude-seed/marketplaces/ccc-agent
    CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
      claude plugin marketplace add /opt/claude-seed/marketplaces/ccc-agent
    CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
      claude plugin install ccc@ccc-agent

At runtime, set CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-seed.
"""

import argparse
import os
import shutil
from importlib import resources


PLUGIN_DIR = "claude-ccc-containment"
MARKETPLACE_DIR = ".claude-plugin"


def bundled_plugins_dir():
    """On-disk path to bundled plugin package data."""
    return str(resources.files("ccc_agent").joinpath("assets", "plugins"))


def materialize_marketplace(dest, source_root=None, force=False):
    """Copy the bundled Claude marketplace source to ``dest``.

    ``dest`` becomes a local marketplace directory with:

      .claude-plugin/marketplace.json
      claude-ccc-containment/...

    This writes the marketplace portion of the runtime seed. For a local source,
    place it at ``<seed>/marketplaces/ccc-agent`` because Claude resolves seed
    marketplace content from that location. The image build should then run
    Claude's own `plugin marketplace add` and `plugin install` commands with
    ``CLAUDE_CODE_PLUGIN_CACHE_DIR`` pointing at the same seed root.
    """
    source_root = source_root or bundled_plugins_dir()
    marketplace_src = os.path.join(source_root, MARKETPLACE_DIR)
    plugin_src = os.path.join(source_root, PLUGIN_DIR)
    if not os.path.isdir(marketplace_src):
        raise FileNotFoundError(marketplace_src)
    if not os.path.isdir(plugin_src):
        raise FileNotFoundError(plugin_src)

    dest = os.path.abspath(dest)
    if os.path.exists(dest):
        if not force:
            raise FileExistsError(dest)
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    shutil.copytree(marketplace_src, os.path.join(dest, MARKETPLACE_DIR))
    shutil.copytree(plugin_src, os.path.join(dest, PLUGIN_DIR))
    return dest


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ccc_agent.claude_plugin",
        description="Write the bundled CCC Claude plugin marketplace source")
    parser.add_argument("--write-to", required=True,
                        help="destination directory for the local marketplace source")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing destination directory")
    args = parser.parse_args(argv)
    path = materialize_marketplace(args.write_to, force=args.force)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
