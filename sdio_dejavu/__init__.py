import multiprocessing
import os
import sys
import traceback
from itertools import groupby
import time
from typing import Dict, List, Tuple
import numpy as np
from hashlib import sha1
import logging
import concurrent
from concurrent.futures import ProcessPoolExecutor, as_completed
import sdio_dejavu.logic.decoder as decoder
from sdio_dejavu.base_classes.base_database import get_database
from sdio_dejavu.config.settings import (DEFAULT_FS, DEFAULT_OVERLAP_RATIO,
                                    DEFAULT_WINDOW_SIZE, FIELD_FILE_SHA1,
                                    FIELD_TOTAL_HASHES,
                                    FINGERPRINTED_CONFIDENCE,
                                    FINGERPRINTED_HASHES, HASHES_MATCHED,
                                    INPUT_CONFIDENCE, INPUT_HASHES, OFFSET,
                                    OFFSET_SECS, SONG_ID, SONG_NAME, TOPN)
from sdio_dejavu.logic.fingerprint import fingerprint
import logging
logger = logging.getLogger(__name__)

class Dejavu:
    def __init__(self, config):
        self.config = config

        # initialize db
        db_cls = get_database(config.get("database_type", "mysql").lower())

        self.db = db_cls(**config.get("database", {}))
        self.db.setup()

        # if we should limit seconds fingerprinted,
        # None|-1 means use entire track
        self.limit = self.config.get("fingerprint_limit", None)
        if self.limit == -1:  # for JSON compatibility
            self.limit = None
        self.__load_fingerprinted_audio_hashes()

    def __load_fingerprinted_audio_hashes(self) -> None:
        """
        Keeps a dictionary with the hashes of the fingerprinted songs, in that way is possible to check
        whether or not an audio file was already processed.
        """
        # get songs previously indexed
        self.songs = self.db.get_songs()
        self.songhashes_set = set()  # to know which ones we've computed before
        for song in self.songs:
            song_hash = song[FIELD_FILE_SHA1]
            self.songhashes_set.add(song_hash)

    def get_fingerprinted_songs(self) -> List[Dict[str, any]]:
        """
        To pull all fingerprinted songs from the database.

        :return: a list of fingerprinted audios from the database.
        """
        return self.db.get_songs()

    def delete_songs_by_id(self, song_ids: List[int]) -> None:
        """
        Deletes all audios given their ids.

        :param song_ids: song ids to delete from the database.
        """
        self.db.delete_songs_by_id(song_ids)
    
    def fingerprint_media_list(
        self, 
        media_list: list[str], 
        nprocesses: int | None = None
        ):
        nprocesses = nprocesses or max(1, min(4, os.cpu_count() or 2))
        failed_files = []

        # Build work items early to avoid surprises from generator laziness
        worker_input = [
            (filename, self.limit)
            for filename in media_list
            if decoder.unique_hash(filename) not in self.songhashes_set
        ]
        logging.info(f"[FP] submitting {len(worker_input)} files with {nprocesses} processes")

        start_time = time.perf_counter()
        submitted = {}
        completed = 0

        # Small chunks keep latency predictable
        timeout_s = 120  # per-file guard; tune to your media
        with ProcessPoolExecutor(max_workers=nprocesses, mp_context=multiprocessing.get_context("spawn")) as ex:
            futures = []
            for item in worker_input:
                fn = item[0]
                fut = ex.submit(Dejavu._fingerprint_worker, item)
                submitted[fut] = fn
                futures.append(fut)
                logging.debug(f"[FP] submitted: {fn}")

            for fut in as_completed(futures, timeout=None):
                fn = submitted[fut]
                try:
                    song_name, hashes, file_hash = fut.result(timeout=timeout_s)
                    # DB writes in parent only (good). Wrap in retry + timeout.
                    with self.db.cursor() as cur:
                        sid = self.db.insert_song(song_name, file_hash, len(hashes))
                        self.db.insert_hashes(sid, hashes)
                        self.db.set_song_fingerprinted(sid)
                    completed += 1
                    if completed % 50 == 0:
                        logging.info(f"[FP] progress: {completed}/{len(futures)} (elapsed {time.perf_counter()-start_time:.1f}s)")
                except concurrent.futures.TimeoutError:
                    logging.error(f"[FP] timeout: {fn}")
                    failed_files.append(fn)
                except Exception as e:
                    logging.exception(f"[FP] failed: {fn} | {e}")
                    failed_files.append(fn)

        logging.info(f"[FP] done. ok={completed} fail={len(failed_files)} elapsed={time.perf_counter()-start_time:.1f}s")
        return failed_files



    def fingerprint_directory(self, path: str, extensions: str, nprocesses: int = None) -> None:
        """
        Given a directory and a set of extensions it fingerprints all files that match each extension specified.

        :param path: path to the directory.
        :param extensions: list of file extensions to consider.
        :param nprocesses: amount of processes to fingerprint the files within the directory.
        """
        # Try to use the maximum amount of processes if not given.
        try:
            nprocesses = nprocesses or multiprocessing.cpu_count()
        except NotImplementedError:
            nprocesses = 1
        else:
            nprocesses = 1 if nprocesses <= 0 else nprocesses

        pool = multiprocessing.Pool(nprocesses)

        filenames_to_fingerprint = []
        for filename, _ in decoder.find_files(path, extensions):
            # don't refingerprint already fingerprinted files
            if decoder.unique_hash(filename) in self.songhashes_set:
                print(f"{filename} already fingerprinted, continuing...")
                continue

            filenames_to_fingerprint.append(filename)

        # Prepare _fingerprint_worker input
        worker_input = list(zip(filenames_to_fingerprint, [self.limit] * len(filenames_to_fingerprint)))

        # Send off our tasks
        iterator = pool.imap_unordered(Dejavu._fingerprint_worker, worker_input)

        # Loop till we have all of them
        while True:
            try:
                song_name, hashes, file_hash = next(iterator)
            except multiprocessing.TimeoutError:
                continue
            except StopIteration:
                break
            except Exception:
                print("Failed fingerprinting")
                # Print traceback because we can't reraise it here
                traceback.print_exc(file=sys.stdout)
            else:
                sid = self.db.insert_song(song_name, file_hash, len(hashes))

                self.db.insert_hashes(sid, hashes)
                self.db.set_song_fingerprinted(sid)
                self.__load_fingerprinted_audio_hashes()

        pool.close()
        pool.join()

    def fingerprint_file(self, file_path: str, song_name: str = None) -> None:
        """
        Given a path to a file the method generates hashes for it and stores them in the database
        for later be queried.

        :param file_path: path to the file.
        :param song_name: song name associated to the audio file.
        """
        song_name_from_path = decoder.get_audio_name_from_path(file_path)
        song_hash = decoder.unique_hash(file_path)
        song_name = song_name or song_name_from_path
        # don't refingerprint already fingerprinted files
        if song_hash in self.songhashes_set:
            print(f"{song_name} already fingerprinted, continuing...")
        else:
            song_name, hashes, file_hash = Dejavu._fingerprint_worker(
                file_path,
                self.limit,
                song_name=song_name
            )
            sid = self.db.insert_song(song_name, file_hash)

            self.db.insert_hashes(sid, hashes)
            self.db.set_song_fingerprinted(sid)
            self.__load_fingerprinted_audio_hashes()

    def generate_fingerprints(self, samples: List[int], Fs=DEFAULT_FS) -> Tuple[List[Tuple[str, int]], float]:
        f"""
        Generate the fingerprints for the given sample data (channel).

        :param samples: list of ints which represents the channel info of the given audio file.
        :param Fs: sampling rate which defaults to {DEFAULT_FS}.
        :return: a list of tuples for hash and its corresponding offset, together with the generation time.
        """
        t = time()
        hashes = fingerprint(samples, Fs=Fs)
        fingerprint_time = time() - t
        return hashes, fingerprint_time

    def find_matches(self, hashes: List[Tuple[str, int]]) -> Tuple[List[Tuple[int, int]], Dict[str, int], float]:
        """
        Finds the corresponding matches on the fingerprinted audios for the given hashes.

        :param hashes: list of tuples for hashes and their corresponding offsets
        :return: a tuple containing the matches found against the db, a dictionary which counts the different
         hashes matched for each song (with the song id as key), and the time that the query took.

        """
        t = time()
        matches, dedup_hashes = self.db.return_matches(hashes)
        query_time = time() - t

        return matches, dedup_hashes, query_time

    def align_matches(self, matches: List[Tuple[int, int]], dedup_hashes: Dict[str, int], queried_hashes: int,
                      topn: int = TOPN,confidence_threshold:float = 0.05) -> List[Dict[str, any]]:
        """
        Finds hash matches that align in time with other matches and finds
        consensus about which hashes are "true" signal from the audio.

        :param matches: matches from the database
        :param dedup_hashes: dictionary containing the hashes matched without duplicates for each song
        (key is the song id).
        :param queried_hashes: amount of hashes sent for matching against the db
        :param topn: number of results being returned back.
        :return: a list of dictionaries (based on topn) with match information.
        """
        # count offset occurrences per song and keep only the maximum ones.
        sorted_matches = sorted(matches, key=lambda m: (m[0], m[1]))
        counts = [(*key, len(list(group))) for key, group in groupby(sorted_matches, key=lambda m: (m[0], m[1]))]
        songs_matches = sorted(
            [max(list(group), key=lambda g: g[2]) for key, group in groupby(counts, key=lambda count: count[0])],
            key=lambda count: count[2], reverse=True
        )

        songs_result = []
        #print(f"total {len(songs_matches)} songs matches found")
        # Preload all songs into a dictionary
        all_songs = {s[SONG_ID]: s for s in self.db.get_songs()}

        for song_id, offset, _ in songs_matches:  # consider topn elements in the result
            song = all_songs.get(song_id)
            if not song:
                continue
            song_name = song.get(SONG_NAME, None)
            song_hashes = song.get(FIELD_TOTAL_HASHES, None)
            nseconds = round(float(offset) / DEFAULT_FS * DEFAULT_WINDOW_SIZE * DEFAULT_OVERLAP_RATIO, 5)
            hashes_matched = dedup_hashes[song_id]
            fingerprinted_confidence = round(hashes_matched / song_hashes, 2)
            
            if fingerprinted_confidence < confidence_threshold:
                continue

            song = {
                SONG_ID: song_id,
                SONG_NAME: song_name.encode("utf8"),
                INPUT_HASHES: queried_hashes,
                FINGERPRINTED_HASHES: song_hashes,
                HASHES_MATCHED: hashes_matched,
                # Percentage regarding hashes matched vs hashes from the input.
                INPUT_CONFIDENCE: round(hashes_matched / queried_hashes, 2),
                # Percentage regarding hashes matched vs hashes fingerprinted in the db.
                FINGERPRINTED_CONFIDENCE: fingerprinted_confidence,
                OFFSET: offset,
                OFFSET_SECS: nseconds,
                FIELD_FILE_SHA1: song.get(FIELD_FILE_SHA1, None).encode("utf8")
            }

            songs_result.append(song)

        return songs_result

    def recognize(self, recognizer, *options, **kwoptions) -> Dict[str, any]:
        r = recognizer(self)
        return r.recognize(*options, **kwoptions)

    @staticmethod
    def _fingerprint_worker(arguments):
        # Pool.imap sends arguments as tuples so we have to unpack
        # them ourself.
        try:
            file_name, limit = arguments
        except ValueError:
            pass

        song_name, extension = os.path.splitext(os.path.basename(file_name))

        fingerprints, file_hash = Dejavu.get_file_fingerprints(file_name, limit, print_output=True)

        return song_name, fingerprints, file_hash

    @staticmethod
    def get_file_fingerprints(file_name: str, limit: int, print_output: bool = False):
        channels, fs, file_hash = decoder.read(file_name, limit)
        fingerprints = set()
        channel_amount = len(channels)
        for channeln, channel in enumerate(channels, start=1):
            if print_output:
                print(f"Fingerprinting channel {channeln}/{channel_amount} for {file_name}")

            hashes = fingerprint(channel, Fs=fs)

            if print_output:
                print(f"Finished channel {channeln}/{channel_amount} for {file_name}")

            fingerprints |= set(hashes)

        return fingerprints, file_hash
    
    @staticmethod
    def get_np_fingerprints(
        channels: list[np.ndarray],
        fs: int,
        file_name: str = "in_memory_audio",
        limit: int = None,
        print_output: bool = False
    ):
        """
        Generate fingerprints directly from in-memory NumPy audio data.

        :param channels: list of NumPy arrays, one per audio channel
        :param fs: sampling rate of the audio data
        :param file_name: optional name tag for logging or hash generation
        :param limit: optional limit in seconds (truncate data if set)
        :param print_output: whether to print progress messages
        :return: (fingerprints, file_hash)
        """
        # Optionally limit audio length
        if limit:
            samples_limit = int(limit * fs)
            channels = [ch[:samples_limit] for ch in channels]

        fingerprints = set()
        channel_amount = len(channels)

        for channeln, channel in enumerate(channels, start=1):
            if print_output:
                print(f"Fingerprinting channel {channeln}/{channel_amount} for {file_name}")

            hashes = fingerprint(channel, Fs=fs)

            if print_output:
                print(f"Finished channel {channeln}/{channel_amount} for {file_name}")

            fingerprints |= set(hashes)

        # Generate a unique hash for the in-memory audio
        # based on data content (for deduplication)
        s = sha1()
        for ch in channels:
            s.update(np.ascontiguousarray(ch).tobytes())
        file_hash = s.hexdigest().upper()

        return fingerprints, file_hash
