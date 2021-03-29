import difflib
import logging
from enum import Enum
from os.path import join
from typing import Dict, List, Optional

import httpx
import lyricsgenius as lg
import numpy as np
import pandas as pd
import tekore as tk
from pydantic import BaseModel
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel
from sklearn.preprocessing import MultiLabelBinarizer

from gtr import data_path
from gtr.constants import LASTFM_API_KEY

logger = logging.getLogger("gtr")


class SimpleArtist(BaseModel):
    """An artist without full info"""

    id: int
    name: str

    def __repr__(self):
        return f"SimpleArtist(id={self.id})"


class Artist(SimpleArtist):
    """A Artist from the Recommender"""

    description: str

    def __repr__(self):
        return f"Artist(id={self.id})"


class Preferences(BaseModel):
    """User Preferences"""

    genres: List[str]
    artists: List[str]

    def __repr__(self):
        return "Preferences(genres=[{genres}], artists=[{artists}])".format(
            genres=", ".join(self.genres),
            artists=", ".join(self.artists),
        )


class Song(BaseModel):
    """A Song from the Recommender"""

    id: int
    artist: str
    name: str
    genres: List[str]
    id_spotify: Optional[str]
    isrc: Optional[str]
    cover_art: Optional[str]
    preview_url: Optional[str]
    download_url: Optional[str]

    def __repr__(self):
        return f"Song(id={self.id})"


class SongType(str, Enum):
    any = "any"
    any_file = "any_file"
    preview = "preview"
    full = "full"
    preview_full = "preview,full"


