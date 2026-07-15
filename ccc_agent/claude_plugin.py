"""Materialize the bundled Claude Code CCC plugin marketplace source.

The wheel contains the marketplace and plugin source. This module turns those
package assets into Claude Code's documented seed layout without invoking Claude:

    python -m ccc_agent.claude_plugin --seed-dir /opt/claude-seed

``ccc-agent setup`` calls the same materializer automatically.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import shutil
from importlib import resources


PLUGIN_DIR = "claude-ccc-containment"
MARKETPLACE_DIR = ".claude-plugin"
MARKETPLACE_NAME = "ccc-agent"
PLUGIN_NAME = "ccc"
PLUGIN_ID = "%s@%s" % (PLUGIN_NAME, MARKETPLACE_NAME)


def bundled_plugins_dir():
    """On-disk path to bundled plugin package data."""
    return str(resources.files("ccc_agent").joinpath("assets", "plugins"))


def materialize_marketplace(dest, source_root=None, force=False):
    """Copy the bundled Claude marketplace source to ``dest``.

    ``dest`` becomes a local marketplace directory with:

      .claude-plugin/marketplace.json
      claude-ccc-containment/...

    This lower-level helper writes only marketplace source. Normal setup should
    call :func:`materialize_seed`, which also creates the cache and metadata.
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


def _load_json_object(path):
    try:
        with open(path) as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _replace_tree(source, dest):
    if os.path.lexists(dest):
        if os.path.islink(dest) or not os.path.isdir(dest):
            os.unlink(dest)
        else:
            shutil.rmtree(dest)
    shutil.copytree(source, dest)


def _timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def materialize_seed(seed_dir, source_root=None):
    """Build a complete Claude plugin seed from bundled package data.

    Only CCC-owned marketplace/cache entries are replaced. Unrelated
    marketplaces and installed plugins in a shared seed are preserved.
    """
    source_root = source_root or bundled_plugins_dir()
    seed_dir = os.path.abspath(seed_dir)
    marketplace_dir = os.path.join(seed_dir, "marketplaces", MARKETPLACE_NAME)
    plugin_source = os.path.join(source_root, PLUGIN_DIR)
    manifest = _load_json_object(os.path.join(
        plugin_source, ".claude-plugin", "plugin.json"))
    version = str(manifest.get("version") or "")
    if manifest.get("name") != PLUGIN_NAME or not version:
        raise ValueError("invalid bundled Claude plugin manifest")
    cache_dir = os.path.join(seed_dir, "cache", MARKETPLACE_NAME,
                             PLUGIN_NAME, version)

    os.makedirs(os.path.dirname(marketplace_dir), exist_ok=True)
    os.makedirs(os.path.dirname(cache_dir), exist_ok=True)
    materialize_marketplace(marketplace_dir, source_root=source_root, force=True)
    _replace_tree(plugin_source, cache_dir)

    now = _timestamp()
    known_path = os.path.join(seed_dir, "known_marketplaces.json")
    known = _load_json_object(known_path)
    old_market = known.get(MARKETPLACE_NAME)
    old_market = old_market if isinstance(old_market, dict) else {}
    known[MARKETPLACE_NAME] = {
        "source": {"source": "directory", "path": marketplace_dir},
        "installLocation": marketplace_dir,
        "lastUpdated": old_market.get("lastUpdated", now),
    }

    installed_path = os.path.join(seed_dir, "installed_plugins.json")
    installed = _load_json_object(installed_path)
    plugins = installed.get("plugins")
    plugins = dict(plugins) if isinstance(plugins, dict) else {}
    old_entries = plugins.get(PLUGIN_ID)
    old_entry = (old_entries[0] if isinstance(old_entries, list) and
                 old_entries and isinstance(old_entries[0], dict) else {})
    plugins[PLUGIN_ID] = [{
        "scope": "user",
        "installPath": cache_dir,
        "version": version,
        "installedAt": old_entry.get("installedAt", now),
        "lastUpdated": now,
    }]
    installed["version"] = 2
    installed["plugins"] = plugins
    _write_json(known_path, known)
    _write_json(installed_path, installed)
    return seed_dir


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ccc_agent.claude_plugin",
        description="Materialize bundled CCC Claude plugin assets")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--seed-dir", help="build a complete runtime plugin seed")
    destination.add_argument(
        "--write-to", help="write only the local marketplace source")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing --write-to destination")
    args = parser.parse_args(argv)
    if args.seed_dir:
        path = materialize_seed(args.seed_dir)
    else:
        path = materialize_marketplace(args.write_to, force=args.force)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
