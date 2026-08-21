from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import media_lookup  # noqa: E402
import state  # noqa: E402
import webui  # noqa: E402
from rename import (  # noqa: E402
    _new_file_node,
    _new_folder_node,
    apply_metadata_match,
    apply_tree,
    compute_ops,
    metadata_search_context,
    undo_last,
)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return b'{"results": []}'


class MediaLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        media_lookup._CACHE.clear()

    def test_mock_search_ranks_title_and_year(self) -> None:
        with patch.dict(os.environ, {"VHC_METADATA_PROVIDER": "mock"}):
            matches = media_lookup.search_media("The Office (2005)", "tv")

        self.assertEqual((matches[0].title, matches[0].year), ("The Office", 2005))
        self.assertTrue(all(match.media_type == "tv" for match in matches))

    def test_tmdb_search_does_not_hard_filter_existing_year(self) -> None:
        urls: list[str] = []
        auth: list[str] = []

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            auth.append(request.get_header("Authorization"))
            return _Response()

        with patch.dict(os.environ, {"TMDB_API_TOKEN": "test-token"}):
            with patch.object(media_lookup, "urlopen", fake_urlopen):
                media_lookup._search_tmdb("The Office", "tv", 2005, "en-US")

        self.assertNotIn("year=", urls[0])
        self.assertNotIn("first_air_date_year", urls[0])
        self.assertEqual(auth[0], "Bearer test-token")

    def test_explicit_token_arg_overrides_env(self) -> None:
        auth: list[str] = []

        def fake_urlopen(request, timeout):
            auth.append(request.get_header("Authorization"))
            return _Response()

        with patch.dict(os.environ, {"TMDB_API_TOKEN": "env-token"}):
            with patch.object(media_lookup, "urlopen", fake_urlopen):
                media_lookup._search_tmdb(
                    "The Office", "tv", 2005, "en-US", token="explicit-token",
                )

        self.assertEqual(auth[0], "Bearer explicit-token")

    def test_search_media_without_token_or_env_reports_unavailable(self) -> None:
        with patch.dict(os.environ, {"TMDB_API_TOKEN": ""}, clear=False):
            os.environ.pop("TMDB_API_TOKEN", None)
            with self.assertRaises(media_lookup.LookupUnavailable):
                media_lookup._search_tmdb("Foo", "movie", None, "en-US")

    def test_tmdb_normalization_excludes_people(self) -> None:
        payload = {
            "results": [
                {"media_type": "person", "id": 1, "name": "Nobody"},
                {"media_type": "movie", "id": 2, "title": "No Date", "release_date": ""},
                {"media_type": "tv", "id": 3, "name": "Valid Show", "first_air_date": "2020-01-01"},
            ]
        }

        matches = media_lookup._parse_tmdb_results(payload, "any")

        self.assertEqual(
            [(match.media_type, match.title, match.year) for match in matches],
            [("movie", "No Date", None), ("tv", "Valid Show", 2020)],
        )