class Recommender:
    """GeniusT Recommender

    Available genres:
    classical,  country, instrumental, persian, pop, rap, rnb, rock, traditional
    """

    def __init__(self):
        # Read tracks
        logger.debug("Reading songs from CSV files")
        en = pd.read_csv(join(data_path, "tracks en.csv"))
        fa = pd.read_csv(join(data_path, "tracks fa.csv"))
        self.songs: pd.DataFrame = pd.merge(
            en.drop(columns=["download_url"]), fa, how="outer"
        )
        self.songs.replace({np.NaN: None}, inplace=True)
        self.num_songs = len(self.songs)

        # Read artists
        logger.debug("Reading artists from CSV files")
        en_artists = pd.read_csv(join(data_path, "artists en.csv"))
        fa_artists = pd.read_csv(join(data_path, "artists fa.csv"))
        self.artists: pd.DataFrame = pd.merge(en_artists, fa_artists, how="outer")
        self.artists["description"] = self.artists["description"].str.replace(r"\n", "")
        self.artists.description.fillna("", inplace=True)

        self.artists_names = self.artists.name.to_list()
        self.lowered_artists_names = {
            name.lower(): {
                "id": i,
                "name": name,
            }
            for i, name in enumerate(self.artists_names)
        }
        # No duplicate values
        # no_duplicates = songs['id_spotify'].dropna().duplicated(
        # ).value_counts().all(False)
        # assert no_duplicates, True

        self.songs["genres"] = self.songs["genres"].str.split(",")
        songs_copy = self.songs.copy()
        # One-hot encode genres
        logger.debug("One-hot encoding genres")
        mlb = MultiLabelBinarizer(sparse_output=True)
        df = songs_copy.join(
            pd.DataFrame.sparse.from_spmatrix(
                mlb.fit_transform(songs_copy.pop("genres")),
                index=songs_copy.index,
                columns=mlb.classes_,
            )
        )
        self.binarizer = mlb
        # Convert df to numpy array
        numpy_df = df.drop(
            columns=[
                "id_spotify",
                "artist",
                "name",
                "download_url",
                "preview_url",
                "isrc",
                "cover_art",
            ]
        )
        self.genres = list(numpy_df.columns)
        self.genres_by_number = {}
        for i, genre in enumerate(self.genres):
            self.genres_by_number[i] = genre
        # dtype=[(genre, int) for genre in self.genres]
        self.numpy_songs = numpy_df.to_numpy()

        logger.debug("Creating TFIDS vectors")
        with open(join(data_path, "persian_stopwords.txt"), "r", encoding="utf8") as f:
            PERSIAN_STOP_WORDS = f.read().strip().split()
        stop_words = list(ENGLISH_STOP_WORDS) + PERSIAN_STOP_WORDS
        self.tfidf = TfidfVectorizer(analyzer="word", stop_words=stop_words)
        self.tfidf = self.tfidf.fit_transform(self.artists["description"])

        # based on https://www.statista.com/statistics/253915/
        # favorite-music-genres-in-the-us/
        self.genres_by_age_group: Dict[int, List[str]] = {
            19: ["pop", "rap", "rock"],
            24: ["pop", "rap", "rock"],
            34: ["pop", "rock", "rap", "country", "traditional"],
            44: ["pop", "rock", "rap", "country", "traditional"],
            54: ["rock", "pop", "country", "traditional"],
            64: ["rock", "country", "traditional"],
            65: ["rock", "country", "traditional"],
        }
        logger.debug("Recommender initialization successful.")

    def artist(self, id: int) -> Artist:
        """Gets Artist

        Args:
            id (int): Artist ID.

        Returns:
            Artist: Artist info.
        """
        row = self.artists.iloc[id]
        return Artist(id=id, **row.to_dict())

    def genres_by_age(self, age: int) -> List[str]:
        """Returns genres based on age group

        Args:
            age (int): User's age.

        Returns:
            List[str]: List of corresponding genres which is
                the first list with a smaller number than
                the age parameter (e.g. 29 would correspond to genres_by_age[24])
        """
        age_groups = list(self.genres_by_age_group.keys())
        for age_group in age_groups:
            if age >= age_group:
                break
        else:
            age_group = age_groups[-1]
        return self.genres_by_age_group[age_group]

    async def preferences_from_platform(
        self, token: str, platform: str
    ) -> Optional[Preferences]:
        """Processes user's data from Spotify/Genius to generate preferences

        Processes activity data from Spotify/Genius to generate user's favorite
        artists and genres. In case of insufficient data, it returns None.

        Args:
            token (str): Token used to log into platform.
            platform (str): Platform to get data from

        Returns:
            Optional[Preferences]: Generated preferences if sufficient data
                is avaialbe, else None.
        """
        genres = []
        artists = []
        if platform == "genius":
            user_genius = lg.Genius(token)
            account = user_genius.account()["user"]
            pyongs = user_genius.user_pyongs(account["id"])
            pyonged_songs = []
            for contribution in pyongs["contribution_groups"]:
                pyong = contribution["contributions"][0]
                if pyong["pyongable_type"] == "song":
                    api_path = pyong["pyongable"]["api_path"]
                    song_id = int(api_path[api_path.rfind("/") + 1 :])
                    pyonged_songs.append(song_id)

            public_genius = lg.PublicAPI(timeout=10)

            for song_id in pyonged_songs:
                song = public_genius.song(song_id)["song"]
                artists.append(song["primary_artist"]["name"])
                for tag in song["tags"]:
                    for genre in self.genres:
                        if genre in tag["name"].lower():
                            genres.append(genre)
        else:
            user_spotify = tk.Spotify(token, sender=tk.RetryingSender())
            top_tracks = await user_spotify.current_user_top_tracks("short_term")
            top_artists = await user_spotify.current_user_top_artists(limit=5)
            user_spotify.close()

            # Add track genres to genres list
            params = {
                "method": "Track.getTopTags",
                "api_key": LASTFM_API_KEY,
                "format": "json",
            }
            lastfm_api_root = "http://ws.audioscrobbler.com/2.0/"
            async with httpx.AsyncClient() as client:
                for track in top_tracks.items:
                    params.update({"artist": track.artists[0], "track": track.name})
                    res = await client.get(lastfm_api_root, params=params)
                    track_genres = res.json()
                    if "toptags" in track_genres:
                        for tag in track_genres["toptags"]["tag"]:
                            for genre in self.genres:
                                if genre in tag["name"].lower():
                                    genres.append(genre)

            artists = [artist.name for artist in top_artists.items]

        # get count of genres and only keep genres with a >=30% occurance
        unique_elements, counts_elements = np.unique(genres, return_counts=True)
        counts_elements = counts_elements.astype(float)
        counts_elements /= counts_elements.sum()
        user_genres = []
        found_artists = []
        genres, percentage = np.asarray((unique_elements, counts_elements)).tolist()
        for i, genre in enumerate(genres):
            if float(percentage[i]) > 0.3:
                user_genres.append(genre)

        # find user artists in recommender artists
        if user_genres:
            for artist in artists:
                found_artist = self.artists[self.artists.name == artist].name.values
                if found_artist.size > 0:
                    found_artists.append(found_artist[0])

        return (
            Preferences(genres=user_genres, artists=found_artists) if genres else None
        )

    def search_artist(self, artist: str) -> List[SimpleArtist]:
        """Searches for artist in artists

        Args:
            artist (str): Artist.

        Returns:
            List[str]: List of possible matches.
        """
        artist = artist.lower()
        matches = difflib.get_close_matches(artist, self.lowered_artists_names.keys())
        return [SimpleArtist(**self.lowered_artists_names[m]) for m in matches]

    # def search_song(self, song: str):
    #     raise NotImplementedError()

    def binarize(self, genres: List[str]) -> np.ndarray:
        """Converts genres to an array of ones and zeroes.

        Args:
            genres (List[str]): Genres.

        Returns:
            np.ndarray: Numpy array of ones and zeroes
            (one if user has that genre, else zero).
        """
        return self.binarizer.transform([genres]).toarray()[0]

    def shuffle(
        self,
        user_preferences: Preferences,
        # language: str = 'any',
        song_type="any",
    ) -> List[Song]:
        """Generates song recommendations based on preferences

        Args:
            user_preferences (Preferences): User's favorite genres and artists.
            song_type (str, optional): Type of song to include in recommendations.
                Can be one of "any", "any_file", "preview", "full"
                or "preview,full". Defaults to "any".

        Returns:
            List[Song]: List of recommended Songs.
        """
        user_genres = self.binarize(user_preferences.genres)
        persian_index = np.where(self.binarize(["persian"]) == 1)[0][0]
        persian_user = True if user_genres[persian_index] == 1 else False
        similar = []
        for index, song in enumerate(self.numpy_songs):
            # skip song if it doesn't match user language
            if bool(song[persian_index]) != persian_user:
                continue
            for i, genre in enumerate(song):
                if i != persian_index and genre == 1 and user_genres[i] == 1:
                    similar.append(index)
                    break
            # no need for language parameter since
            # the first if statement enforeces the value of "persian" genre
            #        if ((language == 'any')
            #            or (language == 'en' and song[persian_index] == 0)
            #                or (language == 'fa' and song[persian_index] == 1)):

        # Randomly choose 20 songs from similar songs
        # This is to avoid sending the same set of songs each time
        if similar:
            logger.debug
            rng = np.random.default_rng()
            selected = rng.choice(
                similar,
                20,
            )  # TODO: set probability array
            logger.debug(
                "Selected songs. Entropy: %s - Preferences: %s",
                rng._bit_generator._seed_seq.entropy,
                user_preferences,
            )
        else:
            logger.debug("No similar songs for %s", user_preferences)
            return []

        def is_valid(song: Song) -> bool:
            is_valid = False
            if song_type == SongType.any:
                is_valid = True
            elif song_type == SongType.any_file:
                if song.preview_url or song.download_url:
                    is_valid = True
            elif song_type == SongType.preview:
                if song.preview_url:
                    is_valid = True
            elif song_type == SongType.full:
                if song.download_url:
                    is_valid = True
            else:
                if song.preview_url and song.download_url:
                    is_valid = True
            return is_valid

        # sort songs by most similar song artists to user artists
        user_artists = [
            self.artists[self.artists.name == artist]
            for artist in user_preferences.artists
        ]
        if user_artists:
            song_artists = [
                self.artists[self.artists.name == self.songs.loc[song].artist]
                for song in selected
            ]
            cosine_similarities = []
            user_tfifd = self.tfidf[[artist.index[0] for artist in user_artists], :]
            user_artists_names = [x.name.values[0] for x in user_artists]
            for index, artist in enumerate(song_artists):
                cosine_similarity = (
                    linear_kernel(self.tfidf[artist.index[0]], user_tfifd)
                    .flatten()
                    .sum()
                )
                if artist.name.values[0] in user_artists_names:
                    cosine_similarity += 1
                cosine_similarities.append((index, cosine_similarity))
            cosine_similarities.sort(key=lambda x: x[1], reverse=True)
            hits = []
            for row in cosine_similarities:
                id = selected[row[0]]
                song = self.song(id)
                if is_valid(song):
                    hits.append(song)
                if len(hits) == 5:
                    break
        else:
            hits = []
            for index in selected:
                song = self.song(index)
                if is_valid(song):
                    hits.append(song)
                if len(hits) == 5:
                    break

        return hits

    def song(self, id: int = None, id_spotify: str = None) -> Song:
        """Gets Song from its ID or Spotify ID

        You must pass either id or spotify_id.

        Args:
            id (int, optional): Song ID. Defaults to None.
            id_spotify (str, optional): Song's Spotify ID. Defaults to None.

        Raises:
            AssertionError: If neither id nor spotify_id is passed.
            If both are supplied, id is used.

        Returns:
            Song: Song info.
        """
        if not any([id is not None, id_spotify]):
            raise AssertionError("Must supply either id or id_spotify.")
        if id:
            row = self.songs.iloc[id]
        else:
            rows = self.songs[self.songs.id_spotify == id_spotify]
            id = int(rows.index[0])
            row = rows.iloc[0]
        return Song(id=id, **row.to_dict())
