@echo off

if not exist config mkdir config
if not exist config\config.yml copy default_config.yml config\config.yml
if not exist config\token.txt type nul > config\token.txt

if not exist data mkdir data
python -c "import pathlib, sqlite3; conn = sqlite3.connect('data/data.db'); conn.executescript(pathlib.Path('schema.sql').read_text(encoding='utf-8')); conn.commit(); conn.close()"

echo OpenPOTD files are ready. Put your Discord bot token in config\token.txt.
