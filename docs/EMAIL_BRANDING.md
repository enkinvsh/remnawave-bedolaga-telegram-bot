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
