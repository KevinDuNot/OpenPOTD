#!/bin/bash
set -euo pipefail

mkdir -p data/config

if [ ! -f data/config/config.yml ]; then
  cp default_config.yml data/config/config.yml
fi

if [ ! -f data/config/token.txt ]; then
  : > data/config/token.txt
fi

if [ ! -f data/config/blacklist.txt ]; then
  : > data/config/blacklist.txt
fi

python3 -c "import pathlib, sqlite3; conn = sqlite3.connect('data/data.db'); conn.executescript(pathlib.Path('schema.sql').read_text(encoding='utf-8')); conn.commit(); conn.close()"

echo "OpenPOTD files are ready. Put your Discord bot token in data/config/token.txt."
