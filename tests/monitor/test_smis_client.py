import unittest
from concurrent.futures import ThreadPoolExecutor

from steam_skin_ops.monitor.integrations.smis import SmisClient, SmisClientError


DETAIL = {
    "code": 200,
    "data": {
        "id": 61753,
        "appid": 730,
        "hashName": "Kilowatt Case",
        "cnName": "千瓦武器箱",
    },
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = DETAIL if payload is None else payload
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses, clock):
        self.headers = {}
        self.responses = list(responses)
        self.clock = clock
        self.request_times = []

    def request(self, *args, **kwargs):
        self.request_times.append(self.clock())
        return self.responses.pop(0)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client_with(responses, *, retries=3, interval=1.0):
    fake_time = FakeTime()
    session = FakeSession(responses, fake_time.clock)
    client = SmisClient(
        session=session,
        max_retries=retries,
        min_request_interval=interval,
    )
    client._clock = fake_time.clock
    client._sleep = fake_time.sleep
    return client, session, fake_time


class SmisClientTestCase(unittest.TestCase):
    def test_global_rate_limit_applies_across_threads(self):
        client, session, fake_time = client_with(
            [FakeResponse(), FakeResponse(), FakeResponse()], interval=1.5
        )

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(client.fetch_metadata, [61753] * 3))

        self.assertEqual(len(results), 3)
        self.assertEqual(session.request_times, [0.0, 1.5, 3.0])
        self.assertEqual(fake_time.sleeps, [1.5, 1.5])

    def test_access_denied_and_other_client_errors_do_not_retry(self):
        for status, message in ((403, "拒绝访问"), (404, "不可接受")):
            with self.subTest(status=status):
                client, session, _ = client_with(
                    [FakeResponse(status), FakeResponse()], retries=3
                )
                with self.assertRaisesRegex(SmisClientError, message):
                    client.fetch_metadata(61753)
                self.assertEqual(len(session.request_times), 1)

    def test_rate_limit_respects_retry_after_and_server_error_backs_off(self):
        client, session, fake_time = client_with([
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(503),
            FakeResponse(),
        ])

        result = client.fetch_metadata(61753)

        self.assertEqual(result["smis_id"], 61753)
        self.assertEqual(len(session.request_times), 3)
        self.assertEqual(fake_time.sleeps, [7.0, 2])

    def test_retryable_error_reports_exhaustion(self):
        client, session, _ = client_with(
            [FakeResponse(500), FakeResponse(502)], retries=2
        )

        with self.assertRaisesRegex(SmisClientError, "重试耗尽.*HTTP 502"):
            client.fetch_metadata(61753)
        self.assertEqual(len(session.request_times), 2)


if __name__ == "__main__":
    unittest.main()
