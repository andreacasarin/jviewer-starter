#! /usr/bin/env bash

set -euo pipefail

script_path="${BASH_SOURCE[0]}"
while [[ -L "$script_path" ]]; do
    script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
    script_path="$(readlink "$script_path")"
    [[ "$script_path" = /* ]] || script_path="$script_dir/$script_path"
done
script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
cd "$script_dir"

if command -v xhost >/dev/null 2>&1; then
    xhost +localhost
elif [[ -x /opt/X11/bin/xhost ]]; then
    /opt/X11/bin/xhost +localhost
else
    echo "xhost not found. On macOS, install XQuartz from https://www.xquartz.org/ and start it before running this script." >&2
    exit 1
fi

docker compose build
docker compose run --rm --entrypoint="" jviewer /usr/local/bin/jviewer-starter.py "$@"
