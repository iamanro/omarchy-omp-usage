#!/usr/bin/env bash
# Copy the plugin into the Omarchy shell plugin directory and reload the shell.
# A copy, not a symlink: the shell's plugin watcher does not follow symlinks.
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.config/omarchy/plugins/iamanro.omp-usage"

if [[ "$src" -ef "$dest" ]]; then
  echo "Already running from $dest; use 'omarchy plugin update iamanro.omp-usage' instead." >&2
  exit 1
fi

omarchy plugin validate "$src"
rm -rf "$dest"
mkdir -p "$dest/bin"
cp "$src/manifest.json" "$src/Panel.qml" "$src/I18n.js" "$src/README.md" "$dest/"
cp "$src/bin/omp_usage.py" "$dest/bin/"
cp -r "$src/assets" "$dest/"

if ! grep -q '"iamanro.omp-usage"' "$HOME/.config/omarchy/shell.json" 2>/dev/null; then
  omarchy plugin enable iamanro.omp-usage --before omarchy.agents
fi
# Already-loaded QML is cached; a restart is the reliable way to pick up changes.
omarchy restart shell
