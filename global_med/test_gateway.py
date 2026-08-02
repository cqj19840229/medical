import unittest

from gateway import extract_user_id


class ExtractUserIdTests(unittest.TestCase):
    def test_header_has_priority(self):
        actual = extract_user_id(
            {"x-user-id": "42", "content-type": "application/json"},
            "/users/9/dialogues",
            {"user_id": "8"},
            b'{"user_id": 7}',
        )
        self.assertEqual(actual, ("42", "header:x-user-id"))

    def test_query(self):
        self.assertEqual(
            extract_user_id({}, "/search", {"user_id": "17"}, b""),
            ("17", "query:user_id"),
        )

    def test_path(self):
        self.assertEqual(
            extract_user_id({}, "/users/23/dialogues/1", {}, b""),
            ("23", "path"),
        )

    def test_json(self):
        self.assertEqual(
            extract_user_id(
                {"content-type": "application/json; charset=utf-8"},
                "/login", {}, b'{"user_id": 31}',
            ),
            ("31", "json:user_id"),
        )


if __name__ == "__main__":
    unittest.main()