class RenameMetadataTests(unittest.TestCase):
    @staticmethod
    def _destination_name(node: dict) -> str:
        source = Path(node["path"])
        root = {
            "id": "root",
            "type": "folder",
            "name": source.parent.name,
            "proposed": source.parent.name,
            "path": str(source.parent),
            "is_root": True,
            "children": [node],
        }
        operations = compute_ops(root)
        return Path(operations[-1]["dst"]).name

    def test_movie_selection_preserves_version(self) -> None:
        node = {
            "type": "file",
            "name": "Dark.mkv",
            "ext": ".mkv",
            "kind": "movie",
            "parts": {"title": "Dark", "middle": "2007", "right": "Extended"},
        }

        self.assertEqual(metadata_search_context(node), ("movie", 2007))
        apply_metadata_match(node, "tmdb", "155", "movie", "The Dark Knight", 2008)

        self.assertEqual(
            node["parts"],
            {"title": "The Dark Knight", "middle": "2008", "right": "Extended"},
        )
        self.assertEqual(node["proposed"], "The Dark Knight (2008) - Extended.mkv")

    def test_movie_selection_emits_canonical_destination_filename(self) -> None:
        node = _new_file_node(Path("/media/Movies/Dark.Knight.2008.2160p.mkv"))

        apply_metadata_match(node, "tmdb", "155", "movie", "The Dark Knight", 2008)

        self.assertEqual(node["parts"]["middle"], "2008")
        self.assertEqual(
            self._destination_name(node),
            "The Dark Knight (2008).mkv",
        )

    def test_show_selection_preserves_episode_fields(self) -> None:
        node = {
            "type": "file",
            "name": "Show.mkv",
            "ext": ".mkv",
            "kind": "tv",
            "parts": {"title": "Wrong", "middle": "S01E05", "right": "Pilot"},
        }

        self.assertEqual(metadata_search_context(node), ("tv", None))
        apply_metadata_match(node, "tmdb", "2316", "tv", "The Office", 2005)

        # Jellyfin convention: year lives on the series folder, NOT in the
        # episode filename — so title stays year-free even though TMDB
        # returned one.
        self.assertEqual(
            node["parts"],
            {"title": "The Office", "middle": "S01E05", "right": "Pilot"},
        )
        self.assertEqual(node["proposed"], "The Office - S01E05 - Pilot.mkv")

    def test_bare_tv_episode_selection_emits_episode_destination_filename(self) -> None:
        node = _new_file_node(Path("/media/TVShows/Series-A/S01E05.mkv"))

        self.assertEqual(
            node["parts"],
            {"title": "Series-A", "middle": "S01E05", "right": ""},
        )
        apply_metadata_match(node, "tmdb", "2316", "tv", "The Office", 2005)

        # Same rule: no year in the emitted episode filename.
        self.assertEqual(
            self._destination_name(node),
            "The Office - S01E05.mkv",
        )

    def test_episode_title_drops_dangling_open_paren_before_release_tag(self) -> None:
        """When the release-info tag opens with '(1080p ...)' the parser
        cuts before the release marker; the leftover unmatched '(' must
        not survive into the episode-title field."""
        node = _new_file_node(Path(
            "/media/TVShows/Better.Call.Saul/SS1/"
            "Better Call Saul (2015) - S01E01 - Uno (1080p BluRay 10bit HEVC x265).mkv"
        ))
        self.assertEqual(node["parts"]["title"], "Better Call Saul (2015)")
        self.assertEqual(node["parts"]["middle"], "S01E01")
        self.assertEqual(node["parts"]["right"], "Uno")

    def test_bare_tv_episode_under_season_uses_series_folder(self) -> None:
        node = _new_file_node(
            Path("/media/TVShows/The Office/Season 01/S01E06.mkv"),
        )

        self.assertEqual(node["parts"]["title"], "The Office")
        self.assertEqual(node["parts"]["middle"], "S01E06")

    def test_folder_context_and_selection_for_movie_and_show(self) -> None:
        movie_file = {
            "type": "file",
            "kind": "movie",
            "parts": {"title": "Wrong", "middle": "2023", "right": ""},
        }
        movie_folder = {
            "type": "folder",
            "name": "Wrong (2023)",
            "proposed": "Wrong (2023)",
            "children": [movie_file],
        }
        episode = {
            "type": "file",
            "kind": "tv",
            "parts": {"title": "Wrong", "middle": "S01E05", "right": "Pilot"},
        }
        show_folder = {
            "type": "folder",
            "name": "Wrong Show",
            "proposed": "Wrong Show",
            "children": [episode],
        }

        self.assertEqual(metadata_search_context(movie_folder), ("movie", 2023))
        self.assertEqual(metadata_search_context(show_folder), ("tv", None))
        apply_metadata_match(
            movie_folder, "tmdb", "155", "movie", "The Dark Knight", 2008,
        )
        apply_metadata_match(
            show_folder, "tmdb", "2316", "tv", "The Office", 2005,
        )

        self.assertEqual(movie_folder["proposed"], "The Dark Knight (2008)")
        self.assertEqual(show_folder["proposed"], "The Office (2005)")
        self.assertEqual(episode["parts"]["middle"], "S01E05")

    def test_folder_editor_is_a_searchable_combobox(self) -> None:
        folder = {
            "id": "folder1",
            "type": "folder",
            "name": "Wrong",
            "proposed": "Wrong",
            "children": [],
        }

        markup = webui._proposed_input(folder, False)

        self.assertIn("role='combobox'", markup)
        self.assertIn("name='proposed'", markup)
        self.assertIn("Movie and show folder matches", markup)

    def test_result_markup_escapes_provider_text(self) -> None:
        match = media_lookup.MediaMatch(
            "tmdb", "12", "movie", "<script>Don't say \"x\"</script>", 2020,
        )

        markup = webui._render_match_options("node", [match])

        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn("&#x27;", markup)
        self.assertIn("\\&quot;x\\&quot;", markup)
        self.assertIn("Results from TMDB", markup)

    def test_settings_form_masks_tmdb_token(self) -> None:
        """The rendered settings HTML must never contain the actual token
        value — only a masked placeholder — so it isn't leaked via view-
        source or DevTools once the user has configured it."""
        secret = "eyJverySECRETtoken12345"
        from config import Config

        cfg = Config()
        cfg.metadata.tmdb_api_token = secret
        cfg.metadata.tmdb_language = "en-US"

        markup = webui._render_page(cfg)

        self.assertNotIn(secret, markup)
        self.assertIn("name='tmdb_api_token'", markup)
        # Configured state should show the mask; unconfigured shows a hint.
        self.assertIn("configured", markup)


