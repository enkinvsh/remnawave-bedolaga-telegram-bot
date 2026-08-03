from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LocaleOverride


async def get_locale_override(db: AsyncSession, key: str, language: str) -> LocaleOverride | None:
    result = await db.execute(
        select(LocaleOverride).where(
            LocaleOverride.key == key,
            LocaleOverride.language == language,
        )
    )
    return result.scalar_one_or_none()


async def list_locale_overrides(db: AsyncSession) -> list[LocaleOverride]:
    result = await db.execute(select(LocaleOverride).order_by(LocaleOverride.language, LocaleOverride.key))
    return list(result.scalars().all())


async def upsert_locale_override(db: AsyncSession, key: str, language: str, value: str) -> LocaleOverride:
    override = await get_locale_override(db, key, language)

    if override is None:
        override = LocaleOverride(key=key, language=language, value=value)
        db.add(override)
    else:
        override.value = value

    await db.flush()
    return override


async def delete_locale_override(db: AsyncSession, key: str, language: str) -> bool:
    """Drop the override so the bundled default takes over again."""
    override = await get_locale_override(db, key, language)
    if override is None:
        return False

    await db.delete(override)
    await db.flush()
    return True
