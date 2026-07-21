# Брендированные email-письма

## Что меняется

Брендированный слой помещает существующее содержимое email-шаблона в единый HTML-макет инстанса: с его названием,
цветами, шапкой и CTA-кнопкой. Тексты и переменные шаблонов продолжают настраиваться в редакторе email-шаблонов.

Функция выключена по умолчанию. Пока `CABINET_EMAIL_LAYOUT_ENABLED=false`, HTML писем и SMTP-доставка работают как
раньше.

## Включение

Настройка хранится в системных настройках инстанса под ключом `CABINET_EMAIL_LAYOUT_ENABLED` со значением `true` или
`false`. В branding API ей соответствуют методы:

- `GET /cabinet/branding/email-layout`;
- `PUT /cabinet/branding/email-layout` с JSON `{"enabled": true}`. Нужны права `settings:edit`.

Чтобы вернуться к старому макету, отправьте `{"enabled": false}`. Перезапуск приложения не требуется.

## Цвета, название и шапка

Цвета берутся из существующей настройки оформления кабинета `CABINET_THEME_COLORS`. Меняйте их через уже
используемую branding-админку. Для письма применяются:

- `darkBackground` — фон письма;
- `darkSurface` — карточка;
- `darkText` — основной текст;
- `darkTextSecondary` — вторичный текст;
- `accent` — CTA и производные границы.

Начальный цвет градиента CTA и границы вычисляются автоматически из `accent`; отдельные email-цвета задавать не
нужно.

Название берётся из `CABINET_BRANDING_NAME`. Если оно не задано, используется `SMTP_FROM_NAME`, затем стандартное
имя сервиса. Без отдельной шапки письмо показывает текстовый wordmark с этим названием.

Для графической шапки задайте системную настройку `CABINET_EMAIL_HEADER_URL`. Требования к файлу:

- рекомендуемый размер 1200×480 px (соотношение около 2,5:1);
- вес не более 150 КБ;
- публичный URL по HTTPS без авторизации и временных query-токенов;
- PNG, JPEG или WebP с уже подготовленным фоном.

Обычный логотип кабинета автоматически в письмо не подставляется: email-клиентам нужен стабильный публичный HTTPS
URL, поэтому для шапки используется отдельный `CABINET_EMAIL_HEADER_URL`.

## Preview

Откройте нужный тип письма в редакторе или вызовите
`POST /cabinet/admin/email-templates/{type}/preview`. При включённом `CABINET_EMAIL_LAYOUT_ENABLED` endpoint возвращает
финальный брендированный HTML конкретного инстанса. Проверяйте preview после изменения цветов, названия или шапки.

## Выбор провайдера

Провайдер задаётся переменной окружения `EMAIL_PROVIDER`:

- `smtp` — значение по умолчанию. Используется существующая SMTP-конфигурация без изменения поведения;
- `postbox` — Yandex Cloud Postbox через SES v2 API и AWS Signature Version 4.

Переключение провайдера требует перезапуска приложения. Автоматического fallback с Postbox на SMTP нет: скрытый
fallback затруднил бы диагностику доставки.

## Настройка Yandex Cloud Postbox

### 1. Сервисный аккаунт и статический ключ

Создайте сервисный аккаунт в нужном каталоге и выдайте ему роль `postbox.sender`:

```bash
yc iam service-account create --name zenbot-postbox
yc resource-manager folder add-access-binding <FOLDER_ID> \
  --role postbox.sender \
  --subject serviceAccount:<SERVICE_ACCOUNT_ID>
yc iam access-key create --service-account-name zenbot-postbox
```

Последняя команда показывает `key_id` и секретный ключ. Секрет выводится один раз. Не добавляйте его в git, логи или
скриншоты.

### 2. Domain identity и BYODKIM

Рекомендуется отдельный поддомен отправки, например `mail.example.com`. Создайте приватный RSA-ключ и selector:

```bash
openssl genrsa -out dkim-private.pem 2048
selector=postbox
```

Зарегистрируйте identity через SES-совместимый endpoint. AWS CLI читает статический ключ из стандартных переменных:

```bash
export AWS_ACCESS_KEY_ID='<key_id>'
export AWS_SECRET_ACCESS_KEY='<secret>'
export AWS_DEFAULT_REGION='ru-central1'

aws --endpoint-url https://postbox.cloud.yandex.net sesv2 create-email-identity \
  --region ru-central1 \
  --email-identity mail.example.com \
  --dkim-signing-attributes \
  "DomainSigningSelector=${selector},DomainSigningPrivateKey=$(openssl base64 -A -in dkim-private.pem)"
```