class PendingRemapAfterRenameTests(unittest.TestCase):
    """A user who renames files via the Rename tab before clicking Convert
    would otherwise see the queue silently drop everything (paths don't
    exist anymore). apply_tree / undo_last must update pending in place.
    """

    def tearDown(self) -> None:
        state.set_pending([])
        state.set_all_media([])

    def test_apply_remaps_pending_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Movie.2020.mkv"
            src.write_bytes(b"x")
            state.set_pending([{"path": str(src), "codec": "h264"}])

            tree = {
                "id": "root", "type": "folder", "name": tmp, "proposed": tmp,
                "path": tmp, "is_root": True,
                "children": [{
                    "id": "f1", "type": "file", "name": "Movie.2020.mkv",
                    "ext": ".mkv", "kind": "movie",
                    "path": str(src), "proposed": "Movie (2020).mkv",
                    "parts": {"title": "Movie", "middle": "2020", "right": ""},
                }],
            }
            apply_tree(tree, Path(tmp) / "undo.log")

            expected = str(Path(tmp) / "Movie (2020).mkv")
            self.assertEqual(state.get_pending()[0]["path"], expected)
            self.assertTrue(Path(expected).exists())

    def test_apply_remaps_pending_under_folder_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Better.Call.Saul"
            folder.mkdir()
            src = folder / "S01E01.mkv"
            src.write_bytes(b"x")
            state.set_pending([{"path": str(src), "codec": "h264"}])

            tree = {
                "id": "root", "type": "folder", "name": tmp, "proposed": tmp,
                "path": tmp, "is_root": True,
                "children": [{
                    "id": "d1", "type": "folder",
                    "name": "Better.Call.Saul",
                    "proposed": "Better Call Saul (2015)",
                    "path": str(folder),
                    "children": [{
                        "id": "f1", "type": "file",
                        "name": "S01E01.mkv", "ext": ".mkv", "kind": "tv",
                        "path": str(src),
                        "proposed": "S01E01.mkv",
                        "parts": {"title": "Better Call Saul (2015)",
                                  "middle": "S01E01", "right": ""},
                    }],
                }],
            }
            apply_tree(tree, Path(tmp) / "undo.log")

            expected = str(Path(tmp) / "Better Call Saul (2015)" / "S01E01.mkv")
            self.assertEqual(state.get_pending()[0]["path"], expected)
            self.assertTrue(Path(expected).exists())

    def test_undo_restores_pending_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Movie.2020.mkv"
            src.write_bytes(b"x")
            state.set_pending([{"path": str(src), "codec": "h264"}])
            log = Path(tmp) / "undo.log"

            tree = {
                "id": "root", "type": "folder", "name": tmp, "proposed": tmp,
                "path": tmp, "is_root": True,
                "children": [{
                    "id": "f1", "type": "file", "name": "Movie.2020.mkv",
                    "ext": ".mkv", "kind": "movie",
                    "path": str(src), "proposed": "Movie (2020).mkv",
                    "parts": {"title": "Movie", "middle": "2020", "right": ""},
                }],
            }
            apply_tree(tree, log)
            self.assertNotEqual(state.get_pending()[0]["path"], str(src))

            undo_last(log)
            self.assertEqual(state.get_pending()[0]["path"], str(src))
            self.assertTrue(src.exists())


