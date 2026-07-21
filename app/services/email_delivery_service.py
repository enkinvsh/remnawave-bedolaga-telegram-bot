import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from email.utils import formataddr
from functools import partial
from html import unescape
from typing import Final, NotRequired, TypedDict, assert_never
from urllib.parse import urlparse

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)
POSTBOX_PATH: Final = '/v2/email/outbound-emails'
CONTENT_TYPE: Final = 'application/json'


class _PostboxContent(TypedDict):
    Data: str
    Charset: str


class _PostboxBody(TypedDict):
    Html: _PostboxContent
    Text: _PostboxContent


class _PostboxMessageHeader(TypedDict):
    Name: str
    Value: str


class _PostboxSimpleContent(TypedDict):
    Subject: _PostboxContent
    Body: _PostboxBody
    Headers: NotRequired[list[_PostboxMessageHeader]]


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _postbox_headers(payload: str, now: datetime) -> dict[str, str]:
    access_key = settings.POSTBOX_ACCESS_KEY_ID
    secret_key = settings.POSTBOX_SECRET_ACCESS_KEY
    if access_key is None or secret_key is None:
        return {}

    endpoint = urlparse(settings.POSTBOX_ENDPOINT)
    host = endpoint.netloc
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    canonical_uri = f'{endpoint.path.rstrip("/")}{POSTBOX_PATH}'
    signed_headers = 'content-type;host;x-amz-content-sha256;x-amz-date'
    canonical_headers = (
        f'content-type:{CONTENT_TYPE}\n'
        f'host:{host}\n'
        f'x-amz-content-sha256:{payload_hash}\n'
        f'x-amz-date:{amz_date}\n'
    )
    canonical_request = f'POST\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}'
    credential_scope = f'{date_stamp}/{settings.POSTBOX_REGION}/ses/aws4_request'
    canonical_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f'AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{canonical_hash}'
    date_key = _sign(f'AWS4{secret_key}'.encode(), date_stamp)
    region_key = _sign(date_key, settings.POSTBOX_REGION)
    service_key = _sign(region_key, 'ses')
    signing_key = _sign(service_key, 'aws4_request')
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f'AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, '
        f'SignedHeaders={signed_headers}, Signature={signature}'
    )
    return {
        'Authorization': authorization,
        'Content-Type': CONTENT_TYPE,
        'Host': host,
        'X-Amz-Content-Sha256': payload_hash,
        'X-Amz-Date': amz_date,
    }


def _from_address() -> str | None:
    if settings.EMAIL_FROM:
        return settings.EMAIL_FROM
    from_email = settings.get_smtp_from_email()
    if from_email is None:
        return None
    return formataddr((settings.SMTP_FROM_NAME, from_email))


async def _send_postbox(
    *, to: str, subject: str, html: str, text: str | None, headers: dict[str, str] | None = None
) -> bool:
    from_address = _from_address()
    if not from_address or not settings.POSTBOX_ACCESS_KEY_ID or not settings.POSTBOX_SECRET_ACCESS_KEY:
        logger.error('Postbox email provider is not configured')
        return False

    text_body = text if text is not None else unescape(re.sub(r'<[^>]+>', '', html))
    simple_content: _PostboxSimpleContent = {
        'Subject': {'Data': subject, 'Charset': 'UTF-8'},
        'Body': {
            'Html': {'Data': html, 'Charset': 'UTF-8'},
            'Text': {'Data': text_body, 'Charset': 'UTF-8'},
        },
    }
    if headers:
        simple_content['Headers'] = [{'Name': name, 'Value': value} for name, value in headers.items()]
    payload = json.dumps(
        {
            'FromEmailAddress': from_address,
            'Destination': {'ToAddresses': [to]},
            'Content': {
                'Simple': simple_content,
            },
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )
    headers = _postbox_headers(payload, datetime.now(UTC))
    url = f'{settings.POSTBOX_ENDPOINT.rstrip("/")}{POSTBOX_PATH}'
    try:
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            url, data=payload.encode(), headers=headers
        ) as response:
            response.raise_for_status()
    except aiohttp.ClientError as error:
        logger.error('Postbox email delivery failed', error=error, recipient=to)
        return False
    return True


async def send_email(
    *, to: str, subject: str, html: str, text: str | None, headers: dict[str, str] | None = None
) -> bool:
    match settings.EMAIL_PROVIDER:
        case 'smtp':
            from app.cabinet.services.email_service import email_service

            if headers:
                send_smtp = partial(
                    email_service.send_email,
                    to_email=to,
                    subject=subject,
                    body_html=html,
                    body_text=text,
                    headers=headers,
                )
            else:
                send_smtp = partial(
                    email_service.send_email,
                    to_email=to,
                    subject=subject,
                    body_html=html,
                    body_text=text,
                )
            return await asyncio.to_thread(send_smtp)
        case 'postbox':
            return await _send_postbox(to=to, subject=subject, html=html, text=text, headers=headers)
        case unreachable:
            assert_never(unreachable)
