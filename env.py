import os
from dotenv import load_dotenv

load_dotenv(override=True)


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def flag(name: str, default: bool) -> bool:
    return bool(env(name, "" if not default else "true"))