Не публикуйте `dkim-private.pem`. Для DNS нужен только публичный ключ:

```bash
openssl rsa -in dkim-private.pem -pubout -outform DER 2>/dev/null | openssl base64 -A
```

### 3. DNS

Добавьте записи и дождитесь их распространения:

| Имя | Тип | Значение |
|---|---|---|
| `<selector>._domainkey.mail.example.com` | TXT | `v=DKIM1;h=sha256;k=rsa;p=<PUBLIC_KEY_BASE64>` |
| `mail.example.com` | TXT | `v=spf1 include:_spf.postbox.cloud.yandex.net ~all` |
| `_dmarc.example.com` | TXT | `v=DMARC1; p=none; rua=mailto:dmarc@example.com` |

Начните DMARC с `p=none`, проверьте отчёты и только потом ужесточайте политику. Не создавайте вторую SPF-запись на
том же имени: объедините разрешённые источники в одну запись.

Проверьте статус identity:

```bash
aws --endpoint-url https://postbox.cloud.yandex.net sesv2 get-email-identity \
  --region ru-central1 \
  --email-identity mail.example.com
```

### 4. Переменные окружения zenbot

```dotenv
EMAIL_PROVIDER=postbox
POSTBOX_ENDPOINT=https://postbox.cloud.yandex.net
POSTBOX_REGION=ru-central1
POSTBOX_ACCESS_KEY_ID=<key_id>
POSTBOX_SECRET_ACCESS_KEY=<secret>
EMAIL_FROM=Service Name <no-reply@mail.example.com>
```

`EMAIL_FROM` должен использовать подтверждённый domain identity. Если переменная не задана, приложение собирает
адрес из `SMTP_FROM_NAME` и `SMTP_FROM_EMAIL` (или `SMTP_USER`). Для независимой конфигурации Postbox лучше задавать
`EMAIL_FROM` явно.

После изменения env перезапустите приложение и отправьте тестовое письмо на свой адрес. Если Postbox отвечает
ошибкой подписи, сначала проверьте регион `ru-central1`, часы на сервере, `key_id` и отсутствие пробелов в секрете.

## Lifecycle-письма

Lifecycle-модуль предназначен для пользователей с подтверждённым email, у которых нет связанного Telegram-аккаунта
(`email_verified=true`, `telegram_id IS NULL`). Он отправляет предупреждение за два часа до завершения триала,
скидочные ступени после триала и две волны возврата после завершения платной подписки. Тайминги, проценты и срок
действия берутся из тех же lifecycle-настроек, что и Telegram-сценарии.

Модуль полностью выключен по умолчанию. Для запуска добавьте системную настройку
`CABINET_LIFECYCLE_EMAILS_ENABLED=true`. Отсутствующий ключ или любое значение, кроме `true`, означает полный no-op.
Telegram-сценарии этот переключатель не меняет.

Скидка в промо-письме активируется на сервере до отправки: пользователь сразу получает
`promo_offer_discount_percent`, а связанный `DiscountOffer` отмечается использованным. Кнопки «забрать скидку» нет.
Письмо сообщает уже активный процент и время завершения предложения.

Ступени после триала и win-back считаются промо-письмами. В них есть ссылка отказа и заголовки
`List-Unsubscribe`/`List-Unsubscribe-Post` для one-click отказа. Публичные `GET` и `POST`
`/cabinet/email/unsubscribe?token=...` идемпотентно заполняют `promo_emails_opt_out_at`; после этого промо-письма не
отправляются. Предупреждение о завершении триала является сервисным, поэтому отказ на него не распространяется.

В письмо об успешном пополнении баланса брендированный макет добавляет реферальный блок, только если включены и
`CABINET_EMAIL_LAYOUT_ENABLED`, и `REFERRAL_PROGRAM_ENABLED`. Процент комиссии и оба бонуса читаются из
`REFERRAL_COMMISSION_PERCENT`, `REFERRAL_INVITER_BONUS_KOPEKS` и `REFERRAL_FIRST_TOPUP_BONUS_KOPEKS`: у каждого
инстанса значения свои.

Перед включением lifecycle-писем оператор должен проверить:

- подтверждённый sending domain и корректный адрес `EMAIL_FROM`;
- рабочие SPF/DKIM и DMARC с контролируемыми отчётами;
- тестовую доставку через выбранного провайдера;
- корректный публичный `CABINET_URL`, поскольку он используется в CTA и ссылке отказа.
