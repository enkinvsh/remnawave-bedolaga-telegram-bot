from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db
from app.cabinet.services.email_unsubscribe import verify_unsubscribe_token
from app.database.models import User


router = APIRouter(prefix='/email', tags=['Cabinet:Email'])
PROMO_OPT_OUT_FIELD: Final = 'promo_emails_opt_out_at'
CONFIRMATION_HTML = (
    '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Готово</title><p>Промо-письма отключены.</p></html>'
)


async def _unsubscribe(token: str, db: AsyncSession) -> HTMLResponse:
    user_id = verify_unsubscribe_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Недействительная ссылка')
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Недействительная ссылка')
    if user.promo_emails_opt_out_at is None:
        setattr(user, PROMO_OPT_OUT_FIELD, datetime.now(UTC))
        await db.commit()
    return HTMLResponse(CONFIRMATION_HTML)


@router.get('/unsubscribe', response_class=HTMLResponse)
async def unsubscribe_get(token: str, db: AsyncSession = Depends(get_cabinet_db)) -> HTMLResponse:
    return await _unsubscribe(token, db)


@router.post('/unsubscribe', response_class=HTMLResponse)
async def unsubscribe_post(token: str, db: AsyncSession = Depends(get_cabinet_db)) -> HTMLResponse:
    return await _unsubscribe(token, db)
