@echo off

if not exist data mkdir data
if not exist data\config mkdir data\config
if not exist data\config\config.yml copy default_config.yml data\config\config.yml
if not exist data\config\token.txt type nul > data\config\token.txt
if not exist data\config\blacklist.txt type nul > data\config\blacklist.txt
python -c "import pathlib, sqlite3; conn = sqlite3.connect('data/data.db'); conn.executescript(pathlib.Path('schema.sql').read_text(encoding='utf-8')); conn.commit(); conn.close()"

echo OpenPOTD files are ready. Put your Discord bot token in data\config\token.txt.
