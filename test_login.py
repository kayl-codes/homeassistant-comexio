import asyncio
import sys
import logging

# Logging für das Terminal einschalten
logging.basicConfig(level=logging.DEBUG)

try:
    from api import ComexioAPI
except ImportError as e:
    print(f"Fehler: api.py nicht gefunden! {e}")
    sys.exit(1)

async def test():
    print("Starte Test...")
    try:
        # Ersetze admin/passwort durch deine echten Daten
        api = ComexioAPI(None, "192.168.0.250", "admin", " ")
        success = await api.login()
        print(f"--- ERGEBNIS ---")
        print(f"Login Erfolg: {success}")
        if success:
            config = await api.get_raw_config()
            print(f"Gefundene Sektionen: {list(config.keys())}")
        await api.close()
    except Exception as e:
        print(f"Ein Fehler ist aufgetreten: {e}")

if __name__ == "__main__":
    asyncio.run(test())