class FolderNameCleaningTests(unittest.TestCase):
    """Dot-separated folder names ('Better.Call.Saul') should surface
    space-separated in the Rename tab so users don't have to fix each one."""

    def test_folder_proposed_converts_dots_to_spaces(self) -> None:
        node = _new_folder_node(Path("/media/TVShows/Better.Call.Saul"))
        self.assertEqual(node["name"], "Better.Call.Saul")
        self.assertEqual(node["proposed"], "Better Call Saul")

    def test_folder_proposed_leaves_non_season_names_alone(self) -> None:
        for name in ("Movies", "TVShows", "Specials"):
            node = _new_folder_node(Path(f"/media/{name}"))
            self.assertEqual(node["proposed"], name)

    def test_season_folder_variants_canonicalize_to_jellyfin_format(self) -> None:
        cases = {
            "S01": "Season 01",
            "S1": "Season 01",
            "s5": "Season 05",
            "SS1": "Season 01",
            "SS 5": "Season 05",
            "ss05": "Season 05",
            "Season 1": "Season 01",
            "Season 01": "Season 01",
            "Season.3": "Season 03",
            "Season_10": "Season 10",
            "SEASON05": "Season 05",
            "S00": "Season 00",
        }
        for input_name, expected in cases.items():
            with self.subTest(input=input_name):
                node = _new_folder_node(Path(f"/media/Show/{input_name}"))
                self.assertEqual(node["proposed"], expected)

    def test_ambiguous_bare_number_folders_are_not_canonicalized(self) -> None:
        # A folder literally named "1" or "01" is too ambiguous to guess.
        for name in ("1", "01", "2024"):
            node = _new_folder_node(Path(f"/media/Show/{name}"))
            self.assertEqual(node["proposed"], name)

    def test_bare_episode_under_season_variant_folder_uses_grandparent(self) -> None:
        # File parser walk-up now recognises SS1/, ss5/, Season.03/ etc.
        # as season folders and looks one level up for the show name.
        node = _new_file_node(Path("/media/TVShows/Better Call Saul/SS1/S01E05.mkv"))
        self.assertEqual(node["parts"]["title"], "Better Call Saul")
        self.assertEqual(node["parts"]["middle"], "S01E05")

    def test_bare_episode_number_under_season_folder_infers_SxxExx(self) -> None:
        """Files whose only episode marker is a bare number (Tập 1,
        Episode 5, E07, or a trailing '05') should still be recognised
        as TV episodes when the parent folder tells us the season."""
        cases = {
            "Tập 1.mkv": "S01E01",
            "Tập 10.mkv": "S01E10",
            "Tap 3.mkv": "S01E03",  # ASCII fallback for user typos
            "Episode 5.mkv": "S01E05",
            "Ep.7.mkv": "S01E07",
            "E08.mkv": "S01E08",
            "Money Heist - 12.mkv": "S01E12",
        }
        for filename, expected_middle in cases.items():
            with self.subTest(filename=filename):
                node = _new_file_node(Path(f"/media/TVShows/Money.Heist/ss1/{filename}"))
                self.assertEqual(node["parts"]["middle"], expected_middle)
                self.assertEqual(node["parts"]["title"], "Money Heist")

    def test_bare_episode_number_not_inferred_without_season_folder(self) -> None:
        """When the parent folder isn't recognisable as a season, we
        must NOT guess an episode number from ambiguous filenames — the
        file should fall back to the movie/unknown path."""
        node = _new_file_node(Path("/media/Movies/Tập 1.mkv"))
        self.assertNotEqual(node["parts"]["middle"], "S01E01")


