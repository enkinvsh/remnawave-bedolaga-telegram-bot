import base64
import hashlib
import hmac
from typing import Final

from app.config import settings


UNSUBSCRIBE_PURPOSE: Final = 'promo-email-unsubscribe'


def _signature(user_id: int) -> bytes:
    payload = f'{UNSUBSCRIBE_PURPOSE}:{user_id}'.encode()
    return hmac.new(settings.get_cabinet_jwt_secret().encode(), payload, hashlib.sha256).digest()


def create_unsubscribe_token(user_id: int) -> str:
    encoded_signature = base64.urlsafe_b64encode(_signature(user_id)).decode().rstrip('=')
    return f'{user_id}.{encoded_signature}'


def verify_unsubscribe_token(token: str) -> int | None:
    user_id_text, separator, encoded_signature = token.partition('.')
    if not separator or not user_id_text.isdigit() or not encoded_signature:
        return None
    user_id = int(user_id_text)
    expected = base64.urlsafe_b64encode(_signature(user_id)).decode().rstrip('=')
    return user_id if hmac.compare_digest(encoded_signature, expected) else None
