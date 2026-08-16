from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))
import store

with tempfile.TemporaryDirectory() as directory:
    database = store.Store(Path(directory))
    with database.connect() as connection:
        connection.execute("INSERT INTO peers VALUES (1,'one','personal',NULL,1,0,'','','now')")
    with database.connect(write=False) as connection:
        assert connection.execute("SELECT title FROM peers").fetchone()[0] == "one"
        connection.execute("UPDATE peers SET title='two'")
    with database.connect(write=False) as connection:
        assert connection.execute("SELECT title FROM peers").fetchone()[0] == "one"
