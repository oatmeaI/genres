def set_tracks_genres(album, tags):
    album.reload()
    tracks = album.tracks()

    for track in tracks:
        print(
            f"processing Plex track: {track.title} ({track.trackNumber} / {album.leafCount})"
        )
        track.reload()
        track.batchEdits()

        for g in list(track.genres or []):
            track.removeGenre(g.tag)

        track.saveEdits()
        track.reload()

        track.addGenre(tags)
        track.reload()


def set_album_genres(album, tags):
    album.reload()
    album.batchEdits()

    for g in list(album.genres or []):
        album.removeGenre(g.tag)

    album.saveEdits()
    album.reload()

    album.addStyle(tags)
    album.addGenre(tags)
    album.reload()
