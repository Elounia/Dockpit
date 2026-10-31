#!/bin/sh
set -eu
# Use Tailwind CSS v3.4.17 standalone CLI, or npx tailwindcss@3.4.17.
# Runtime installations do not need Node, npm or the Tailwind CLI.
cd "$(dirname "$0")/.."
"${TAILWINDCSS:-tailwindcss}" -i dockpit/static/input.css -o dockpit/static/app.css \
  --content 'dockpit/static/index.html,dockpit/static/app.js' --minify
