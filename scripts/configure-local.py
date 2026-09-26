"""Bootstrap this project's own configuration from explicitly available session credentials.
Does not import, read, or execute any other project's code/configuration/token files.
Never prints credential values. Existing project config is preserved.
"""

import os
from pathlib import Path
from dotenv import set_key

p = Path(".env")
if p.exists():
    print("Existing project .env preserved")
else:
    p.touch(mode=0o600)
    config = {
        "OD_DATABASE_URL": "postgresql+psycopg://optiondesk:optiondesk@127.0.0.1:55432/optiondesk_live",
        "OD_MODE": "live",
        "OD_PROVIDERS": "opend",
        "OD_PORT": "8765",
        "OD_MAX_CONTRACTS": "30",
        "OD_REFRESH_SECONDS": "60",
        "OD_OPEND_HOST": "127.0.0.1",
        "OD_OPEND_PORT": "11111",
    }
    # Public channel IDs from the reviewed source adapter, now owned by this project config.
    channels = "1281228073597141054,1394690850394472448"
    if os.environ.get("DISCORD_TOKEN_MIRANDA"):
        config["OD_DISCORD_TOKEN"] = os.environ["DISCORD_TOKEN_MIRANDA"]
        config["OD_DISCORD_CHANNELS"] = channels
    if os.environ.get("MONGODB_URI"):
        config["OD_MONGO_URI"] = os.environ["MONGODB_URI"]
        config["OD_MONGO_DB"] = "wfreedom"
        config["OD_MONGO_COLLECTION"] = "tweets"
        config["OD_MONGO_CHANNELS"] = channels
    for key, val in config.items():
        set_key(str(p), key, val)
    os.chmod(p, 0o600)
    print(
        "Independent configuration created. Enabled providers: OpenD. Credential values not displayed."
    )
