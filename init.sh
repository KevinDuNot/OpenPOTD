#!/bin/bash
set -euo pipefail

mkdir -p config data

if [ ! -f config/config.yml ]; then
  cp default_config.yml config/config.yml
fi

if [ ! -f config/token.txt ]; then
  : > config/token.txt
fi

python3 -c "import pathlib, sqlite3; conn = sqlite3.connect('data/data.db'); conn.executescript(pathlib.Path('schema.sql').read_text(encoding='utf-8')); conn.commit(); conn.close()"

echo "OpenPOTD files are ready. Put your Discord bot token in config/token.txt."