class ApplyIsIdempotentTests(unittest.TestCase):
    """Applying the same rename twice should not raise 'destination already
    exists' — the second run should observe the file is already at its
    target and no-op silently."""

    def tearDown(self) -> None:
        state.set_pending([])
        state.set_all_media([])

    def test_second_apply_of_same_op_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Movie.2020.mkv"
            src.write_bytes(b"payload")
            log = Path(tmp) / "undo.log"
            state.set_pending([{"path": str(src)}])

            tree = {
                "id": "root", "type": "folder", "name": tmp, "proposed": tmp,
                "path": tmp, "is_root": True,
                "children": [{
                    "id": "f1", "type": "file", "name": "Movie.2020.mkv",
                    "ext": ".mkv", "kind": "movie",
                    "path": str(src), "proposed": "Movie (2020).mkv",
                    "parts": {"title": "Movie", "middle": "2020", "right": ""},
                }],
            }

            first = apply_tree(tree, log)
            self.assertEqual(first["failed"], 0)
            self.assertEqual(first["applied"], 1)

            # Same op again. Src no longer exists, dst does. Must not fail.
            second = apply_tree(tree, log)
            self.assertEqual(second["failed"], 0)
            self.assertEqual(second["applied"], 1)
            self.assertTrue(second["results"][0].get("skipped"))

            # Undo log must contain only ONE batch — the second (no-op)
            # apply must not add an entry that undo would then try to
            # reverse.
            with open(log, "r", encoding="utf-8") as f:
                batches = [line for line in f if line.strip()]
            self.assertEqual(len(batches), 1)


class UndoOnlyReversesLastApplyTests(unittest.TestCase):
    """Users can rename the same file across multiple Apply passes; only
    the MOST RECENT rename should be in the undo log. Older Apply history
    is discarded."""

    def tearDown(self) -> None:
        state.set_pending([])
        state.set_all_media([])

    def test_undo_after_two_applies_reverses_only_the_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Original.mkv"
            src.write_bytes(b"payload")
            log = Path(tmp) / "undo.log"
            state.set_pending([{"path": str(src)}])

            def _tree(current_path: Path, proposed_name: str) -> dict:
                return {
                    "id": "root", "type": "folder", "name": tmp,
                    "proposed": tmp, "path": tmp, "is_root": True,
                    "children": [{
                        "id": "f1", "type": "file",
                        "name": current_path.name, "ext": ".mkv",
                        "kind": "movie", "path": str(current_path),
                        "proposed": proposed_name,
                        "parts": {"title": current_path.stem,
                                  "middle": "", "right": ""},
                    }],
                }

            # Apply 1: Original → Intermediate
            apply_tree(_tree(src, "Intermediate.mkv"), log)
            mid = Path(tmp) / "Intermediate.mkv"
            self.assertTrue(mid.exists())
            self.assertFalse(src.exists())

            # Apply 2: Intermediate → Final
            apply_tree(_tree(mid, "Final.mkv"), log)
            final = Path(tmp) / "Final.mkv"
            self.assertTrue(final.exists())
            self.assertFalse(mid.exists())

            # Log holds exactly ONE batch — the most recent Apply.
            with open(log, "r", encoding="utf-8") as f:
                batches = [line for line in f if line.strip()]
            self.assertEqual(len(batches), 1)

            # Undo reverses only the second Apply. The file returns to
            # Intermediate (not Original), and pending follows.
            undo_last(log)
            self.assertTrue(mid.exists())
            self.assertFalse(final.exists())
            self.assertEqual(state.get_pending()[0]["path"], str(mid))


if __name__ == "__main__":
    unittest.main()