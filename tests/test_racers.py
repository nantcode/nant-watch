import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import app
from app import load_secrets, make_accounts, racer_params, run_weekly, secrets_from_ssm
from race import RACERS_FILE, load_entrants, make_race_accounts, parse_entrants
from test_app import FakeSSM
from test_weekly import MarketWithHistory, make_market

OCT2, OCT9 = date(2026, 10, 2), date(2026, 10, 9)
ENV = {"ALPACA_KEY_ID_PARAM": "/k", "ALPACA_SECRET_KEY_PARAM": "/s", "DISCORD_WEBHOOK_PARAM": "/w"}


def config(*racers):
    return {"racers": list(racers)}


class TestShippedConfig(unittest.TestCase):
    def test_racers_json_keeps_todays_lineup(self):
        entrants = load_entrants()
        self.assertEqual([e.name for e in entrants], ["NantBot", "Sparticus"])
        self.assertTrue(entrants[0].main)
        self.assertEqual(entrants[1].key_id_param, "/nant-watch/sparticus_key_id")
        self.assertEqual(entrants[1].secret_key_param, "/nant-watch/sparticus_secret_key")

    def test_racers_json_ships_with_the_lambda_code(self):
        self.assertEqual(os.path.basename(os.path.dirname(RACERS_FILE)), "src")


class TestParseEntrants(unittest.TestCase):
    def test_defaults_come_from_the_name(self):
        (entrant,) = parse_entrants(config({"name": "Turbo Bot"}))
        self.assertEqual(entrant.slug, "turbo_bot")
        self.assertFalse(entrant.main)
        self.assertEqual(entrant.key_id_param, "/nant-watch/racers/turbo_bot/key_id")
        self.assertEqual(entrant.secret_key_param, "/nant-watch/racers/turbo_bot/secret_key")
        self.assertEqual((entrant.key_id_env, entrant.secret_key_env), ("TURBO_BOT_KEY_ID", "TURBO_BOT_SECRET_KEY"))
        self.assertEqual((entrant.key_id_secret, entrant.secret_key_secret),
                         ("turbo_bot_key_id", "turbo_bot_secret_key"))

    def test_explicit_paths_and_env_names_win(self):
        (entrant,) = parse_entrants(config({"name": "Zed", "key_id_param": "/x/id", "secret_key_param": "/x/sk",
                                            "key_id_env": "ZK", "secret_key_env": "ZS"}))
        self.assertEqual((entrant.key_id_param, entrant.secret_key_param), ("/x/id", "/x/sk"))
        self.assertEqual((entrant.key_id_env, entrant.secret_key_env), ("ZK", "ZS"))

    def test_name_is_trimmed(self):
        self.assertEqual(parse_entrants(config({"name": "  Zed "}))[0].name, "Zed")

    def test_bad_configs_fail_loudly(self):
        cases = {
            "non-empty 'racers' list": {},
            "non-empty 'racers' list ": config(),
            "must be an object": config("NantBot"),
            "needs a name": config({"main": True}),
            "needs a name ": config({"name": "   "}),
            "longer than 10": config({"name": "WayTooLongName"}),
            "letter or digit": config({"name": "!!!"}),
            "benchmark": config({"name": "spy"}),
            "unique": config({"name": "Zed"}, {"name": "zed"}),
            "only one racer can be the main": config({"name": "A", "main": True}, {"name": "B", "main": True}),
            "'main' must be true or false": config({"name": "A", "main": "yes"}),
            "unknown field(s): api_key": config({"name": "A", "api_key": "oops"}),
            "must be a non-empty string": config({"name": "A", "key_id_param": ""}),
        }
        for message, bad in cases.items():
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as caught:
                    parse_entrants(bad)
                self.assertIn(message.strip(), str(caught.exception))

    def test_invalid_json_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "racers.json")
            with open(path, "w") as handle:
                handle.write('{"racers": [')
            with self.assertRaises(ValueError) as caught:
                load_entrants(path)
            self.assertIn("not valid JSON", str(caught.exception))

    def test_load_from_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "racers.json")
            with open(path, "w") as handle:
                json.dump(config({"name": "NantBot", "main": True}, {"name": "Zed"}), handle)
            self.assertEqual([e.name for e in load_entrants(path)], ["NantBot", "Zed"])


