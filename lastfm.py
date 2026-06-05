import requests
import json
from env import env
from rym import get_rym_hierarchy
from rym_hierarchy import expand_genre_picks, resolve_one


def get_album_tags(album):
    params = {
        "artist": album.parentTitle,
        "album": album.title,
        "api_key": env("LAST_FM_KEY"),
        "method": "album.gettoptags",
        "format": "json",
    }
    url = "http://ws.audioscrobbler.com/2.0"
    response = requests.get(url, params=params)
    content = json.loads(response.content)

    rym = get_rym_hierarchy()
    if rym is None:
        return
    tags = content["toptags"]["tag"]
    tag_names = []
    for tag in tags:
        tag_names.append(tag["name"])
    rym_genres, unknown = expand_genre_picks(tag_names, rym.nodes)
    rym_tags = []
    for genre in rym_genres:
        rym_tags.append({"tag": genre})
    return rym_tags
