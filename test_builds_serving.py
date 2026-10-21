"""Leaderboard HTTP errors distinguish missing scenarios from server failures."""

from contextlib import redirect_stdout
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import webapp


class TestLeaderboardRoutes(unittest.TestCase):
    def setUp(self):
        args = SimpleNamespace(no_warm=True, no_open=True, host="127.0.0.1", port=0)
        with patch.object(webapp, "AutoWarm"), \
             patch.object(webapp, "TftResponses"), \
             patch("http.server.ThreadingHTTPServer") as server, \
             patch.object(webapp.signal, "signal"), \
             redirect_stdout(io.StringIO()):
            webapp.cmd_serve(args)
        handler_class = server.call_args.args[1]
        self.handler = object.__new__(handler_class)
        self.handler._json = Mock()

    def request(self, key):
        self.handler.path = f"/api/builds/leaderboard/{key}.json"
        self.handler.do_GET()
        return self.handler._json.call_args

    def test_known_scenario_uses_leaderboard_route_before_champion_route(self):
        board = {"scenario": {"key": "full-squishy"}, "rows": []}
        with patch.object(webapp.builds_leaderboard, "cached_leaderboard", return_value=board) as leaderboard, \
             patch.object(webapp.builds, "cached_scenario", side_effect=AssertionError("champion route")):
            response = self.request("full-squishy")
        self.assertEqual(response.args, (board,))
        leaderboard.assert_called_once_with("full-squishy")

    def test_unknown_scenario_returns_404_without_reading_cells(self):
        with patch.object(webapp.builds_leaderboard, "cached_leaderboard") as leaderboard:
            response = self.request("missing")
        self.assertEqual(response.args, ({"error": "unknown leaderboard scenario 'missing'"}, 404))
        leaderboard.assert_not_called()

    def test_invalid_saved_data_returns_500_instead_of_404(self):
        with patch.object(webapp.builds_leaderboard, "cached_leaderboard",
                          side_effect=ValueError("invalid saved leaderboard data")):
            response = self.request("full-overall")
        self.assertEqual(response.args, ({"error": "invalid saved leaderboard data"}, 500))


if __name__ == "__main__":
    unittest.main()
