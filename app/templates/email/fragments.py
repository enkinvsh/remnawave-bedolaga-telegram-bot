from typing import Final


NEUTRAL_DEFAULTS: Final[dict[str, str]] = {
    'bg_color': '#0b0b10',
    'card_color': '#141418',
    'card_border_color': '#1d2c49',
    'footer_border_color': '#222226',
    'card_radius': '26px',
    'text_color': '#e6e6e6',
    'dim_color': '#9aa0aa',
    'muted_color': '#5b5b66',
    'accent_color': '#1e3a8a',
    'accent_end_color': '#3b82f6',
    'accent_border_color': '#2a61ce',
    'accent_sheen_color': '#306bec',
    'accent_text_color': '#ffffff',
    'button_radius': '26px',
    'font_stack': "Outfit, 'Onest', -apple-system, 'Segoe UI', Roboto, Arial, sans-serif",
    'title_font_stack': "'Unbounded', Outfit, 'Onest', -apple-system, 'Segoe UI', Roboto, Arial, sans-serif",
}

HEADER_IMAGE_BLOCK: Final[str] = """<tr>
  <td style="padding:0;line-height:0;border-radius:{card_radius} {card_radius} 0 0;overflow:hidden;">
    <img src="{url}" width="600" alt="{alt}" border="0"
      style="display:block;width:100%;max-width:600px;height:auto;border:0;
      border-radius:{card_radius} {card_radius} 0 0;outline:none;text-decoration:none;">
  </td>
</tr>"""

WORDMARK_BLOCK: Final[str] = """<tr>
  <td style="padding:24px 28px;color:{text_color};font-family:{font_stack};font-size:20px;font-weight:600;
    line-height:24px;letter-spacing:-0.3px;border-radius:{card_radius} {card_radius} 0 0;">
    {name}
  </td>
</tr>"""

CTA_BLOCK: Final[str] = """<table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0"
  bgcolor="{accent_color}"
  style="width:100%;border-collapse:separate;background-color:{accent_color};margin-top:24px;
  border-radius:{button_radius};">
  <tr>
    <td align="center" bgcolor="{accent_color}"
      style="padding:14px 20px;background-color:{accent_color};
      background-image:linear-gradient(90deg, {accent_color} 0%, {accent_end_color} 100%);
      border:1px solid {accent_border_color};border-radius:{button_radius};color:{accent_text_color};
      box-shadow:inset 0 1px 0 0 {accent_sheen_color};
      font-family:{font_stack};font-size:15px;font-weight:600;line-height:20px;letter-spacing:-0.35px;
      mso-padding-alt:14px 20px;">
      <a href="{url}" target="_blank"
        style="display:block;color:{accent_text_color};font-family:{font_stack};font-size:15px;font-weight:600;
        line-height:20px;letter-spacing:-0.35px;text-align:center;text-decoration:none;text-transform:none;
        white-space:nowrap;">{text}</a>
    </td>
  </tr>
</table>"""


def header_image_block(url: str, alt: str, card_radius: str) -> str:
    return HEADER_IMAGE_BLOCK.format(url=url, alt=alt, card_radius=card_radius)


def wordmark_block(name: str, text_color: str, card_radius: str) -> str:
    return WORDMARK_BLOCK.format(
        name=name,
        text_color=text_color,
        card_radius=card_radius,
        font_stack=NEUTRAL_DEFAULTS['font_stack'],
    )


def cta_block(
    text: str,
    url: str,
    accent_color: str,
    accent_end_color: str,
    accent_border_color: str,
    accent_sheen_color: str,
    accent_text_color: str,
    button_radius: str,
) -> str:
    return CTA_BLOCK.format(
        text=text,
        url=url,
        accent_color=accent_color,
        accent_end_color=accent_end_color,
        accent_border_color=accent_border_color,
        accent_sheen_color=accent_sheen_color,
        accent_text_color=accent_text_color,
        button_radius=button_radius,
        font_stack=NEUTRAL_DEFAULTS['font_stack'],
    )
