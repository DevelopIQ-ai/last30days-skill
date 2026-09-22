#!/usr/bin/env bash
# Mirrors skills/last30days/scripts/{last30days.py,lib/} into mcp/vendored/
# so the Go binary's embed.FS captures the engine at build time.
#
# Source of truth: skills/last30days/scripts/. Never edit mcp/vendored/ directly.
# Run before `go build` locally and in CI before `printing-press bundle`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MCP_DIR}/.." && pwd)"
ENGINE_SRC="${REPO_ROOT}/skills/last30days/scripts"
# Embed path must live inside the consuming package (Go //go:embed cannot
# reach outside its own directory tree), so vendored/ sits under engine/.
VENDORED="${MCP_DIR}/internal/engine/vendored"

if [ ! -f "${ENGINE_SRC}/last30days.py" ]; then
  echo "sync-engine: ${ENGINE_SRC}/last30days.py not found" >&2
  exit 1
fi

mkdir -p "${VENDORED}"
# Clear stale content while keeping the .gitkeep that anchors the embed path.
find "${VENDORED}" -mindepth 1 -not -name ".gitkeep" -delete

# Copy the entry script and the lib/ tree (modules + lib/vendor/).
cp "${ENGINE_SRC}/last30days.py" "${VENDORED}/last30days.py"
cp -R "${ENGINE_SRC}/lib" "${VENDORED}/lib"

# Vendor the plugin manifest too. The engine reports its own version by
# walking up from lib/render.py looking for .claude-plugin/plugin.json (then
# SKILL.md). In a normal install both sit above scripts/; in the extracted MCP
# cache neither exists, so every report rendered through the MCP server
# announced itself as "last30days v?". Copying the real manifest fixes that
# through the mechanism the engine already has, with no engine change and no
# second place to record the version.
#
# //go:embed all:vendored keeps dot-directories, so .claude-plugin survives
# the embed.
MANIFEST_SRC="${REPO_ROOT}/.claude-plugin/plugin.json"
if [ -f "${MANIFEST_SRC}" ]; then
  mkdir -p "${VENDORED}/.claude-plugin"
  cp "${MANIFEST_SRC}" "${VENDORED}/.claude-plugin/plugin.json"
else
  echo "sync-engine: ${MANIFEST_SRC} not found; engine will report version '?'" >&2
fi

# Strip caches so the embed.FS stays deterministic.
find "${VENDORED}" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "${VENDORED}" -type f -name "*.pyc" -delete

echo "sync-engine: vendored engine at ${VENDORED}"
