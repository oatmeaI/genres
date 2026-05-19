from util import disable_requests_logging, enable_requests_logging
from concurrent.futures import ThreadPoolExecutor, as_completed


def process_track(album, track, tags):
    print(
        f"processing Plex track: {track.title} ({track.trackNumber} / {album.leafCount})"
    )

    track.batchEdits()

    edits = {}
    if len(track.genres) > 0:
        edits["genre[].tag.tag-"] = ",".join(g.tag for g in track.genres)

    i = 0
    for tag in tags:
        edits[f"genre[{i}].tag.tag"] = tag
        edits[f"style[{i}].tag.tag"] = tag
        i += 1

    track.edit(**edits)
    track.saveEdits()


def set_tracks_genres(album, tags):
    tracks = album.tracks()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(process_track, album, track, tags): track
            for track in tracks
        }

        for future in as_completed(futures):
            track = futures[future]
            try:
                future.result()  # This will raise any exception that occurred during processing
            except Exception as e:
                print(f"Error processing track {track.title}: {e}")


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
