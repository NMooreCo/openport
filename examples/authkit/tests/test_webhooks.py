from authkit import webhooks


class FakeSession:
    def __init__(self):
        self.sent = []

    def post(self, url, json, timeout):
        self.sent.append((url, json, timeout))
        return type("Response", (), {"status_code": 204})()


def test_notify_posts_json_and_returns_the_status():
    session = FakeSession()
    assert webhooks.notify("https://example.com/hook", {"type": "user.created"}, session=session) == 204
    assert session.sent == [("https://example.com/hook", {"type": "user.created"}, 5)]
