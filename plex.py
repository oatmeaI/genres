from env import env
import requests


def set_tracks_genres(album, tags):
    tracks = album.tracks()

    # We're going rogue here, because Python-PlexAPI doesn't seem to support this.
    params = {
        "type": 10,
        "id": ",".join([f"{t.ratingKey}" for t in tracks]),
        "genre.locked": 1,
        "X-Plex-Token": env("PLEX_TOKEN"),
    }

    for track in tracks:
        if len(track.genres) > 0:
            params["genre[].tag.tag-"] = ",".join(g.tag for g in track.genres)

    i = 0
    for tag in tags:
        params[f"genre[{i}].tag.tag"] = tag
        i += 1

    url = f"{env('PLEX_URL')}/library/sections/1/all"
    response = requests.put(url, params=params)
    if response.status_code == 200:
        print("Processed Plex tracks")
    else:
        print(f"Request failed with status code: {response.status_code}")


def set_album_genres(album, tags):
    album.batchEdits()

    edits = {}

    if len(album.genres) > 0:
        edits["genre[].tag.tag-"] = ",".join(g.tag for g in album.genres)

    if len(album.styles) > 0:
        edits["style[].tag.tag-"] = ",".join(g.tag for g in album.styles)

    i = 0
    for tag in tags:
        edits[f"genre[{i}].tag.tag"] = tag
        edits[f"style[{i}].tag.tag"] = tag
        i += 1

    album.edit(**edits)
    album.saveEdits()
