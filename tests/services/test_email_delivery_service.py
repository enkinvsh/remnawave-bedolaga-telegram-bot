import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from app.cabinet.services.email_service import email_service
from app.config import settings
from app.services import email_delivery_service


@pytest.mark.asyncio
async def test_send_email_uses_existing_smtp_sender_by_default(monkeypatch: pytest.MonkeyPatch):
    smtp_send = MagicMock(return_value=True)
    monkeypatch.setattr(settings, 'EMAIL_PROVIDER', 'smtp')
    monkeypatch.setattr(email_service, 'send_email', smtp_send)

    sent = await email_delivery_service.send_email(
        to='user@example.com',
        subject='Subject',
        html='<p>Body</p>',
        text='Body',
    )

    assert sent is True
    smtp_send.assert_called_once_with(
        to_email='user@example.com',
        subject='Subject',
        body_html='<p>Body</p>',
        body_text='Body',
    )


@pytest.mark.asyncio
async def test_send_email_builds_sigv4_postbox_request(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, url, *, data, headers):
            captured.update({'url': url, 'data': data, 'headers': headers})
            return Response()

    monkeypatch.setattr(settings, 'EMAIL_PROVIDER', 'postbox')
    monkeypatch.setattr(settings, 'POSTBOX_ENDPOINT', 'https://postbox.cloud.yandex.net')
    monkeypatch.setattr(settings, 'POSTBOX_REGION', 'ru-central1')
    monkeypatch.setattr(settings, 'POSTBOX_ACCESS_KEY_ID', 'test-access-key')
    monkeypatch.setattr(settings, 'POSTBOX_SECRET_ACCESS_KEY', 'test-secret-key')
    monkeypatch.setattr(settings, 'EMAIL_FROM', 'Brand <mail@example.com>')
    monkeypatch.setattr(email_delivery_service.aiohttp, 'ClientSession', Session)

    sent = await email_delivery_service.send_email(
        to='user@example.com',
        subject='Subject',
        html='<p>Body</p>',
        text='Body',
    )

    assert sent is True
    assert captured['url'] == 'https://postbox.cloud.yandex.net/v2/email/outbound-emails'
    assert captured['headers']['Host'] == 'postbox.cloud.yandex.net'
    authorization = captured['headers']['Authorization']
    assert authorization.startswith('AWS4-HMAC-SHA256 ')
    assert 'Credential=test-access-key/' in authorization
    assert '/ru-central1/ses/aws4_request' in authorization
    payload = json.loads(captured['data'])
    assert payload == {
        'FromEmailAddress': 'Brand <mail@example.com>',
        'Destination': {'ToAddresses': ['user@example.com']},
        'Content': {
            'Simple': {
                'Subject': {'Data': 'Subject', 'Charset': 'UTF-8'},
                'Body': {
                    'Html': {'Data': '<p>Body</p>', 'Charset': 'UTF-8'},
                    'Text': {'Data': 'Body', 'Charset': 'UTF-8'},
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_send_email_propagates_headers_into_postbox_payload(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, _url, *, data, headers):
            captured.update({'data': data, 'headers': headers})
            return Response()

    monkeypatch.setattr(settings, 'EMAIL_PROVIDER', 'postbox')
    monkeypatch.setattr(settings, 'POSTBOX_ACCESS_KEY_ID', 'test-access-key')
    monkeypatch.setattr(settings, 'POSTBOX_SECRET_ACCESS_KEY', 'test-secret-key')
    monkeypatch.setattr(settings, 'EMAIL_FROM', 'Brand <mail@example.com>')
    monkeypatch.setattr(email_delivery_service.aiohttp, 'ClientSession', Session)

    sent = await email_delivery_service.send_email(
        to='user@example.com',
        subject='Subject',
        html='<p>Body</p>',
        text='Body',
        headers={
            'List-Unsubscribe': '<https://example.com/unsubscribe?token=x>',
            'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
        },
    )

    assert sent is True
    payload = json.loads(captured['data'])
    assert payload['Content']['Simple']['Headers'] == [
        {'Name': 'List-Unsubscribe', 'Value': '<https://example.com/unsubscribe?token=x>'},
        {'Name': 'List-Unsubscribe-Post', 'Value': 'List-Unsubscribe=One-Click'},
    ]


@pytest.mark.asyncio
async def test_postbox_retries_two_rate_limits_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    statuses = [429, 429, 200]
    attempts = 0

    class Response:
        def __init__(self, response_status: int):
            self.status = response_status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def raise_for_status(self):
            if self.status == 429:
                raise aiohttp.ClientResponseError(MagicMock(), (), status=429)

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, _url, *, data, headers):
            nonlocal attempts
            response = Response(statuses[attempts])
            attempts += 1
            return response

    sleep = AsyncMock()
    monkeypatch.setattr(settings, 'EMAIL_PROVIDER', 'postbox')
    monkeypatch.setattr(settings, 'POSTBOX_ACCESS_KEY_ID', 'test-access-key')
    monkeypatch.setattr(settings, 'POSTBOX_SECRET_ACCESS_KEY', 'test-secret-key')
    monkeypatch.setattr(settings, 'EMAIL_FROM', 'Brand <mail@example.com>')
    monkeypatch.setattr(email_delivery_service.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(email_delivery_service.asyncio, 'sleep', sleep)

    sent = await email_delivery_service.send_email(
        to='user@example.com', subject='Subject', html='<p>Body</p>', text='Body'
    )

    assert sent is True
    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.15, 1, 3]


@pytest.mark.asyncio
async def test_postbox_stops_after_third_rate_limit(monkeypatch: pytest.MonkeyPatch):
    attempts = 0

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def raise_for_status(self):
            raise aiohttp.ClientResponseError(MagicMock(), (), status=429)

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def post(self, _url, *, data, headers):
            nonlocal attempts
            attempts += 1
            return Response()

    sleep = AsyncMock()
    monkeypatch.setattr(settings, 'EMAIL_PROVIDER', 'postbox')
    monkeypatch.setattr(settings, 'POSTBOX_ACCESS_KEY_ID', 'test-access-key')
    monkeypatch.setattr(settings, 'POSTBOX_SECRET_ACCESS_KEY', 'test-secret-key')
    monkeypatch.setattr(settings, 'EMAIL_FROM', 'Brand <mail@example.com>')
    monkeypatch.setattr(email_delivery_service.aiohttp, 'ClientSession', Session)
    monkeypatch.setattr(email_delivery_service.asyncio, 'sleep', sleep)

    sent = await email_delivery_service.send_email(
        to='user@example.com', subject='Subject', html='<p>Body</p>', text='Body'
    )

    assert sent is False
    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.15, 1, 3]
