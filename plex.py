import urllib.parse
from env import env
from titlecase import titlecase
import requests
from plex_queue import plex_queue

GENRE_IDENTIFIER = "[Genre] "


def collection_name_from_tag(genre):
    return f"{GENRE_IDENTIFIER}{genre}"


def find_collection(genre, section):
    try:
        return section.collection(genre)
    except Exception as e:
        return None


def create_genre_collection(genre, album, section):
    name = collection_name_from_tag(genre)
    print(f"Create {name}")
    section.createCollection(name, items=[album])


def album_in_collection(album, collection):
    return album in collection.items()


def add_album_to_collection(album, collection):
    collection.addItems(album)


def sync_album_genre_to_collection(album, section):
    genres = [g.tag for g in album.genres]
    print("tags:", genres, album.genres)
    for genre in genres:
        name = collection_name_from_tag(genre)
        collection = find_collection(name, section)
        if collection is None:
            collection = create_genre_collection(genre, album, section)
        elif not album_in_collection(album, collection):
            add_album_to_collection(album, collection)


def genre_name_from_collection(collection):
    return collection.replace(GENRE_IDENTIFIER, "")


def remove_from_collections(album, section):
    # TODO could probably filter on GENRE_IDENTIFIER when building array
    collections = album.collections
    genres = [g.tag for g in album.genres]
    print("remove_from_collections", collections, genres)
    for collection in collections:
        collection_name = collection.tag
        if GENRE_IDENTIFIER not in collection_name:
            continue
        name = genre_name_from_collection(collection_name)

        print(name, genres)
        if name not in genres:
            print("remove from", collection_name)
            collection = section.collection(collection_name)
            collection.removeItems([album])


def sync_collection_to_album_genre(album, section):
    collections = [c.tag for c in album.collections]
    genres = [g.tag for g in album.genres]

    genres_to_add = []
    print("collections:", collections, album.collections)
    for collection in collections:
        if GENRE_IDENTIFIER not in collection:
            continue
        name = genre_name_from_collection(collection)
        if name not in genres:
            genres_to_add.append(name)

    if len(genres_to_add) < 1:
        return

    tags = genres + genres_to_add
    set_album_genres(album, tags)
    set_tracks_genres(album, tags, section)


def remove_album_genre(album, genre, section):
    genre = urllib.parse.quote(genre)
    album.batchEdits()

    edits = {}
    edits["genre.locked"] = 1
    edits["style.locked"] = 1

    if len(album.genres) > 0:
        edits["genre[].tag.tag-"] = genre

    if len(album.styles) > 0:
        edits["style[].tag.tag-"] = genre

    album.edit(**edits)
    plex_queue.queue_request(lambda: album.saveEdits(), f"Remove from {album.title}")


def remove_tracks_genre(album, genre, section):
    genre = urllib.parse.quote(genre)
    tracks = album.tracks()

    # We're going rogue here, because Python-PlexAPI doesn't seem to support this.
    params = {
        "type": 10,
        "id": ",".join([f"{t.ratingKey}" for t in tracks]),
        "genre.locked": 1,
        "style.locked": 1,
        "X-Plex-Token": env("PLEX_TOKEN"),
    }

    for track in tracks:
        if len(track.genres) > 0:
            params["genre[].tag.tag-"] = genre

    url = f"{env('PLEX_URL')}/library/sections/{section.key}/all"

    def req():
        response = requests.put(url, params=params)
        if response.status_code == 200:
            print("Processed Plex tracks")
        else:
            print(f"Request failed with status code: {response.status_code}")

    plex_queue.queue_request(req, f"Remove from {album.title} tracks")


def set_tracks_genres(album, tags, section):
    tracks = album.tracks()

    # We're going rogue here, because Python-PlexAPI doesn't seem to support this.
    params = {
        "type": 10,
        "id": ",".join([f"{t.ratingKey}" for t in tracks]),
        "genre.locked": 1,
        "style.locked": 1,
        "X-Plex-Token": env("PLEX_TOKEN"),
    }

    for track in tracks:
        if len(track.genres) > 0:
            params["genre[].tag.tag-"] = ",".join(g.tag for g in track.genres)

    i = 0
    for tag in tags:
        params[f"genre[{i}].tag.tag"] = tag
        i += 1

    url = f"{env('PLEX_URL')}/library/sections/{section.key}/all"

    def req():
        response = requests.put(url, params=params)
        if response.status_code == 200:
            print("Processed Plex tracks")
        else:
            print(f"Request failed with status code: {response.status_code}")

    req()
    # plex_queue.queue_request(req, f"Save {album.title} track genres")


def titlecase_album(album):
    album.editTitle(titlecase(album.title))


def titlecase_tracks(album):
    tracks = album.tracks()
    for track in tracks:
        track.editTitle(titlecase(track.title))


def set_album_genres(album, tags):
    album.batchEdits()

    edits = {}
    edits["genre.locked"] = 1
    edits["style.locked"] = 1

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
    # plex_queue.queue_request(album.saveEdits, f"Save {album.title} genres")