class TestMakeRaceAccounts(unittest.TestCase):
    ENTRANTS = parse_entrants(config({"name": "NantBot", "main": True}, {"name": "Zed"}, {"name": "Ace"}))

    def build(self, keys):
        logs = []
        accounts = make_race_accounts(self.ENTRANTS, "MAIN", "NKEY", keys, lambda k, s: f"client:{k}", logs.append)
        return accounts, logs

    def test_every_entrant_gets_a_seat_in_config_order(self):
        accounts, logs = self.build({"Zed": ("ZK", "ZS"), "Ace": ("AK", "AS")})
        self.assertEqual(list(accounts.items()),
                         [("NantBot", "MAIN"), ("Zed", "client:ZK"), ("Ace", "client:AK")])
        self.assertEqual(logs, [])

    def test_missing_keys_show_as_no_data(self):
        accounts, logs = self.build({"Zed": ("ZK", "")})
        self.assertEqual((accounts["Zed"], accounts["Ace"]), (None, None))
        self.assertTrue(any("no Zed keys" in line for line in logs))
        self.assertTrue(any("no Ace keys" in line for line in logs))

    def test_shared_accounts_warn(self):
        _, logs = self.build({"Zed": ("NKEY", "x"), "Ace": ("NKEY", "y")})
        self.assertIn("WARNING: Zed and NantBot share one Alpaca account; their results will match", logs)
        self.assertIn("WARNING: Ace and NantBot share one Alpaca account; their results will match", logs)

    def test_two_entrants_sharing_keys_warn(self):
        _, logs = self.build({"Zed": ("ZK", "x"), "Ace": ("ZK", "y")})
        self.assertIn("WARNING: Ace and Zed share one Alpaca account; their results will match", logs)


class TestSecretsForEntrants(unittest.TestCase):
    def test_racer_params_skip_the_main_account(self):
        entrants = parse_entrants(config({"name": "NantBot", "main": True}, {"name": "Zed"}))
        self.assertEqual(racer_params(entrants), {"zed_key_id": "/nant-watch/racers/zed/key_id",
                                                  "zed_secret_key": "/nant-watch/racers/zed/secret_key"})

    def test_new_racer_keys_are_fetched(self):
        entrants = parse_entrants(config({"name": "NantBot", "main": True}, {"name": "Zed"}))
        ssm = FakeSSM({"/k": "K", "/s": "S", "/w": "W",
                       "/nant-watch/racers/zed/key_id": "ZK", "/nant-watch/racers/zed/secret_key": "ZS"})
        secrets = secrets_from_ssm(ssm, ENV, entrants=entrants)
        self.assertEqual((secrets["zed_key_id"], secrets["zed_secret_key"]), ("ZK", "ZS"))

    def test_many_racers_are_fetched_ten_names_per_call(self):
        names = [f"/p{i}" for i in range(23)]
        ssm = FakeSSM({name: name.upper() for name in names})
        self.assertEqual(len(load_secrets(ssm, names)), 23)
        self.assertEqual([len(call[0]) for call in ssm.calls], [10, 10, 3])

    def test_duplicate_paths_are_asked_for_once(self):
        ssm = FakeSSM({"/a": "1"})
        load_secrets(ssm, ["/a", "/a"])
        self.assertEqual(ssm.calls, [(["/a"], True)])

    def test_missing_required_secret_in_a_later_batch_still_fails(self):
        names = [f"/p{i}" for i in range(12)]

        class PerCallSSM(FakeSSM):
            def get_parameters(self, Names, WithDecryption):
                self.calls.append((Names, WithDecryption))
                return {"Parameters": [{"Name": n, "Value": "v"} for n in Names if n != "/p11"],
                        "InvalidParameters": [n for n in Names if n == "/p11"]}

        with self.assertRaises(RuntimeError):
            load_secrets(PerCallSSM({}), names)

    def test_make_accounts_uses_the_given_entrants(self):
        entrants = parse_entrants(config({"name": "NantBot", "main": True}, {"name": "Zed"}))
        secrets = {"key_id": "N", "secret_key": "S", "zed_key_id": "ZK", "zed_secret_key": "ZS"}
        nantbot, accounts = make_accounts(secrets, lambda k, s: k, lambda line: None, entrants=entrants)
        self.assertEqual((nantbot, accounts), ("N", {"NantBot": "N", "Zed": "ZK"}))


class TestNewEntrantReachesTheBoard(unittest.TestCase):
    """End to end: add a third bot to the config and it races on the weekly board."""

    def test_third_racer_on_weekly_scoreboard(self):
        entrants = parse_entrants(config({"name": "NantBot", "main": True},
                                         {"name": "Sparticus", "key_id_param": "/sk", "secret_key_param": "/ss"},
                                         {"name": "Rocket"}))
        market = make_market()
        rocket = MarketWithHistory(equity_points=[(OCT2, 1_000), (OCT9, 1_100)])
        secrets = {"key_id": "NKEY", "secret_key": "NS", "webhook_url": "https://discord.example/hook",
                   "rocket_key_id": "RKEY", "rocket_secret_key": "RS"}
        logs = []
        with mock.patch.object(app, "load_entrants", return_value=entrants):
            outcome = run_weekly({"dry_run": True}, datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc), secrets,
                                 make_client=lambda k, s: rocket if k == "RKEY" else market,
                                 poster=None, log=logs.append)
        text = "\n".join(outcome["messages"])
        self.assertIn("Rocket", text)
        self.assertIn("Rocket is beating SPY", text)
        self.assertIn("Sparticus: no data yet", text)
        self.assertTrue(any(line.startswith("race: Rocket") for line in logs))


if __name__ == "__main__":
    unittest.main()
