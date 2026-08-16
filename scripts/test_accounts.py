from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent))
import accounts
import keychain
from store import Store


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    # A legacy index is copied once into the account-specific index.
    legacy = Store(root)
    with legacy.connect() as db:
        db.execute("INSERT INTO peers VALUES (7,'legacy','personal',NULL,1,0,'','','now')")
    original_get, original_put = keychain.get, keychain.put
    saved: dict[str, str] = {}
    keychain.get = lambda service: {keychain.OWNER_ID: "100", keychain.SESSION: "session"}.get(service)
    keychain.put = lambda service, value: saved.setdefault(service, value)
    try:
        assert accounts.active(root)["id"] == "100"
        with Store(root, "100").connect(write=False) as db:
            assert db.execute("SELECT title FROM peers").fetchone()[0] == "legacy"
    finally:
        keychain.get, keychain.put = original_get, original_put

    accounts.add(root, 200, "Second", "old")
    with Store(root, "200").connect() as db:
        db.execute("INSERT INTO peers VALUES (7,'separate','personal',NULL,1,0,'','','now')")
    assert accounts.active(root)["id"] == "200"
    accounts.use(root, 100)
    assert accounts.comment(root, 100, "main")["comment"] == "main"
    with Store(root, "100").connect(write=False) as db:
        assert db.execute("SELECT title FROM peers").fetchone()[0] == "legacy"
    assert accounts.disconnect(root, 100)["active_id"] == "200"
