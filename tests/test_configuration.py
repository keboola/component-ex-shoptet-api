import unittest
from typing import Any, ClassVar

from keboola.component.exceptions import UserException

from configuration import AuthType, Configuration, Credentials, LoadType, ObjectType


class TestCredentials(unittest.TestCase):
    def test_private_token_is_read_from_the_encrypted_key(self):
        creds = Credentials(auth_type="private_token", **{"#private_api_token": "secret"})
        self.assertEqual(AuthType.private_token, creds.auth_type)
        self.assertEqual("secret", creds.private_api_token)

    def test_private_token_is_required_for_that_auth_type(self):
        with self.assertRaises(UserException) as ctx:
            Credentials(auth_type="private_token")
        self.assertIn("private API token", str(ctx.exception))

    def test_oauth_needs_both_the_token_and_the_server_url(self):
        with self.assertRaises(UserException):
            Credentials(auth_type="addon_oauth", **{"#oauth_access_token": "t"})

        creds = Credentials(
            auth_type="addon_oauth",
            oauth_token_url="https://1.myshoptet.com/action/ApiOAuthServer/getAccessToken",
            **{"#oauth_access_token": "t"},
        )
        self.assertEqual(AuthType.addon_oauth, creds.auth_type)

    def test_validation_errors_surface_as_user_exceptions(self):
        # A wrong auth_type is a user mistake (exit 1), not a crash (exit 2).
        with self.assertRaises(UserException):
            Credentials(auth_type="basic", **{"#private_api_token": "secret"})


class TestConfiguration(unittest.TestCase):
    _BASE: ClassVar[dict[str, Any]] = {"auth_type": "private_token", "#private_api_token": "secret"}

    def test_object_is_required(self):
        with self.assertRaises(UserException) as ctx:
            Configuration(**self._BASE)
        self.assertIn("object", str(ctx.exception))

    def test_defaults(self):
        cfg = Configuration(**self._BASE, object="orders")
        self.assertEqual(ObjectType.orders, cfg.object)
        self.assertEqual(LoadType.incremental_load, cfg.load_type)
        self.assertTrue(cfg.incremental)
        self.assertTrue(cfg.extract_child_tables)
        self.assertEqual(24, cfg.lookback_hours)
        self.assertEqual([], cfg.include)

    def test_full_load_turns_off_the_incremental_flag(self):
        cfg = Configuration(**self._BASE, object="products", load_type="full_load")
        self.assertFalse(cfg.incremental)

    def test_date_range_is_optional_and_nested(self):
        cfg = Configuration(**self._BASE, object="orders", date_range={"date_from": "30 days ago"})
        self.assertEqual("30 days ago", cfg.date_range.date_from)
        self.assertEqual("", cfg.date_range.date_to)

    def test_lookback_is_bounded(self):
        with self.assertRaises(UserException):
            Configuration(**self._BASE, object="orders", lookback_hours=-1)


if __name__ == "__main__":
    unittest.main()
