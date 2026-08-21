from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import media_lookup  # noqa: E402
import webui  # noqa: E402
from rename import (  # noqa: E402
    _new_file_node,
    apply_metadata_match,
    compute_ops,
    metadata_search_context,
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

        def fake_urlopen(request, timeout):
            urls.append(request.full_url)
            return _Response()

        with patch.dict(os.environ, {"TMDB_API_TOKEN": "test-token"}):
            with patch.object(media_lookup, "urlopen", fake_urlopen):
                media_lookup._search_tmdb("The Office", "tv", 2005, "en-US")

        self.assertNotIn("year=", urls[0])
        self.assertNotIn("first_air_date_year", urls[0])

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

        self.assertEqual(
            node["parts"],
            {"title": "The Office (2005)", "middle": "S01E05", "right": "Pilot"},
        )
        self.assertEqual(node["proposed"], "The Office (2005) - S01E05 - Pilot.mkv")

    def test_bare_tv_episode_selection_emits_episode_destination_filename(self) -> None:
        node = _new_file_node(Path("/media/TVShows/Series-A/S01E05.mkv"))

        self.assertEqual(
            node["parts"],
            {"title": "Series-A", "middle": "S01E05", "right": ""},
        )
        apply_metadata_match(node, "tmdb", "2316", "tv", "The Office", 2005)

        self.assertEqual(
            self._destination_name(node),
            "The Office (2005) - S01E05.mkv",
        )

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


if __name__ == "__main__":
    unittest.main()