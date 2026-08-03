"""Screen-first preview for the locale editor.

The flat editor lists ~2000 keys; finding the six that compose one screen is
hopeless by hand. Here a screen is a first-class thing: pick it, render it
through the bot's own handler, get back the text plus the exact strings it was
built from — in the order they were touched.
"""

from .registry import ScreenDefinition, ScreenRenderer, get_screen, list_screens, register_screen
from .renderer import render_screen, supported_languages
from .synthetic import build_synthetic_user


__all__ = [
    'ScreenDefinition',
    'ScreenRenderer',
    'build_synthetic_user',
    'get_screen',
    'list_screens',
    'register_screen',
    'render_screen',
    'supported_languages',
]
