import os


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v
